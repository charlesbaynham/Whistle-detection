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
void publish_telemetry(const String& json);

// Set by a `cmd`, consumed by loop() (which owns I2S0).
extern std::atomic<bool> identify_requested;

}  // namespace ha
