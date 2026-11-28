// Whistle trigger for the ATOM Echo. The PDM mic at 16 kHz feeds a ring buffer and the v1 note-sequence detector
// (lib/whistle_dsp). An event of >=3 notes is a trigger: its clip is POSTed with debug metadata to a whistle-lab
// server (DEBUG_SERVER), which re-checks it with the full detector and replies with a verdict. Heartbeats go to
// the same server every 10 s. Debug mode (HA switch, docs/debug-stream.md): detection off, all audio streamed to the
// server for DEBUG_TIMEOUT_MS. With MQTT_HOST set, checked triggers and heartbeats also go to Home Assistant over
// MQTT (ha_mqtt.cpp, docs/mqtt.md). Also joins Wi-Fi and accepts ArduinoOTA uploads.
#include <Arduino.h>
#include <ArduinoOTA.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <driver/i2s.h>
#include <math.h>

#include <atomic>

#include "detector.h"
#include "ha_mqtt.h"

namespace {
constexpr int PIN_LED = 27;       // SK6812
constexpr int PIN_BUTTON = 39;    // active low, external pull-up
constexpr int PIN_MIC_CLK = 33;   // shared with speaker LRCK: mic and speaker can't run together
constexpr int PIN_MIC_DATA = 23;
constexpr int PIN_SPK_BCLK = 19;  // NS4168 amp; its LRCK is PIN_MIC_CLK
constexpr int PIN_SPK_DATA = 22;
constexpr i2s_port_t MIC_PORT = I2S_NUM_0;  // PDM RX only exists on I2S0
constexpr uint32_t SAMPLE_RATE = wd::FS;
constexpr int HOP = wd::HOP, NFFT = wd::NFFT;

constexpr int MIN_NOTES = 3;           // notes.Params.min_notes_to_forward
constexpr float PRE_S = 0.25f, POST_S = 0.25f, MAX_CLIP_S = 4.0f;
constexpr uint32_t HEARTBEAT_MS = 10000;
constexpr uint32_t DEBUG_TIMEOUT_MS = 10 * 60 * 1000;  // debug mode reverts to normal operation after this
constexpr int DEBUG_FRAME_SAMPLES = 1024;
constexpr bool HAVE_WIFI = sizeof(WIFI_SSID) > 1;
constexpr bool HAVE_SERVER = HAVE_WIFI && sizeof(DEBUG_SERVER) > 1;
constexpr bool HAVE_NET_TASK = HAVE_SERVER || ha::ENABLED;

// ---- ring buffer: int16 at 16 kHz in 32 KB blocks (the heap's largest free block is ~110 KB) ----
constexpr size_t BLOCK_SAMPLES = 16384;
constexpr int MAX_BLOCKS = 5;            // 5.1 s
constexpr size_t MIN_FREE_AFTER = 70000;       // leave this much heap for Wi-Fi and TCP
constexpr size_t MIN_FREE_AFTER_TLS = 105000;  // plus the mbedTLS buffers and handshake
int16_t* ring_blocks[MAX_BLOCKS];
int n_blocks = 0;
uint32_t ring_len = 0;                    // samples
std::atomic<uint32_t> write_pos{0};       // total samples written; wraps after 74 h, differences stay correct

void ring_init(size_t min_free) {
  while (n_blocks < MAX_BLOCKS && ESP.getMaxAllocHeap() >= BLOCK_SAMPLES * 2 &&
         ESP.getFreeHeap() >= BLOCK_SAMPLES * 2 + min_free) {
    ring_blocks[n_blocks] = static_cast<int16_t*>(malloc(BLOCK_SAMPLES * 2));
    if (!ring_blocks[n_blocks]) break;
    n_blocks++;
  }
  ring_len = n_blocks * BLOCK_SAMPLES;
}

inline int16_t& ring_at(uint32_t pos) {
  const uint32_t i = pos % ring_len;
  return ring_blocks[i / BLOCK_SAMPLES][i % BLOCK_SAMPLES];
}

// Sample `pos` is intact if the writer (which writes HOP samples before advancing write_pos) hasn't reached
// pos + ring_len.
inline bool ring_intact(uint32_t pos) { return write_pos.load() + HOP - pos <= ring_len; }

// ---- jobs for the network task ----
struct Job {
  bool trigger;
  uint32_t start, end;  // sample positions (trigger)
  String json;          // trigger metadata, or the heartbeat body
  // trigger, for MQTT
  float level_db;  // members left out of a brace-init are zeroed
  int n_notes;
  String intervals;
  uint32_t detected_ms;
};
QueueHandle_t jobs;

// Written by the network task, read by loop().
std::atomic<uint32_t> n_sent{0}, n_failed{0}, n_overrun{0}, n_dropped{0};
std::atomic<int> led_result{0};          // 1 detected, -1 rejected/error
std::atomic<uint32_t> led_result_ms{0};
std::atomic<int> beep_result{0};         // a Result, played (once) by loop()
std::atomic<bool> uploading{false};

// Debug mode: ha::debug_active is the mode (owned by loop()); the generation tells the network task a new
// session began, `debug_start` is the first sample to stream.
std::atomic<uint32_t> debug_gen{0}, debug_start{0}, debug_deadline{0};
std::atomic<bool> debug_streaming{false};  // the network task has a live connection to the server

bool audio_ok = false;
uint32_t last_read = 0;  // millis() at the last mic read

// ---- mic ----
bool mic_init() {
  const i2s_config_t cfg = {
      .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
      .sample_rate = SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = 0,
      .dma_buf_count = 8,
      .dma_buf_len = HOP,
  };
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = I2S_PIN_NO_CHANGE;
  pins.ws_io_num = PIN_MIC_CLK;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_DATA;
  // Log rather than abort: a crash loop here would also take OTA down.
  esp_err_t e = i2s_driver_install(MIC_PORT, &cfg, 0, nullptr);
  if (e == ESP_OK) e = i2s_set_pin(MIC_PORT, &pins);
  if (e != ESP_OK) Serial.printf("mic init failed: %s\n", esp_err_to_name(e));
  return e == ESP_OK;
}

// ---- speaker ----
// Square-ish loudness is set here; the little speaker is easily overdriven, so stay well below full scale.
constexpr float BEEP_AMPLITUDE = 6000;
constexpr int BEEP_MS = 90, GAP_MS = 50;
constexpr int SPK_DMA_BUFS = 4, SPK_DMA_LEN = 256;  // frames

// Plays the tones (Hz) and returns once they are out of the speaker. The mic must be uninstalled.
void play_tones(const int* freqs, int n) {
  const i2s_config_t cfg = {
      .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_TX),
      .sample_rate = SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = 0,
      .dma_buf_count = SPK_DMA_BUFS,
      .dma_buf_len = SPK_DMA_LEN,
      .use_apll = false,
      .tx_desc_auto_clear = true,
  };
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = PIN_SPK_BCLK;
  pins.ws_io_num = PIN_MIC_CLK;
  pins.data_out_num = PIN_SPK_DATA;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  esp_err_t e = i2s_driver_install(MIC_PORT, &cfg, 0, nullptr);
  if (e != ESP_OK) {
    Serial.printf("speaker init failed: %s\n", esp_err_to_name(e));
    return;
  }
  e = i2s_set_pin(MIC_PORT, &pins);
  if (e == ESP_OK) {
    int16_t buf[2 * 128];
    int fill = 0;
    size_t written;
    auto put = [&](int16_t v) {
      buf[fill++] = v;
      buf[fill++] = v;
      if (fill == int(sizeof buf / sizeof *buf)) {
        i2s_write(MIC_PORT, buf, sizeof buf, &written, portMAX_DELAY);
        fill = 0;
      }
    };
    constexpr int N = SAMPLE_RATE * BEEP_MS / 1000, RAMP = SAMPLE_RATE * 5 / 1000;
    for (int t = 0; t < n; t++) {
      for (int i = 0; i < N; i++) {
        const float env = std::min(1.f, std::min(i, N - 1 - i) / float(RAMP));  // no clicks
        put(int16_t(BEEP_AMPLITUDE * env * sinf(2 * float(M_PI) * freqs[t] * i / SAMPLE_RATE)));
      }
      for (int i = 0; i < SAMPLE_RATE * GAP_MS / 1000; i++) put(0);
    }
    // i2s_write returns when data is queued: push the tones out of the DMA with a ring's worth of silence.
    for (int i = 0; i < SPK_DMA_BUFS * SPK_DMA_LEN + 128; i++) put(0);
  } else {
    Serial.printf("speaker pins failed: %s\n", esp_err_to_name(e));
  }
  i2s_driver_uninstall(MIC_PORT);
}

