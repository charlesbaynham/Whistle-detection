---
name: flash-board
description: Invoke whenever an ESP32 board (M5Stack ATOM Echo) is flashed with firmware, over USB or OTA, or when a flash fails.
---

# Flashing the ATOM Echo

## Cloud session: one command

```sh
scripts/flash.sh                 # build HEAD on the flasher, OTA it, verify the reboot; ~1-4 min
scripts/flash.sh --ref <ref>     # or --worktree for uncommitted changes (rev shows as <sha>+)
```

It ships `firmware/` to the flasher container (CT 138, `10.0.2.14`, declared in homelab-infra `tofu/flasher`), writes
`secrets.ini` there for the build only, runs `pio run -e atom-echo-ota -t upload`, then polls the debug server's
`device-heartbeats.jsonl` (via the Proxmox host) for a heartbeat with `fw == <rev>` and a fresh `uptime_s`. Output is a few
lines ending in `FLASH OK ...` or `FLASH FAIL (<code>): ...`. Exit codes: 0 ok, 2 usage, 10 secrets missing, 11 flasher or
tailnet unreachable, 12 build/upload failed (log tail printed), 13 uploaded but reboot not verified.

Only the Secret Server fetches need an agent (an MCP call cannot be made from a script). First time in a session (exit 10):

1. `scripts/flash.sh prep` mints a session GPG key and prints its public half.
2. In one parallel batch call `get_lab_ssh_key`, `get_whistle_secrets_ini` and `get_ts_authkey` with that public key.
   `ts` is only needed if the tailnet is not already up.
3. Pipe each result's `ciphertext` value into `scripts/flash.sh save ssh|ini|ts` (stdin; the JSON string or armored block).
4. Run `scripts/flash.sh`.

State lives in `/tmp/flash-secrets` (override `FLASH_DIR`); later flashes in the same session need only step 4. The script joins
the tailnet itself (`ts_connect.sh`; `tailscaled` dies when the container sleeps, which it handles). Override with
`FLASHER_HOST`, `BOARD_IP`, `LOG_HOST`/`LOG_CT` (where the heartbeats are read).

- The flasher keeps its build cache in `/root/flash/.pio`, and `secrets.ini` is deleted after each run.
- Board probes are about 50 % lossy (Wi-Fi power-save, do not disable it); the script retries the upload once.
- Don't use the `workspace` CT (113) as flasher: its firewall drops traffic to `10.0.0.0/16`.
- Do not turn Pi SNAT off: that broke tailnet access to LAN hosts (rolled back 28 Nov).
- The firmware's `fw` rev comes from `$GIT_REV` (set by the script, the flasher has no `.git`), else from git.
- Verified 2026-11-28: build + OTA + heartbeat check `FLASH OK` in 70 s (incremental) from a cloud session.

## Where else it can run

- **Cloud session, USB:** impossible. Ask the user, or start a Remote Control session on a machine with the board on USB.
- **User's machine / Remote Control:** everything below.

## Prerequisites

- `firmware/secrets.ini` exists (copy `secrets.ini.example`). If it's missing the build succeeds but
  Wi-Fi/OTA are compiled out, and the next update will need USB again. Check before a USB flash.
  It is gitignored, so a fresh worktree or cloud session lacks it: see "secrets.ini" below.
- Linux USB access: user in `dialout` (or `uucp`). That is enough on Ubuntu; if the port is still
  refused, install PlatformIO's udev rules
  (`curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core/develop/platformio/assets/system/99-platformio-udev.rules | sudo tee /etc/udev/rules.d/99-platformio-udev.rules`).
- Linux mDNS for `<hostname>.local`: avahi + nss-mdns. A Nix-installed PlatformIO can't use it
  (see OTA below).
- On the home LAN the board `whistle-echo` has a DHCP reservation (10.0.1.52, in the
  `houseabsolute-openwrt` router config) and DNS name `whistle-echo.ha`. Give new boards one too.

## secrets.ini

The shared copy is the Secret Server entry `whistle_secrets_ini` (base64 of the whole file): fetch it
with `get_whistle_secrets_ini` and `house-absolute:fetching-secrets` (`scripts/flash.sh` does this for cloud flashes);
decrypt it straight into `firmware/secrets.ini`. Its `debug_server` is the deployed server,
`http://whistle.i.houseabsolute.co.uk` (plain HTTP on purpose: over TLS the board's clip uploads fail for
lack of memory; the ingress serves `/api/device/` on port 80, homelab-infra#235). To use a laptop debug
server instead, override that line locally.

To flash a powered board from the home LAN: write `secrets.ini` as above, run
`pio run -e atom-echo-ota -t upload --upload-port 10.0.1.52`, then delete `secrets.ini`. It needs only
power and Wi-Fi, no USB data. Without serial, check that the server's `device-heartbeats.jsonl` (CT 131,
`/data/whistle/`) gains lines with the new `fw`.

Whenever `secrets.ini` changes, store it again (from a machine with lab SSH access):

```sh
printf 'WHISTLE_SECRETS_INI=%s\n' "$(base64 -w0 firmware/secrets.ini)" \
  | ssh root@10.0.1.3 /opt/homelab-infra/bin/seed-secret.sh --amend secretserver
ssh root@10.0.1.3 'pct exec 118 -- /run/current-system/sw/bin/systemctl restart secretserver'
```

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
pio device monitor -e atom-echo      # expect "whistle-fw <rev>: mic up, ring 4.10 s ..." (3.07 s with https),
                                     # "wifi up: <ip>, OTA at <host>.local, server ...", then "net: heartbeat ok" every 10 s
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
