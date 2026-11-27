// MQTT link to Home Assistant, option A of docs/ha-integration-plan.md: the node announces itself with MQTT
// discovery (an event entity, an Identify button, diagnostic sensors) and publishes one event per checked
// trigger. Wire format: docs/mqtt.md. Runs on the network task.
#include "ha_mqtt.h"

#include <PubSubClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_mac.h>
#include <time.h>

namespace ha {

std::atomic<bool> identify_requested{false};

namespace {
constexpr int SCHEMA = 1;
constexpr int MAX_TYPES = 16, MAX_TYPE_LEN = 32;
constexpr uint32_t RETRY_MS = 5000;
constexpr uint16_t BUF = 1536;  // largest message is the event discovery config with 16 types

WiFiClient tcp;
PubSubClient mqtt(tcp);
char node[7];            // last 3 MAC bytes, hex
char base[24];           // whistle/<node>
char uid[16];            // whistle_<node>
char t_status[40], t_cmd[40], t_detection[48], t_telemetry[48], t_info[40];

// Event types HA has been told about. HA drops events whose type isn't in the discovery config, so a new
// server label is added (and persisted) before its first event is published.
char types[MAX_TYPES][MAX_TYPE_LEN + 1];
int n_types = 0;
Preferences prefs;
uint32_t det_seq = 0, boot_nonce = 0;

void load_types() {
  prefs.begin("ha", false);
  String s = prefs.getString("types", "");
  n_types = 0;
  strlcpy(types[n_types++], "rejected", sizeof types[0]);
  strlcpy(types[n_types++], "trigger", sizeof types[0]);
  for (int a = 0; a < int(s.length()) && n_types < MAX_TYPES;) {
    int b = s.indexOf(',', a);
    if (b < 0) b = s.length();
    if (b > a) strlcpy(types[n_types++], s.substring(a, b).c_str(), sizeof types[0]);
    a = b + 1;
  }
}

void save_types() {
  String s;
  for (int i = 2; i < n_types; i++) s += String(i > 2 ? "," : "") + types[i];
  prefs.putString("types", s);
}

int find_type(const char* t) {
  for (int i = 0; i < n_types; i++)
    if (!strcmp(types[i], t)) return i;
  return -1;
}

// Lower-case, [a-z0-9_] only, so labels are safe as HA event types and JSON strings.
void sanitize(const char* in, char* out) {
  int n = 0;
  for (; *in && n < MAX_TYPE_LEN; in++) {
    const char c = tolower(*in);
    out[n++] = isalnum(uint8_t(c)) ? c : '_';
  }
  out[n] = 0;
  if (!n) strcpy(out, "unlabelled");
}

// Copies `in` into a JSON string body, dropping characters that would need escaping.
String json_safe(const char* in) {
  String out;
  for (; *in; in++)
    if (*in != '"' && *in != '\\' && uint8_t(*in) >= 0x20) out += *in;
  return out;
}

String device_json() {
  return String("{\"identifiers\":[\"") + uid + "\"],\"name\":\"" HOSTNAME
         "\",\"manufacturer\":\"M5Stack\",\"model\":\"ATOM Echo\",\"sw_version\":\"" GIT_REV "\"}";
}

// Fields every discovery config shares. default_entity_id pins the entity id to the node id: HA would
// otherwise derive it from the device name (the hostname), which collides between nodes.
String common(const char* domain, const char* key, const char* name) {
  return String("\"name\":\"") + name + "\",\"unique_id\":\"" + uid + "_" + key + "\",\"default_entity_id\":\"" +
         domain + "." + uid + "_" + key + "\",\"availability_topic\":\"" + t_status + "\",\"device\":" + device_json();
}

bool pub(const char* topic, const String& payload, bool retain) {
  const bool ok = mqtt.publish(topic, payload.c_str(), retain);
  if (!ok) Serial.printf("mqtt: publish %s (%u B) failed\n", topic, unsigned(payload.length()));
  return ok;
}

void publish_event_config() {
  String types_json;
  for (int i = 0; i < n_types; i++) types_json += String(i ? ",\"" : "\"") + types[i] + "\"";
  pub((String("homeassistant/event/") + uid + "/whistle/config").c_str(),
      "{" + common("event", "whistle", "Whistle") + ",\"state_topic\":\"" + t_detection + "\",\"event_types\":[" +
          types_json + "],\"icon\":\"mdi:whistle\"}",
      true);
}

void publish_sensor_config(const char* key, const char* name, const char* unit, const char* dev_class) {
  String s = "{" + common("sensor", key, name) + ",\"state_topic\":\"" + t_telemetry + "\",\"value_template\":\"{{ value_json." +
             key + " }}\",\"entity_category\":\"diagnostic\",\"state_class\":\"measurement\",\"expire_after\":45";
  if (*unit) s += String(",\"unit_of_measurement\":\"") + unit + "\"";
  if (*dev_class) s += String(",\"device_class\":\"") + dev_class + "\"";
  s += "}";
  pub((String("homeassistant/sensor/") + uid + "/" + key + "/config").c_str(), s, true);
}

void publish_discovery() {
  publish_event_config();
  pub((String("homeassistant/button/") + uid + "/identify/config").c_str(),
      "{" + common("button", "identify", "Identify") + ",\"command_topic\":\"" + t_cmd +
          "\",\"payload_press\":\"{\\\"cmd\\\":\\\"identify\\\"}\",\"device_class\":\"identify\","
          "\"entity_category\":\"config\"}",
      true);
  publish_sensor_config("rssi", "Wi-Fi signal", "dBm", "signal_strength");
  publish_sensor_config("heap", "Free heap", "B", "data_size");
  publish_sensor_config("uptime_s", "Uptime", "s", "duration");
  publish_sensor_config("level_dbfs", "Mic level", "dBFS", "");
  publish_sensor_config("band_median_db", "Noise floor", "dB", "");
  pub(t_info,
      String("{\"v\":1,\"node\":\"") + node + "\",\"host\":\"" HOSTNAME "\",\"fw\":\"" GIT_REV "\",\"ip\":\"" +
          WiFi.localIP().toString() + "\"}",
      true);
}

void on_message(char* topic, uint8_t* payload, unsigned len) {
  String msg;
  msg.reserve(len);
  for (unsigned i = 0; i < len; i++) msg += char(payload[i]);
  Serial.printf("mqtt: %s %s\n", topic, msg.c_str());
  if (!strcmp(topic, "homeassistant/status")) {
    if (msg == "online") publish_discovery();  // HA restarted: it may have lost non-retained state
    return;
  }
  if (!strcmp(topic, t_cmd)) {
    if (msg.indexOf("\"identify\"") >= 0 || msg == "identify") identify_requested = true;
    else if (msg.indexOf("\"reboot\"") >= 0 || msg == "reboot") {
      mqtt.publish(t_status, "offline", true);
      mqtt.disconnect();
      delay(200);
      ESP.restart();
    } else Serial.println("mqtt: unknown cmd");
  }
}

void try_connect() {
  static uint32_t last = 0;
  if (last && millis() - last < RETRY_MS) return;
  last = millis();
  const bool auth = sizeof(MQTT_USER) > 1;
  const bool ok = mqtt.connect(uid, auth ? MQTT_USER : nullptr, auth ? MQTT_PASSWORD : nullptr, t_status, 1, true,
                               "offline");
  if (!ok) {
    Serial.printf("mqtt: connect to %s:%d failed, state %d\n", MQTT_HOST, MQTT_PORT, mqtt.state());
    return;
  }
  Serial.printf("mqtt: connected to %s:%d as %s\n", MQTT_HOST, MQTT_PORT, uid);
  mqtt.publish(t_status, "online", true);
  mqtt.subscribe(t_cmd, 1);
  mqtt.subscribe("homeassistant/status", 1);
  publish_discovery();
}
}  // namespace

void setup() {
  if (!ENABLED) return;
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(node, sizeof node, "%02x%02x%02x", mac[3], mac[4], mac[5]);
  snprintf(uid, sizeof uid, "whistle_%s", node);
  snprintf(base, sizeof base, "whistle/%s", node);
  snprintf(t_status, sizeof t_status, "%s/status", base);
  snprintf(t_info, sizeof t_info, "%s/info", base);
  snprintf(t_cmd, sizeof t_cmd, "%s/cmd", base);
  snprintf(t_detection, sizeof t_detection, "%s/detection", base);
  snprintf(t_telemetry, sizeof t_telemetry, "%s/telemetry", base);
  boot_nonce = esp_random() & 0xffff;
  load_types();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(BUF);
  mqtt.setKeepAlive(30);       // the network task can block for ~10 s in an HTTP upload
  mqtt.setSocketTimeout(5);
  mqtt.setCallback(on_message);
  configTime(0, 0, "pool.ntp.org");  // for detection timestamps
  Serial.printf("mqtt: node %s, broker %s:%d\n", node, MQTT_HOST, MQTT_PORT);
}

void poll() {
  if (!ENABLED || WiFi.status() != WL_CONNECTED) return;
  if (!mqtt.connected()) try_connect();
  mqtt.loop();
}

bool connected() { return ENABLED && mqtt.connected(); }

void publish_detection(const Detection& d) {
  if (!connected()) {
    // Not queued: a stale "lights off" replayed after a reconnect is worse than none.
    Serial.println("mqtt: not connected, detection dropped");
    return;
  }
  char type[MAX_TYPE_LEN + 1];
  sanitize(d.event_type, type);
  if (find_type(type) < 0) {
    if (n_types < MAX_TYPES) {
      strlcpy(types[n_types++], type, sizeof types[0]);
      save_types();
    } else strcpy(type, "trigger");  // table full; the label is still in `label`
    publish_event_config();
    // Give HA time to apply the new config before the event that uses it.
    for (const uint32_t t0 = millis(); millis() - t0 < 500;) {
      mqtt.loop();
      vTaskDelay(pdMS_TO_TICKS(20));
    }
  }
  const time_t now = time(nullptr);
  const uint32_t age_ms = millis() - d.detected_ms;
  char b[256];
  snprintf(b, sizeof b,
           "{\"v\":%d,\"event_type\":\"%s\",\"id\":\"%04x-%u\",\"t\":%.3f,\"age_ms\":%u,\"level\":%.1f,"
           "\"n_notes\":%d,\"score\":",
           SCHEMA, type, unsigned(boot_nonce), unsigned(++det_seq),
           now > 1600000000 ? double(now) - age_ms / 1000.0 : 0.0, unsigned(age_ms), d.level_db, d.n_notes);
  String s = b;
  s += isnan(d.score) ? String("null") : String(d.score, 1);
  s += ",\"label\":\"";
  s += json_safe(d.event_type);  // the server's label as given, for display
  s += "\",\"veto\":";
  s += d.veto ? String("\"") + json_safe(d.veto) + "\"" : String("null");
  s += ",\"intervals\":" + d.intervals + "}";
  pub(t_detection, s, false);
}

void publish_telemetry(const String& json) {
  if (connected()) pub(t_telemetry, json, false);
}

}  // namespace ha
