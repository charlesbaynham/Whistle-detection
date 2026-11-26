---
name: flash-board
description: Invoke whenever an ESP32 board (M5Stack ATOM Echo) is flashed with firmware, over USB or OTA, or when a flash fails.
---

# Flashing the ATOM Echo

## Where it can run

- **Cloud session:** can build (`cd firmware && pio run`) but cannot reach the board (no USB, no LAN).
  Either ask the user to run the upload, or start a Remote Control session on a machine that has the
  board on USB or is on the same LAN, with PlatformIO installed (`pipx install platformio` or `uv tool install platformio`).
- **User's machine / Remote Control:** everything below.

## Prerequisites

- `firmware/secrets.ini` exists (copy `secrets.ini.example`). If it's missing the build succeeds but
  Wi-Fi/OTA are compiled out, and the next update will need USB again. Check before a USB flash.
  It is gitignored, so a fresh worktree lacks it. On the user's laptop an encrypted copy is at
  `~/whistle-echo-backup/secrets.ini.asc`, readable with the agent GPG key (`get_gpg_private_key`, via
  the `house-absolute:fetching-secrets` skill; import into a temporary `--homedir`, then decrypt with
  `--output firmware/secrets.ini`). Re-encrypt it to that key whenever `secrets.ini` changes.
- Linux USB access: user in `dialout` (or `uucp`). That is enough on Ubuntu; if the port is still
  refused, install PlatformIO's udev rules
  (`curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core/develop/platformio/assets/system/99-platformio-udev.rules | sudo tee /etc/udev/rules.d/99-platformio-udev.rules`).
- Linux mDNS for `<hostname>.local`: avahi + nss-mdns. A Nix-installed PlatformIO can't use it
  (see OTA below).
- On the home LAN the board `whistle-echo` has a DHCP reservation (10.0.1.52, in the
  `houseabsolute-openwrt` router config) and DNS name `whistle-echo.ha`. Give new boards one too.

## New board: identify and back up (read-only)

Run `pio run` first, which downloads esptool. The ATOM Echo's bridge enumerates as `0403:6001`
"Hades2001 M5stack".

```sh
cd firmware
# expect ESP32-PICO-D4, 4MB
pio pkg exec -p tool-esptoolpy -- esptool.py --port /dev/ttyUSB0 flash_id
# ~70 s; keep the backup outside the repo (it may hold credentials)
pio pkg exec -p tool-esptoolpy -- esptool.py --port /dev/ttyUSB0 --baud 750000 read_flash 0 0x400000 <backup>.bin
```

Stock boards ship with ESPHome (`m5stack-atom-echo-wake-word-voice-assistant`), which starts an AP
`m5stack-atom-echo-<mac>` until provisioned. Its partition table differs from ours, so erase before
the first upload: `pio run -e atom-echo -t erase`. To restore, run the same esptool command with
`write_flash 0 <backup>.bin`.

## USB (first flash, partition-table change, or recovery)

```sh
cd firmware
pio device list                      # ATOM shows as /dev/ttyUSB* or /dev/ttyACM* (macOS: /dev/cu.usbserial-*)
pio run -e atom-echo -t upload       # auto-detects the port; add --upload-port /dev/ttyUSB0 if several
pio device monitor -e atom-echo      # expect "whistle-fw <rev>: mic up, ring 5.12 s ...", then "wifi up: <ip>, OTA at <host>.local, server ..."
```

- `pio device monitor` is interactive. To capture a boot log from an agent session:
  `uv run ../.claude/skills/flash-board/serial_log.py /dev/ttyUSB0 20`.
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
# Nix PlatformIO ("Host <hostname>.local Not Found"): on the home LAN use the router's DNS name
pio run -e atom-echo-ota -t upload --upload-port whistle-echo.ha
# ... elsewhere, resolve with the system's resolver
pio run -e atom-echo-ota -t upload --upload-port "$(getent ahostsv4 whistle-echo.local | awk '{print $1; exit}')"
```

Success is `Result: OK`; if USB is attached, the serial log shows `OTA start` and then a reboot.
A one-off `Authenticating...FAIL` has succeeded on retry: retry before suspecting the password.

- The board must be running OTA-capable firmware with the same `ota_password`.
- `Sending invitation .......... No response from the ESP`: first `ping whistle-echo.ha`. No reply
  (and `ip neigh` shows FAILED) means the board is off or off Wi-Fi, not a firewall problem.
- espota makes the device connect *back* to the host on a random TCP port: a host firewall blocking
  inbound connections gives "No response from device". Fix by allowing it, or pin a port with
  `--host_port=<port>` in `upload_flags` and open that one.
- Host and board must be on the same subnet for mDNS; across VLANs use the IP.
- During OTA the LED is magenta and the mic is stopped; a failed OTA reboots into the old image.

## Afterwards

Check serial output (`pio device monitor`, USB only) or behaviour (LED responds to whistling)
before calling the flash a success. When a step here was wrong or missing, fix this file.
