// MQTT link to Home Assistant (option A: discovery only). Protocol: docs/mqtt.md.
// Everything except the setters runs on the network task; PubSubClient is not thread-safe.
#pragma once
#include <Arduino.h>

#include <atomic>

namespace ha {

constexpr bool ENABLED = sizeof(WIFI_SSID) > 1 && sizeof(MQTT_HOST) > 1;

struct Detection {
  const char* event_type;  // server label, "rejected", or "trigger" when there is no server
  float score;             // server rho; NAN if none
  float level_db;          // loudest note's prominence over the band median, dB
  int n_notes;
  String intervals;        // JSON array text, semitones
  uint32_t detected_ms;    // millis() at the end of the event
  const char* veto;        // server veto name, or nullptr
};

void setup();                    // after WiFi.mode(); reads the MAC
void poll();                     // network task: (re)connect, pump the client
bool connected();
void publish_detection(const Detection& d);
bool awaiting_feedback();        // a published detection has had no `feedback` yet
void publish_telemetry(const String& json);

// HA's verdict on our last detection (docs/mqtt.md, `feedback`). TIMEOUT: none within FEEDBACK_MS.
enum Feedback : int { NONE, OK, NO_ACTION, DUP, TIMEOUT };
constexpr uint32_t FEEDBACK_MS = 2500;

// Set by the network task, consumed by loop() (which owns I2S0).
extern std::atomic<bool> identify_requested;
extern std::atomic<int> feedback;

}  // namespace ha
