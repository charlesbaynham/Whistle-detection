---
name: flash-board
description: Invoke whenever an ESP32 board (M5Stack ATOM Echo) is flashed with firmware, over USB or OTA, or when a flash fails.
---

# Flashing the ATOM Echo

## Where it can run

- **Cloud session, OTA via the flasher container (the only cloud route):** see "Flasher container" below.
- **Cloud session, USB:** impossible. Ask the user, or start a Remote Control session on a machine with the board on USB.
- **User's machine / Remote Control:** everything below.

## Flasher container (cloud route)

CT 138 `flasher` on the Proxmox host: stock Debian 13 LXC with `pio` and `uv`, fixed `10.0.2.14`, declared in
homelab-infra (`tofu/flasher`, `bin/deploy-flasher.sh`; homelab-infra#237). It sits on the LAN, so the board's espota
connect-back reaches it directly. It is **not** on the tailnet: cloud sessions reach it through the Pi's `10.0.0.0/16`
subnet route, which works with Pi SNAT on (leave SNAT on; never `tailscale set --snat-subnet-routes=false`, it breaks
tailnet access to most LAN hosts). Cattle: it is rebuilt when its declaration changes, so PlatformIO re-downloads
(about 1.5 GB) on the first build afterwards. Authorised keys are Charles's set plus the agent key.

Verified 2026-11-28: build + OTA `SUCCESS` in 115 s from CT 138. The old hand-made CT 137 is gone; never recreate guests by hand.

```sh
# 1. Join the tailnet (tailscale-connect skill in ha-workspace, or .claude/skills/flash-board/ts_connect.sh with
#    get_ts_authkey), and have the lab SSH key loaded (lab-ssh skill). Then ssh root@10.0.2.14.
# 2. Ship the firmware and secrets.ini (decrypt straight into ssh; never echo):
tar --exclude=.git --exclude=.pio --exclude=secrets.ini -czf - firmware | ssh root@10.0.2.14 'rm -rf /root/wd && mkdir /root/wd && tar -xzf - -C /root/wd'
"$S/decrypt.sh" whistle.asc "$FPR" | base64 -d | ssh root@10.0.2.14 'umask 077; cat > /root/wd/firmware/secrets.ini'
# 3. Build + upload on the flasher, in the background (about 2-4 min), then delete secrets.ini:
ssh root@10.0.2.14 'cd /root/wd/firmware && PATH=/root/.local/bin:$PATH nohup sh -c "pio run -e atom-echo-ota -t upload --upload-port 10.0.1.52; echo EXIT=\$?; rm -f secrets.ini" >/root/ota.log 2>&1 </dev/null &'
ssh root@10.0.2.14 tail /root/ota.log            # expect Result/SUCCESS and EXIT=0
scp .claude/skills/flash-board/ota_probe.py root@10.0.2.14:/tmp/ && ssh root@10.0.2.14 'python3 /tmp/ota_probe.py 10.0.1.52'   # optional
```

- The board is `10.0.1.52` (DHCP reservation on the Pi; DNS `whistle-echo.ha` is not resolvable from the cloud). Probes
  are about 50 % lossy (Wi-Fi power-save): retry before concluding the board is down.
- Don't keep secrets.ini on the flasher (step 3 removes it).
- Don't use the `workspace` CT (113) as the flash host: its Proxmox firewall drops outbound to `10.0.0.0/16` except SSH.
- Flashing straight from the cloud session over the tailnet (without the flasher) needs the Pi to not SNAT, which
  was tried on 2026-11-26/27 and rolled back on 2026-11-28 (it broke tailnet access to LAN hosts behind `10.0.1.1`).
- Check afterwards: the board reboots; `device-heartbeats.jsonl` on CT 131 (NixOS CT: use `/run/current-system/sw/bin`
  in PATH) shows `uptime_s` restarting from 0, and HA's `sensor.whistle_echo_uptime` / `sensor.whistle_echo_*` update.

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
with `get_whistle_secrets_ini` and `house-absolute:fetching-secrets`, and decrypt only into
`firmware/secrets.ini` as in "Flasher container" step 2. Its `debug_server` is the deployed server,
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
