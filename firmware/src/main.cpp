// Toolchain smoke test for the ATOM Echo: read the PDM mic and show the level
// on the RGB LED and the serial port. Joins Wi-Fi and accepts ArduinoOTA uploads.
#include <Arduino.h>
#include <ArduinoOTA.h>
#include <WiFi.h>
#include <driver/i2s.h>
#include <math.h>

namespace {
constexpr int PIN_LED = 27;       // SK6812
constexpr int PIN_BUTTON = 39;    // active low, external pull-up
constexpr int PIN_MIC_CLK = 33;   // shared with speaker LRCK: mic and speaker can't run together
constexpr int PIN_MIC_DATA = 23;
constexpr i2s_port_t MIC_PORT = I2S_NUM_0;  // PDM RX only exists on I2S0
constexpr uint32_t SAMPLE_RATE = 16000;
constexpr size_t BLOCK = 512;  // 32 ms

int16_t buf[BLOCK];

void mic_init() {
  const i2s_config_t cfg = {
      .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
      .sample_rate = SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT,
      .communication_format = I2S_COMM_FORMAT_STAND_I2S,
      .intr_alloc_flags = 0,
      .dma_buf_count = 4,
      .dma_buf_len = BLOCK,
  };
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = I2S_PIN_NO_CHANGE;
  pins.ws_io_num = PIN_MIC_CLK;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_DATA;
  ESP_ERROR_CHECK(i2s_driver_install(MIC_PORT, &cfg, 0, nullptr));
  ESP_ERROR_CHECK(i2s_set_pin(MIC_PORT, &pins));
}

void net_init() {
  if (sizeof(WIFI_SSID) <= 1) {
    Serial.println("no WIFI_SSID (secrets.ini missing?): OTA disabled");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

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
}

// Starts OTA (and mDNS) the first time the link comes up. Wi-Fi auto-reconnects after that.
void net_poll() {
  static bool started = false;
  if (sizeof(WIFI_SSID) <= 1) return;
  if (!started && WiFi.status() == WL_CONNECTED) {
    ArduinoOTA.begin();
    started = true;
    Serial.printf("wifi up: %s, OTA at %s.local\n", WiFi.localIP().toString().c_str(), HOSTNAME);
  }
  if (started) ArduinoOTA.handle();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUTTON, INPUT);
  neopixelWrite(PIN_LED, 0, 0, 16);
  mic_init();
  net_init();
  Serial.println("whistle-fw: mic up");
}

void loop() {
  net_poll();
  size_t n = 0;
  i2s_read(MIC_PORT, buf, sizeof(buf), &n, portMAX_DELAY);
  n /= sizeof(int16_t);
  if (n == 0) return;

  // Mean-subtracted RMS in dBFS.
  int64_t sum = 0;
  for (size_t i = 0; i < n; i++) sum += buf[i];
  const float mean = float(sum) / n;
  double ss = 0;
  for (size_t i = 0; i < n; i++) {
    const float x = buf[i] - mean;
    ss += x * x;
  }
  const float dbfs = 10.f * log10f(float(ss / n) + 1e-9f) - 20.f * log10f(32768.f);

  // -70 dBFS -> off, -20 dBFS -> full green. Button turns it red.
  const float frac = constrain((dbfs + 70.f) / 50.f, 0.f, 1.f);
  const uint8_t v = uint8_t(frac * 64);
  if (digitalRead(PIN_BUTTON) == LOW) neopixelWrite(PIN_LED, v, 0, 0);
  else neopixelWrite(PIN_LED, 0, v, 0);

  static uint32_t last = 0;
  if (millis() - last >= 250) {
    last = millis();
    Serial.printf("level %.1f dBFS\n", dbfs);
  }
}
