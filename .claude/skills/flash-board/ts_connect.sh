#!/usr/bin/env bash
# Join the house Headscale tailnet from a cloud session (idempotent). Needs TS_AUTHKEY in the environment
# (see SKILL.md: decrypt get_ts_authkey straight into this call; never echo it).
set -euo pipefail
VER=1.86.2
if ! pgrep -x tailscaled >/dev/null; then
  if [ "$(tailscale version 2>/dev/null | head -1)" != "$VER" ]; then
    curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_${VER}_amd64.tgz" | tar -xz -C /tmp
    install -m755 /tmp/tailscale_${VER}_amd64/tailscale{,d} /usr/local/bin/
  fi
  mkdir -p /var/lib/tailscale /var/run/tailscale
  # Kernel mode if /dev/net/tun exists; userspace networking otherwise (ssh stalled in userspace mode, but
  # single-flow UDP/TCP such as espota should be fine; use `tailscale nc`/SOCKS only if routes don't appear).
  MODE=""; [ -c /dev/net/tun ] || MODE="--tun=userspace-networking"
  # Go honours HTTPS_PROXY + SSL_CERT_FILE, both set in cloud sessions.
  nohup tailscaled $MODE --state=/var/lib/tailscale/tailscaled.state \
    --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 &
  for _ in $(seq 20); do rc=0; tailscale status >/dev/null 2>&1 || rc=$?; [ "$rc" -ne 1 ] && break; sleep 1; done
fi
tailscale up --login-server=https://headscale.houseabsolute.co.uk --authkey="${TS_AUTHKEY}" \
  --hostname="cc-$(hostname)-$RANDOM" --accept-routes --accept-dns=false --ssh=false >/dev/null
tailscale status | head -3
