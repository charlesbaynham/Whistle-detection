---
name: flash-board
description: Invoke whenever an ESP32 board (M5Stack ATOM Echo) is flashed with firmware, over USB or OTA, or when a flash fails.
---

# Flashing the ATOM Echo

Status: written from docs before first hardware flash. After the first real USB and OTA flash,
correct anything below that turned out wrong and delete this line.

## Where it can run

- **Cloud session:** can build (`cd firmware && pio run`) but cannot reach the board (no USB, no LAN).
  Either ask the user to run the upload, or start a Remote Control session on a machine that has the
  board on USB or is on the same LAN, with PlatformIO installed (`pipx install platformio` or `uv tool install platformio`).
- **User's machine / Remote Control:** everything below.

## Prerequisites

- `firmware/secrets.ini` exists (copy `secrets.ini.example`). If it's missing the build succeeds but
  Wi-Fi/OTA are compiled out, and the next update will need USB again. Check before a USB flash.
- Linux USB access: user in `dialout` (or `uucp`), and PlatformIO's udev rules installed
  (`curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core/develop/platformio/assets/system/99-platformio-udev.rules | sudo tee /etc/udev/rules.d/99-platformio-udev.rules`).
- Linux mDNS for `<hostname>.local`: avahi + nss-mdns. Otherwise use the IP.

## USB (first flash, partition-table change, or recovery)

```sh
cd firmware
pio device list                      # ATOM shows as /dev/ttyUSB* or /dev/ttyACM* (macOS: /dev/cu.usbserial-*)
pio run -e atom-echo -t upload       # auto-detects the port; add --upload-port /dev/ttyUSB0 if several
pio device monitor -e atom-echo      # expect "whistle-fw: mic up", then "wifi up: <ip>, OTA at <host>.local"
```

- Upload speed is 750000; the ATOM's serial bridge fails above that. If it still fails, try
  `--upload-speed 115200` (edit `upload_speed` only if it's consistently needed).
- If esptool can't enter the bootloader ("Wrong boot mode" / "Failed to connect"): unplug, hold G0
  (bottom header) to GND, plug in, release, retry. Also try another cable; many USB-C cables are power-only.
- Note the IP from the monitor output in case mDNS doesn't resolve later.

## OTA (normal updates)

```sh
cd firmware
pio run -e atom-echo-ota -t upload                       # targets <hostname>.local from secrets.ini
pio run -e atom-echo-ota -t upload --upload-port 192.168.x.y   # if mDNS fails
```

- The board must be running OTA-capable firmware with the same `ota_password`.
- espota makes the device connect *back* to the host on a random TCP port: a host firewall blocking
  inbound connections gives "No response from device". Fix by allowing it, or pin a port with
  `--host_port=<port>` in `upload_flags` and open that one.
- Host and board must be on the same subnet for mDNS; across VLANs use the IP.
- During OTA the LED is magenta and the mic is stopped; a failed OTA reboots into the old image.

## Afterwards

Check serial output (`pio device monitor`, USB only) or behaviour (LED responds to whistling)
before calling the flash a success. When a step here was wrong or missing, fix this file.
