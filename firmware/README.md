# Firmware: M5Stack ATOM Echo

PlatformIO, Arduino framework (arduino-esp32 2.x on `espressif32@6.9.0`). Currently a smoke test:
reads the PDM mic at 16 kHz, shows the level on the LED and prints it at 4 Hz, joins Wi-Fi and
accepts ArduinoOTA uploads.

LED: blue at boot, green brightness = mic level, red while the button is held, magenta during OTA.

## Pins

| Function        | GPIO | Notes                                         |
|-----------------|------|-----------------------------------------------|
| SK6812 RGB LED  | 27   | `neopixelWrite()`                             |
| Button          | 39   | active low, input only                        |
| PDM mic CLK     | 33   | SPM1423; shared with speaker LRCK             |
| PDM mic DATA    | 23   |                                               |
| Speaker BCLK    | 19   | NS4168 I2S amp                                |
| Speaker DATA    | 22   |                                               |

Mic and speaker share GPIO33, so they can't run at the same time; reconfigure I2S0 to switch.

## Build and flash

```sh
cp secrets.ini.example secrets.ini   # Wi-Fi, OTA password, hostname; gitignored
pio run                              # build only
pio run -e atom-echo -t upload       # first time: USB
pio run -e atom-echo-ota -t upload   # afterwards: Wi-Fi, to <hostname>.local
pio device monitor
```

Without `secrets.ini` the firmware still builds (CI does this) but Wi-Fi and OTA are compiled out.
The partition table (`min_spiffs`, two 1.9 MB app slots) can only change over USB.

Full procedure and troubleshooting: `.claude/skills/flash-board/SKILL.md`.

## OTA choice

ArduinoOTA (espota, push from a machine on the LAN) for now: no infrastructure, PlatformIO does it
natively. Once there are several room nodes, switch to HTTP pull (`HTTPUpdate` against a firmware
image + version file on the local HTTPS server) so CI or one command updates them all.
