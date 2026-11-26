---
name: device-debug-server
description: Run whistle-lab on the laptop as the ATOM Echo's debug server, to receive its trigger clips and heartbeats and re-check them with the full detector. Use when testing firmware triggers, reading device debug output, or when the device's uploads fail.
---

# Device debug server

The firmware POSTs triggers and heartbeats to `debug_server` (host:port) from `firmware/secrets.ini`.
For testing, that is the laptop running whistle-lab.

## Templates: copy the datasets from the deployed server

The deployed app (`https://whistle.i.houseabsolute.co.uk`) sits behind GitHub SSO, so fetch the files
over SSH instead. The data is on CT 131 at `/data/whistle/<dataset>/`, reachable via the Proxmox host.
Read-only. Use the lab SSH key from the secret server (`get_lab_ssh_key`, the `fetching-secrets` skill in
`~/ha-workspace`). Load it into a throwaway agent, not `~/.ssh` (that is the user's own config):

```sh
eval "$(ssh-agent -a /tmp/lab-agent.sock -t 3h)"
decrypt.sh <ciphertext> <fpr> | base64 -d | SSH_AUTH_SOCK=/tmp/lab-agent.sock ssh-add -
mkdir -p data && SSH_AUTH_SOCK=/tmp/lab-agent.sock ssh root@10.0.1.3 \
  'pct exec 131 -- /run/current-system/sw/bin/bash -lc "cd /data/whistle && tar -cz default detect-tests"' \
  | tar -xz -C data
```

`pct exec` has no PATH, hence the full bash path. `data/` is gitignored. In a worktree, copy it into
that worktree.

## Run

```sh
ip -4 addr show wlp0s20f3           # laptop IP; the lease is dynamic, so check it matches debug_server
WHISTLE_DATA_DIR=data PYTHONUNBUFFERED=1 uv run whistle-lab > /tmp/whistle-server.log 2>&1   # background
curl -s http://<laptop-ip>:8000/health
```

Changing `debug_server` needs a reflash (`flash-board`). Port 8000 must be reachable from the LAN.

## Reading the output

- The server log has one `[trigger]` line per clip (notes in Hz, verdict, best pattern, ρ̂, veto, id) and one
  `[heartbeat]` line every 10 s.
- Clips and metadata: `data/device-triggers/<id>.{wav,json}`. The device's debug fields are under `client`
  and the server's verdict under `server`. They can be browsed and relabelled in the web UI at
  `http://localhost:8000`.
- Heartbeats: `data/device-heartbeats.jsonl`. `sent`/`failed`/`overrun`/`dropped` are cumulative;
  the other counts cover the last 10 s.
- No heartbeats at all: the device isn't on Wi-Fi, `debug_server` is wrong or empty, or a firewall is in
  the way. Check the device's serial boot line `wifi up: ..., server ...`.