// Swaps I2S0 from the mic to the speaker for the tones, then back. Nothing is recorded (and the beeps can't
// trigger) meanwhile. Returns false if the mic doesn't come back.
bool beep(const int* freqs, int n) {
  i2s_driver_uninstall(MIC_PORT);
  play_tones(freqs, n);
  if (!mic_init()) return false;
  // Drop the PDM start-up transient and the room's tail of the last beep.
  int16_t junk[HOP];
  size_t got;
  for (int i = 0; i < 12; i++) i2s_read(MIC_PORT, junk, sizeof junk, &got, portMAX_DELAY);
  return true;
}

// The verdict on a trigger. With HA, a detection's verdict is HA's `feedback` (docs/mqtt.md).
enum Result : int { FAILED = -1, NONE = 0, DETECTED = 1, NO_ACTION = 2 };

// Three rising: detected (and HA acted). Two flat: HA has nothing set up for it. FAILED (rejected, upload
// failed, or HA didn't answer) is silent: LED only.
bool beep_result_tones(int r) {
  static const int UP[] = {1200, 1600, 2100}, FLAT[] = {1400, 1400};
  if (r == FAILED) return true;
  return r == DETECTED ? beep(UP, 3) : beep(FLAT, 2);
}

// ---- network ----
char server_host[64];
uint16_t server_port = 80;
bool server_tls = false;
char host_hdr[80];  // Host header value: the port is left out when it is the scheme default

