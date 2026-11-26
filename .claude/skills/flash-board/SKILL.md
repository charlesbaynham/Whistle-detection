---
name: flash-board
description: Invoke whenever an ESP32 board (M5Stack ATOM Echo) is flashed with firmware, over USB or OTA, or when a flash fails.
---

# Flashing the ATOM Echo

## Where it can run

- **Cloud session, OTA over Tailscale (canonical):** see "Cloud OTA" below.
- **Cloud session, USB:** impossible. Ask the user, or start a Remote Control session on a machine with the board on USB.
- **User's machine / Remote Control:** everything below.

## Cloud OTA (Tailscale)

The board has a fixed IP, `10.0.1.52` (DHCP reservation on the Pi; DNS `whistle-echo.ha`, not resolvable from the cloud, use the IP).
Cloud sessions reach `10.0.0.0/16` via the house Headscale; `openwrt-pi` (`100.64.0.1`, `10.0.1.11`) is the subnet router.

```sh
# 1. Mint an ephemeral GPG key (house-absolute:fetching-secrets), then call get_ts_authkey and
#    get_whistle_secrets_ini together. Decrypt each straight into its consumer; never echo either:
TS_AUTHKEY="$($S/decrypt.sh ts.asc $FPR)" .claude/skills/flash-board/ts_connect.sh
# 2. Check the board answers (expect "reply: AUTH <nonce>"):
python3 .claude/skills/flash-board/ota_probe.py 10.0.1.52
# 3. Write secrets.ini (see "secrets.ini" below), then build and upload:
(umask 077; "$S/decrypt.sh" whistle.asc "$FPR" | base64 -d > firmware/secrets.ini)
uv tool install platformio    # once; ~/.local/bin/pio
cd firmware && pio run -e atom-echo-ota -t upload --upload-port 10.0.1.52
```

- The key is reusable + ephemeral, expires 2027-10-15 (renewal procedure is in the `tailscale-connect` skill of `ha-workspace`).
- `tailscaled` dies when the container sleeps; re-run `ts_connect.sh` (idempotent) if probes start timing out.
- Verified 2026-11-26: kernel-mode join works in a cloud session (`/dev/net/tun` present), `tailscale ping 10.0.1.52`
  routes via openwrt-pi (DERP), and the board answers the OTA invitation with `AUTH`. Build of `atom-echo-ota` works.
- **Not yet verified: the actual upload.** espota makes the board connect *back* over TCP to the invitation's source
  address. Tailnet→LAN traffic is SNATed by the Pi, so the board sees `10.0.1.11` and will connect to the Pi, not to
  the cloud session. If the upload stalls after `Authenticating...OK` ("No response from device" / hangs at 0%), that is
  why. Fixes, in order of preference: (a) forward the port through the Pi (`ssh -R 0.0.0.0:<p>:127.0.0.1:<p>` to the Pi
  with `--host_port=<p>`, needs dropbear `-a`/GatewayPorts; `lab-ssh` skill in ha-workspace); (b) disable SNAT on the Pi
  (`--snat-subnet-routes=false`) and add `100.64.0.0/10 via 10.0.1.11` on the board's gateway; (c) switch to HTTP-pull OTA
  (planned). Update this section with whichever works.
- No `/dev/net/tun`: `ts_connect.sh` falls back to userspace networking, where only processes using
  `tailscale nc`/the SOCKS proxy are routed; plain espota would not be. Untested.

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
`firmware/secrets.ini` as in "Cloud OTA" step 3. Its `debug_server` is the deployed server,
`https://whistle.i.houseabsolute.co.uk` (since 2026-11-26; the firmware needs HTTPS support, #15, so flash
current `main`). To use a laptop debug server instead, override that line locally.

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
pio device monitor -e atom-echo      # expect "whistle-fw <rev>: mic up, ring 3.07 s ..." (4.10 s with plain HTTP),
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