// DEBUG_SERVER is host:port, http://host[:port] or https://host[:port] (TLS, port 443 by default).
void parse_server() {
  const char* s = DEBUG_SERVER;
  if (!strncmp(s, "http://", 7)) s += 7;
  else if (!strncmp(s, "https://", 8)) {
    s += 8;
    server_tls = true;
    server_port = 443;
  }
  strlcpy(server_host, s, sizeof(server_host));
  if (char* c = strchr(server_host, ':')) {
    *c = 0;
    server_port = atoi(c + 1);
  }
  if (char* c = strchr(server_host, '/')) *c = 0;
  if (server_port == (server_tls ? 443 : 80)) strlcpy(host_hdr, server_host, sizeof host_hdr);
  else snprintf(host_hdr, sizeof host_hdr, "%s:%u", server_host, unsigned(server_port));
}

void wav_header(uint8_t* h, uint32_t n) {
  auto u32 = [&](int o, uint32_t v) { memcpy(h + o, &v, 4); };
  auto u16 = [&](int o, uint16_t v) { memcpy(h + o, &v, 2); };
  memcpy(h, "RIFF", 4); u32(4, 36 + 2 * n); memcpy(h + 8, "WAVEfmt ", 8);
  u32(16, 16); u16(20, 1); u16(22, 1); u32(24, SAMPLE_RATE); u32(28, 2 * SAMPLE_RATE); u16(32, 2); u16(34, 16);
  memcpy(h + 36, "data", 4); u32(40, 2 * n);
}

// Reads one HTTP response, up to Content-Length, sleeping between polls. Not Stream::readString: in this core
// it spins until the timeout without yielding, which starves core 0 and trips the task watchdog.
bool read_response(WiFiClient& c, String& head, String& body, uint32_t timeout_ms) {
  String r;
  int body_at = -1, body_len = -1;
  uint8_t buf[256];
  for (const uint32_t t0 = millis(); millis() - t0 < timeout_ms;) {
    const int n = c.available() ? c.read(buf, sizeof buf) : 0;
    if (n <= 0) {
      if (!c.connected()) break;
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    for (int i = 0; i < n; i++) r += char(buf[i]);
    if (body_at < 0) {
      const int end = r.indexOf("\r\n\r\n");
      if (end < 0) continue;
      body_at = end + 4;
      String h = r.substring(0, end);
      h.toLowerCase();
      const int cl = h.indexOf("content-length:");
      if (cl >= 0) body_len = h.substring(cl + 15).toInt();
    }
    if (body_len >= 0 && int(r.length()) - body_at >= body_len) break;
  }
  if (body_at < 0) return false;
  head = r.substring(0, r.indexOf('\r'));
  body = r.substring(body_at);
  return body_len < 0 || int(body.length()) >= body_len;
}

// Connects to the server over `plain` or `tls` and returns whichever was used, or nullptr.
WiFiClient* connect_server(WiFiClientSecure& tls, WiFiClient& plain, const char* what) {
  // No certificate check: the server is on the LAN and the clips are not secret. TLS here is only to get past
  // the reverse proxy, which redirects plain HTTP to HTTPS.
  tls.setInsecure();
  WiFiClient& c = server_tls ? static_cast<WiFiClient&>(tls) : plain;
  c.setTimeout(5);  // seconds, for socket sends
  const bool up = server_tls ? tls.connect(server_host, server_port, 8000) : plain.connect(server_host, server_port, 3000);
  if (!up) {
    Serial.printf("%s: connect to %s:%u failed\n", what, server_host, server_port);
    return nullptr;
  }
  return &c;
}

// POSTs a body built by `send` (which must write exactly `len` bytes) and returns the response body, or ""
// on failure.
template <typename F>
String http_post(const char* path, const char* content_type, size_t len, F send) {
  // Shared secret for the server's WHISTLE_DEVICE_TOKEN check; empty = none sent.
  static const String token_header = sizeof(DEVICE_TOKEN) > 1 ? String("X-Whistle-Token: " DEVICE_TOKEN "\r\n") : String();
  WiFiClientSecure tls;
  WiFiClient plain;
  WiFiClient* conn = connect_server(tls, plain, path);
  if (!conn) return "";
  WiFiClient& c = *conn;
  c.printf("POST %s HTTP/1.1\r\nHost: %s\r\n%sContent-Type: %s\r\nContent-Length: %u\r\nConnection: close\r\n\r\n",
           path, host_hdr, token_header.c_str(), content_type, unsigned(len));
  if (!send(c)) return "";
  String status, body;
  // 10 s: the server runs the full detector before replying.
  if (!read_response(c, status, body, 10000)) {
    Serial.printf("POST %s: no complete response\n", path);
    return "";
  }
  if (!status.startsWith("HTTP/1.1 200")) {
    Serial.printf("POST %s: %s %s\n", path, status.c_str(), body.substring(0, 200).c_str());
    return "";
  }
  return body;
}

// Minimal readers for the server's flat JSON reply: "" / NAN when the key is missing or null.
String json_str(const String& s, const char* key) {
  const String k = String("\"") + key + "\":\"";
  const int a = s.indexOf(k);
  if (a < 0) return "";
  const int b = s.indexOf('"', a + k.length());
  return b < 0 ? "" : s.substring(a + k.length(), b);
}

float json_num(const String& s, const char* key) {
  const String k = String("\"") + key + "\":";
  const int a = s.indexOf(k);
  if (a < 0) return NAN;
  const char* p = s.c_str() + a + k.length();
  char* end;
  const float v = strtof(p, &end);
  return end == p ? NAN : v;
}

bool post_trigger(Job& j) {
  static const char* B = "----whistleclip";
  const uint32_t n = j.end - j.start;
  String head = String("--") + B + "\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n";
  // The overrun flag is known only after the audio has been streamed, so the metadata part comes last and is
  // padded to a fixed size (Content-Length must be known up front).
  String mid = String("\r\n--") + B + "\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n";
  String tail = String("\r\n--") + B + "--\r\n";
  constexpr int FLAG_LEN = 17;  // `,"overrun":false}`, "true" padded with a space
  j.json.remove(j.json.length() - 1);  // drop the closing brace; the flag closes it
  const size_t len = head.length() + 44 + 2 * n + mid.length() + j.json.length() + FLAG_LEN + tail.length();
  char ctype[64];
  snprintf(ctype, sizeof ctype, "multipart/form-data; boundary=%s", B);

  bool overrun = false;
  String reply = http_post("/api/device/trigger", ctype, len, [&](WiFiClient& c) {
    uint8_t buf[1024];
    c.print(head);
    wav_header(buf, n);
    c.write(buf, 44);
    for (uint32_t p = j.start; p < j.end;) {
      const uint32_t m = std::min<uint32_t>(sizeof(buf) / 2, j.end - p);
      int16_t* out = reinterpret_cast<int16_t*>(buf);
      if (!ring_intact(p)) overrun = true;
      for (uint32_t i = 0; i < m; i++) out[i] = ring_at(p + i);
      if (!ring_intact(p)) overrun = true;
      if (c.write(buf, 2 * m) != 2 * m) return false;
      p += m;
    }
    c.print(mid);
    c.print(j.json);
    char flag[FLAG_LEN + 1];
    snprintf(flag, sizeof flag, ",\"overrun\":%-5s}", overrun ? "true" : "false");
    c.write(reinterpret_cast<uint8_t*>(flag), FLAG_LEN);
    c.print(tail);
    return true;
  });
  if (overrun) n_overrun++;
  if (reply.isEmpty()) return false;
  const bool detected = reply.indexOf("\"detected\":true") >= 0;
  Serial.printf("server: %s\n", reply.c_str());
  const String label = json_str(reply, "label"), veto = json_str(reply, "veto");
  ha::publish_detection({detected ? label.c_str() : "rejected", json_num(reply, "rho"), j.level_db, j.n_notes,
                         j.intervals, j.detected_ms, veto.isEmpty() ? nullptr : veto.c_str()});
  led_result = detected ? 1 : -1;
  led_result_ms = millis();
  return true;
}

bool post_heartbeat(Job& j) {
  return !http_post("/api/device/heartbeat", "application/json", j.json.length(), [&](WiFiClient& c) {
            return c.print(j.json) == j.json.length();
          }).isEmpty();
}

// ---- debug stream ----
// Frames are `<IIIH` (seq, sample position, device millis, n) + n int16, each sent as one HTTP chunk
// (src/whistle_lab/stream.py). The ring buffer is the stall buffer: if the link stalls for longer than the ring
// holds, the cursor jumps ahead and the position field shows the gap.
bool debug_running(uint32_t gen) { return ha::debug_active.load() && debug_gen.load() == gen; }

// Heartbeats still feed the HA sensors; triggers can't occur in debug mode.
void drain_jobs() {
  Job* j;
  while (xQueueReceive(jobs, &j, 0) == pdTRUE) {
    if (!j->trigger) ha::publish_telemetry(j->json);
    delete j;
  }
}

void pump(uint32_t ms) {
  for (const uint32_t t0 = millis(); millis() - t0 < ms;) {
    ha::poll();
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

void stream_session(uint32_t gen, uint32_t& cursor, uint32_t& seq) {
  static const String token_header = sizeof(DEVICE_TOKEN) > 1 ? String("X-Whistle-Token: " DEVICE_TOKEN "\r\n") : String();
  WiFiClientSecure tls;
  WiFiClient plain;
  WiFiClient* conn = connect_server(tls, plain, "debug stream");
  if (!conn) return;
  WiFiClient& c = *conn;
  c.printf("POST /api/device/stream HTTP/1.1\r\nHost: %s\r\n%sX-Whistle-Host: " HOSTNAME "\r\nX-Whistle-Fw: " GIT_REV
           "\r\nContent-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
           host_hdr, token_header.c_str());
  debug_streaming = true;
  Serial.printf("debug stream: connected, from sample %u\n", unsigned(cursor));

  constexpr int BODY = 8;  // fixed offset of the frame in `out`: keeps the int16 samples aligned
  alignas(4) static uint8_t out[BODY + 14 + 2 * DEBUG_FRAME_SAMPLES + 2];
  bool ok = true;
  while (ok && debug_running(gen)) {
    ha::poll();
    drain_jobs();
    const uint32_t avail = write_pos.load() - cursor;
    if (avail > ring_len - 2 * HOP) {  // the writer is about to lap us (or has)
      cursor = write_pos.load() - ring_len / 2;
      n_overrun++;
      continue;
    }
    if (avail < DEBUG_FRAME_SAMPLES / 2) {
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    const uint32_t m = std::min<uint32_t>(avail, DEBUG_FRAME_SAMPLES);
    const uint32_t len = 14 + 2 * m;
    char size_line[8];
    const int sl = snprintf(size_line, sizeof size_line, "%x\r\n", unsigned(len));
    uint8_t* const start = out + BODY - sl;
    memcpy(start, size_line, sl);
    uint8_t* const f = out + BODY;
    const uint32_t hdr[3] = {seq, cursor, millis()};
    const uint16_t n16 = m;
    memcpy(f, hdr, 12);
    memcpy(f + 12, &n16, 2);
    int16_t* pcm = reinterpret_cast<int16_t*>(f + 14);
    for (uint32_t i = 0; i < m; i++) pcm[i] = ring_at(cursor + i);
    if (!ring_intact(cursor)) {  // overwritten while copying: drop the frame, the position gap says so
      cursor = write_pos.load() - ring_len / 2;
      n_overrun++;
      continue;
    }
    memcpy(f + len, "\r\n", 2);
    const size_t total = f + len + 2 - start;
    ok = c.write(start, total) == total;
    cursor += m;
    seq++;
  }
  debug_streaming = false;
  if (ok) {
    c.print("0\r\n\r\n");
    String status, body;
    read_response(c, status, body, 3000);
    Serial.printf("debug stream: ended: %s %s\n", status.c_str(), body.c_str());
  } else {
    Serial.println("debug stream: write failed, reconnecting");
  }
  c.stop();
}

void stream_debug() {
  const uint32_t gen = debug_gen.load();
  uint32_t cursor = debug_start.load(), seq = 0;
  while (debug_running(gen)) {
    if (WiFi.status() == WL_CONNECTED) stream_session(gen, cursor, seq);
    if (debug_running(gen)) pump(1000);  // connect failed or the link dropped
  }
  debug_streaming = false;
  Serial.println("debug stream: stopped");
}

void net_task(void*) {
  Job* j;
  for (;;) {
    ha::poll();
    if (ha::debug_active.load()) {
      stream_debug();
      continue;
    }
    // Short wait so the MQTT client is pumped between jobs.
    if (xQueueReceive(jobs, &j, pdMS_TO_TICKS(50)) != pdTRUE) continue;
    if (!j->trigger) ha::publish_telemetry(j->json);
    else if (!HAVE_SERVER) {  // nothing to check it: forward the raw trigger
      ha::publish_detection({"trigger", NAN, j->level_db, j->n_notes, j->intervals, j->detected_ms, nullptr});
      delete j;
      continue;
    }
    bool ok = false;
    if (HAVE_SERVER && WiFi.status() == WL_CONNECTED) {
      if (j->trigger) uploading = true;
      const uint32_t t0 = millis();
      ok = j->trigger ? post_trigger(*j) : post_heartbeat(*j);
      Serial.printf("net: %s %s in %lu ms\n", j->trigger ? "trigger" : "heartbeat", ok ? "ok" : "FAILED",
                    millis() - t0);
      uploading = false;
    }
    if (j->trigger) {
      if (ok) n_sent++;
      else n_failed++;
      if (!ok) {
        led_result = -1;
        led_result_ms = millis();
      }
      // A published detection is beeped when HA's feedback arrives (or doesn't).
      if (!ok) beep_result = FAILED;
      else if (!ha::awaiting_feedback()) beep_result = led_result.load();
    }
    delete j;
  }
}

void enqueue(Job* j) {
  if (!HAVE_NET_TASK || xQueueSend(jobs, &j, 0) != pdTRUE) {
    if (HAVE_NET_TASK) n_dropped++;
    delete j;
  }
}

void net_init() {
  if (!HAVE_WIFI) {
    Serial.println("no WIFI_SSID (secrets.ini missing?): OTA and uploads disabled");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  ha::setup();

  ArduinoOTA.setHostname(HOSTNAME);  // also advertises <HOSTNAME>.local over mDNS
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([] {
    i2s_driver_uninstall(MIC_PORT);
    neopixelWrite(PIN_LED, 16, 0, 16);
    Serial.println("OTA start");
  });
  ArduinoOTA.onError([](ota_error_t e) {
    Serial.printf("OTA error %u, rebooting\n", e);  // mic was torn down in onStart
    delay(100);
    ESP.restart();
  });

  if (HAVE_SERVER) parse_server();
  if (HAVE_NET_TASK) {
    jobs = xQueueCreate(4, sizeof(Job*));
    xTaskCreatePinnedToCore(net_task, "net", 8192, nullptr, 1, nullptr, 0);  // loop() runs on core 1
  }
}

// Starts OTA (and mDNS) the first time the link comes up. Wi-Fi auto-reconnects after that.
void net_poll() {
  static bool started = false;
  if (!HAVE_WIFI) return;
  if (!started && WiFi.status() == WL_CONNECTED) {
    ArduinoOTA.begin();
    started = true;
    Serial.printf("wifi up: %s, OTA at %s.local, server %s\n", WiFi.localIP().toString().c_str(), HOSTNAME,
                  HAVE_SERVER ? DEBUG_SERVER : "(none)");
  }
  if (started) ArduinoOTA.handle();
}

// ---- detection ----
wd::Detector det;
float frame[NFFT];
int16_t hop_buf[HOP];
float dc = 0;
uint32_t det_start = 0;  // write_pos when the detector saw its first sample

// Heartbeat-window stats (loop() only).
struct Stats {
  uint32_t frames, short_reads, tonal, notes, events, triggers;
  uint32_t dsp_us_sum, dsp_us_max;
  uint32_t loop_gap_ms_max;  // longest time between mic reads; the DMA holds 128 ms
  double band_db_sum, ms_sum;  // band median dB, mean square of the signal
} st{};
uint32_t frames_total = 0, triggers_total = 0;
uint32_t last_dsp_us_avg = 0, last_dsp_us_max = 0;

uint32_t t_to_pos(double t) { return det_start + uint32_t(lround(t * SAMPLE_RATE)); }

void on_event(const wd::Event& e) {
  st.events++;
  const wd::Note& first = e.notes[0];
  const wd::Note& last = e.notes[e.n - 1];
  Serial.printf("event: %d notes, %.2f s\n", e.n_total, last.end() - first.start());
  if (e.n_total < MIN_NOTES) return;

  st.triggers++;
  triggers_total++;
  Job* j = new Job{true, 0, 0, String()};
  j->end = t_to_pos(last.end() + POST_S);
  j->start = t_to_pos(first.start() - PRE_S);
  const bool truncated = j->end - j->start > MAX_CLIP_S * SAMPLE_RATE;
  if (truncated) j->start = j->end - uint32_t(MAX_CLIP_S * SAMPLE_RATE);
  if (int32_t(j->start - det_start) < 0) j->start = det_start;

  String& s = j->json;
  s.reserve(1200);
  char b[160];
  s += "{\"host\":\"" HOSTNAME "\",\"fw\":\"" GIT_REV "\",\"notes\":[";
  int32_t note_frames = 0;
  for (int i = 0; i < e.n; i++) {
    const wd::Note& n = e.notes[i];
    note_frames += n.n_frames;
    snprintf(b, sizeof b, "%s[%.3f,%.3f,%.1f,%.1f,%.2f]", i ? "," : "", n.start() - first.start(), n.dur(),
             n.freq, n.prominence, n.concentration);
    s += b;
  }
  s += "],\"note_fields\":[\"start_s\",\"dur_s\",\"hz\",\"prominence_db\",\"concentration\"],\"intervals_st\":[";
  for (int i = 1; i < e.n; i++) {
    snprintf(b, sizeof b, "%s%.2f", i > 1 ? "," : "", 12 * log2f(e.notes[i].freq / e.notes[i - 1].freq));
    s += b;
  }
  const float span = last.end() - first.start();
  snprintf(b, sizeof b, "],\"n_notes\":%d,\"event_s\":%.3f,\"note_frame_fraction\":%.2f,", e.n_total, span,
           note_frames * float(HOP) / SAMPLE_RATE / span);
  s += b;
  snprintf(b, sizeof b, "\"clip_s\":%.3f,\"clip_pre_s\":%.2f,\"truncated\":%s,\"latency_s\":%.3f,",
           float(j->end - j->start) / SAMPLE_RATE, PRE_S, truncated ? "true" : "false",
           float(write_pos.load() - j->end) / SAMPLE_RATE);
  s += b;
  snprintf(b, sizeof b, "\"dsp_us_avg\":%u,\"dsp_us_max\":%u,\"heap\":%u,\"heap_min\":%u,\"rssi\":%d,",
           unsigned(last_dsp_us_avg), unsigned(last_dsp_us_max), ESP.getFreeHeap(), ESP.getMinFreeHeap(),
           int(WiFi.RSSI()));
  s += b;
  snprintf(b, sizeof b, "\"uptime_s\":%lu,\"trigger_n\":%u}", millis() / 1000, unsigned(triggers_total));
  s += b;
  j->n_notes = e.n_total;
  j->detected_ms = millis();
  j->intervals = "[";
  for (int i = 0; i < e.n; i++) {
    j->level_db = std::max(j->level_db, e.notes[i].prominence);
    if (i) {
      snprintf(b, sizeof b, "%s%.2f", i > 1 ? "," : "", 12 * log2f(e.notes[i].freq / e.notes[i - 1].freq));
      j->intervals += b;
    }
  }
  j->intervals += "]";
  Serial.printf("trigger #%u: %s\n", unsigned(triggers_total), s.c_str());
  enqueue(j);
}

void heartbeat() {
  static uint32_t last = 0;
  const uint32_t now = millis();
  if (now - last < HEARTBEAT_MS) return;
  last = now;
  const uint32_t f = std::max<uint32_t>(st.frames, 1);
  last_dsp_us_avg = st.dsp_us_sum / f;
  last_dsp_us_max = st.dsp_us_max;
  Job* j = new Job{false, 0, 0, String()};
  char b[704];
  snprintf(b, sizeof b,
           "{\"host\":\"" HOSTNAME "\",\"fw\":\"" GIT_REV "\",\"uptime_s\":%u,\"frames\":%u,\"short_reads\":%u,"
           "\"tonal_frac\":%.3f,\"notes\":%u,\"events\":%u,\"triggers\":%u,\"sent\":%u,\"failed\":%u,"
           "\"overrun\":%u,\"dropped\":%u,\"level_dbfs\":%.1f,\"band_median_db\":%.1f,\"dsp_us_avg\":%u,"
           "\"dsp_us_max\":%u,\"loop_gap_ms_max\":%u,\"heap\":%u,\"heap_min\":%u,\"rssi\":%d,\"ring_s\":%.2f}",
           now / 1000, unsigned(st.frames), unsigned(st.short_reads), st.tonal / float(f), unsigned(st.notes),
           unsigned(st.events), unsigned(st.triggers), unsigned(n_sent.load()), unsigned(n_failed.load()),
           unsigned(n_overrun.load()), unsigned(n_dropped.load()), 10 * log10(st.ms_sum / f + 1e-12),
           st.band_db_sum / f, unsigned(last_dsp_us_avg), unsigned(last_dsp_us_max),
           unsigned(st.loop_gap_ms_max), ESP.getFreeHeap(), ESP.getMinFreeHeap(), int(WiFi.RSSI()),
           float(ring_len) / SAMPLE_RATE);
  j->json = b;
  Serial.printf("heartbeat %s\n", b);
  enqueue(j);
  st = Stats{};
}

uint32_t identify_until_ms = 0;

// HA's Identify button: three equal beeps, distinct from the verdict tones, and the LED white for 5 s.
void identify() {
  static const int ID[] = {1800, 1800, 1800};
  identify_until_ms = millis() + 5000;
  neopixelWrite(PIN_LED, 40, 40, 40);
  audio_ok = beep(ID, 3);
  last_read = millis();
  Serial.printf("identify: beeped%s\n", audio_ok ? "" : ", mic did not restart");
}

// Debug mode on/off, from HA's switch or the timeout. Runs in loop(), which owns the detector.
void debug_update() {
  const int req = ha::debug_request.exchange(0);
  const bool on = ha::debug_active.load();
  if (req > 0 && !HAVE_SERVER) Serial.println("debug mode: no debug_server configured, ignored");
  else if (req > 0) {
    debug_deadline = millis() + DEBUG_TIMEOUT_MS;
    if (!on) {
      debug_start = write_pos.load();
      debug_gen++;
      ha::debug_active = true;
      Serial.printf("debug mode ON for %u s\n", unsigned(DEBUG_TIMEOUT_MS / 1000));
    }
  } else if (on && (req < 0 || int32_t(millis() - debug_deadline.load()) >= 0)) {
    ha::debug_active = false;
    det.~Detector();  // placement-new: no large temporary on this stack
    new (&det) wd::Detector();
    memset(frame, 0, sizeof frame);
    det_start = write_pos.load();
    Serial.printf("debug mode OFF (%s)\n", req < 0 ? "switched off" : "timeout");
  }
}

void led_update(float level_dbfs) {
  const uint32_t now = millis();
  const int res = led_result.load();
  if (digitalRead(PIN_BUTTON) == LOW || now < identify_until_ms) neopixelWrite(PIN_LED, 40, 40, 40);
  else if (ha::debug_active.load()) {  // cyan while streaming, blinking while there is no connection
    const bool lit = debug_streaming || now / 500 % 2;
    neopixelWrite(PIN_LED, 0, lit ? 48 : 0, lit ? 48 : 0);
  }
  else if (res && now - led_result_ms.load() < 1500) neopixelWrite(PIN_LED, res < 0 ? 64 : 0, res > 0 ? 64 : 0, 0);
  else if (uploading) neopixelWrite(PIN_LED, 40, 30, 0);
  else {
    // -70 dBFS -> off, -20 dBFS -> full blue.
    const float frac = constrain((level_dbfs + 70.f) / 50.f, 0.f, 1.f);
    neopixelWrite(PIN_LED, 0, 0, uint8_t(frac * 48));
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUTTON, INPUT);
  neopixelWrite(PIN_LED, 0, 0, 16);
  Serial.printf("whistle-fw %s: boot, heap %u free, %u max block\n", GIT_REV, ESP.getFreeHeap(),
                ESP.getMaxAllocHeap());
  // Wi-Fi and OTA first, so they get their memory and stay up whatever happens to the audio path.
  net_init();
  ring_init(server_tls ? MIN_FREE_AFTER_TLS : MIN_FREE_AFTER);
  audio_ok = ring_len && mic_init();
  Serial.printf("whistle-fw %s: %s, ring %.2f s (%d blocks), heap %u free, %u max block\n", GIT_REV,
                audio_ok ? "mic up" : "AUDIO DISABLED", float(ring_len) / SAMPLE_RATE, n_blocks,
                ESP.getFreeHeap(), ESP.getMaxAllocHeap());
  det_start = write_pos.load();
  last_read = millis();
}

void loop() {
  net_poll();
  debug_update();
  if (ha::identify_requested.exchange(false) && !ha::debug_active.load()) identify();
  if (!audio_ok) {  // keep OTA alive and show the fault
    neopixelWrite(PIN_LED, millis() / 500 % 2 ? 64 : 0, 0, 0);
    delay(10);
    return;
  }
  switch (ha::feedback.exchange(ha::NONE)) {
    case ha::OK: beep_result = DETECTED; break;
    case ha::NO_ACTION: beep_result = NO_ACTION; break;
    case ha::DUP: Serial.println("feedback: dup (another room heard it louder)"); break;  // stay quiet
    case ha::TIMEOUT: beep_result = FAILED; break;
  }
  if (const int r = beep_result.exchange(NONE)) {
    led_result_ms = millis();
    neopixelWrite(PIN_LED, r == DETECTED ? 0 : 64, r == DETECTED ? 64 : r == NO_ACTION ? 32 : 0, 0);
    audio_ok = beep_result_tones(r);
    Serial.printf("beep: %s%s\n", r == DETECTED ? "success" : r == NO_ACTION ? "no action" : "failure",
                  audio_ok ? "" : ", mic did not restart");
    led_result_ms = millis();  // hold the colour for its full time after the beeps
    last_read = millis();
    if (!audio_ok) return;
  }
  size_t got = 0;
  i2s_read(MIC_PORT, hop_buf, sizeof(hop_buf), &got, portMAX_DELAY);
  const uint32_t gap = millis() - last_read;
  last_read += gap;
  st.loop_gap_ms_max = std::max(st.loop_gap_ms_max, gap);
  if (gap > 100) Serial.printf("loop stalled %u ms\n", unsigned(gap));
  if (got != sizeof(hop_buf)) {
    st.short_reads++;
    return;
  }

  // DC blocker (the PDM path has an offset), then into the ring and the detector's sliding frame.
  const uint32_t pos = write_pos.load();
  double ms = 0;
  memmove(frame, frame + HOP, (NFFT - HOP) * sizeof(float));
  for (int i = 0; i < HOP; i++) {
    dc += 0.004f * (hop_buf[i] - dc);  // ~10 Hz corner
    const int16_t y = int16_t(constrain(lroundf(hop_buf[i] - dc), -32768L, 32767L));
    ring_at(pos + i) = y;
    frame[NFFT - HOP + i] = y / 32768.f;
    ms += double(y) * y;
  }
  write_pos.store(pos + HOP);
  const float ms_frame = ms / HOP / (32768.0 * 32768.0);
  const float level_dbfs = 10 * log10f(ms_frame + 1e-12f);

  // The first frame needs NFFT samples, so frame i covers samples [i*HOP, i*HOP + NFFT) after det_start.
  if (!ha::debug_active.load() && pos + HOP - det_start >= uint32_t(NFFT)) {
    const uint32_t t0 = micros();
    const wd::Event* e = det.push(frame);
    const uint32_t dt = micros() - t0;
    frames_total++;
    st.frames++;
    st.dsp_us_sum += dt;
    st.dsp_us_max = std::max(st.dsp_us_max, dt);
    st.ms_sum += ms_frame;
    st.band_db_sum += det.features().band_median_db;
    if (det.features().tonal) st.tonal++;
    if (det.note_emitted()) {
      const wd::Note& n = det.emitted_note();
      st.notes++;
      Serial.printf("note: %.0f Hz, %.2f s, prom %.1f dB, conc %.2f\n", n.freq, n.dur(), n.prominence,
                    n.concentration);
    }
    if (e) on_event(*e);
  }

  heartbeat();
  led_update(level_dbfs);
}
