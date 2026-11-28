#!/usr/bin/env bash
# One-command OTA flash of the ATOM Echo from a cloud session, via the LAN flasher container.
#   scripts/flash.sh prep            mint the session GPG key; prints what to fetch from the Secret Server
#   scripts/flash.sh save NAME       store a Secret Server ciphertext (stdin) as NAME = ssh | ts | ini
#   scripts/flash.sh [--ref REF | --worktree] [--no-verify]    build REF (default HEAD) and flash it
# Exit: 0 flashed and verified | 2 usage | 10 secrets missing | 11 flasher unreachable | 12 build/upload failed
#       13 uploaded but reboot not verified. Details: .claude/skills/flash-board/SKILL.md
set -uo pipefail

FLASHER_HOST=${FLASHER_HOST:-10.0.2.14}     # CT 138 over the Pi subnet route; 100.64.0.19 for the old hand-made CT 137
BOARD_IP=${BOARD_IP:-10.0.1.52}
LOG_HOST=${LOG_HOST:-root@10.0.1.3}         # Proxmox host; the debug server (CT 131) holds the board's heartbeats
LOG_CT=${LOG_CT:-131}
FLASH_DIR=${FLASH_DIR:-/tmp/flash-secrets}
UPLOAD_TIMEOUT=${UPLOAD_TIMEOUT:-900}
VERIFY_TIMEOUT=${VERIFY_TIMEOUT:-120}

RDIR=/root/flash   # own directory on the flasher; the build tree and log live here
KEY=$FLASH_DIR/ssh_key
export GNUPGHOME=$FLASH_DIR/gnupg
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE/.." || exit 2

die() { local c=$1; shift; echo "FLASH FAIL ($c): $*" >&2; exit "$c"; }
log() { echo "[flash] $*"; }
dec() { gpg -q --batch --decrypt "$FLASH_DIR/$1.asc" 2>/dev/null; }
have() { [ -s "$FLASH_DIR/$1.asc" ]; }
SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new
          -o UserKnownHostsFile="$FLASH_DIR/known_hosts" -o ConnectTimeout=10 -o ServerAliveInterval=15 -o LogLevel=ERROR)
fssh() { ssh "${SSH_OPTS[@]}" "root@$FLASHER_HOST" "$@"; }

cmd_prep() {
  umask 077; mkdir -p "$GNUPGHOME"
  gpg --list-keys agent@localhost >/dev/null 2>&1 || gpg --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "Flash Agent <agent@localhost>" default default 1d >/dev/null 2>&1
  gpg --list-keys agent@localhost >/dev/null 2>&1 || die 10 "could not mint a GPG key"
  cat <<MSG
Call these Secret Server tools in parallel with the armored public key below as armored_public_key
(ts is only needed when the tailnet is not already up), then pipe each result's "ciphertext" value to
  scripts/flash.sh save <name>      (stdin; the JSON \\n-escaped string or the armored block both work)
    get_lab_ssh_key -> ssh | get_whistle_secrets_ini -> ini | get_ts_authkey -> ts
Then run: scripts/flash.sh

MSG
  gpg --armor --export agent@localhost
}

cmd_save() {
  case ${1:-} in ssh|ts|ini) ;; *) die 2 "usage: flash.sh save ssh|ts|ini  (ciphertext on stdin)";; esac
  umask 077; mkdir -p "$FLASH_DIR"
  local t; t=$(cat); t=${t#\"}; t=${t%\"}
  case $t in *'\n'*) t=$(printf '%b' "${t//\\\"/\"}");; esac
  printf '%s\n' "$t" > "$FLASH_DIR/$1.new"
  if gpg -q --batch --decrypt "$FLASH_DIR/$1.new" >/dev/null 2>&1; then mv "$FLASH_DIR/$1.new" "$FLASH_DIR/$1.asc"; log "saved $1"
  else rm -f "$FLASH_DIR/$1.new"; die 10 "$1 does not decrypt with this session's key (run prep first; use the key it printed)"; fi
}

ensure_key() {
  have ssh || die 10 "lab SSH key missing: run scripts/flash.sh prep and follow it"
  umask 077
  dec ssh | { read -r first; if [[ $first == -----BEGIN* ]]; then { echo "$first"; cat; }; else { echo "$first"; cat; } | base64 -d; fi; } > "$KEY"
  chmod 600 "$KEY"; ssh-keygen -yf "$KEY" >/dev/null 2>&1 || die 10 "lab SSH key did not decode"
}

ensure_flasher() {
  fssh true 2>/dev/null && return
  have ts || die 10 "flasher unreachable and no tailnet key: run scripts/flash.sh prep (fetch ts too)"
  log "joining tailnet"
  TS_AUTHKEY=$(dec ts) bash "$HERE/../.claude/skills/flash-board/ts_connect.sh" >/dev/null 2>&1 || die 11 "tailnet join failed (see /var/log/tailscaled.log)"
  for _ in $(seq 15); do fssh true 2>/dev/null && return; sleep 2; done
  die 11 "cannot ssh root@$FLASHER_HOST (key not in its authorized_keys, or host down; set FLASHER_HOST)"
}

heartbeat() {  # latest heartbeat line from the debug server, via the Proxmox host
  ssh "${SSH_OPTS[@]}" "$LOG_HOST" "pct exec $LOG_CT -- /run/current-system/sw/bin/tail -n 1 /data/whistle/device-heartbeats.jsonl" 2>/dev/null
}
hb_field() { python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get(sys.argv[1],""))' "$1" 2>/dev/null; }

REF=HEAD; WORKTREE=0; VERIFY=1
case ${1:-} in
  prep) cmd_prep; exit;;
  save) shift; cmd_save "$@"; exit;;
esac
while [ $# -gt 0 ]; do
  case $1 in
    --ref) REF=${2:?}; shift;; --worktree) WORKTREE=1;; --no-verify) VERIFY=0;;
    *) die 2 "usage: flash.sh [prep | save NAME | --ref REF | --worktree | --no-verify]";;
  esac; shift
done

have ini || die 10 "secrets.ini missing: run scripts/flash.sh prep and follow it"
ensure_key
ensure_flasher
fssh 'PATH=/root/.local/bin:$PATH command -v pio >/dev/null' || die 11 "no PlatformIO on $FLASHER_HOST"

if [ $WORKTREE = 1 ]; then REV=$(git rev-parse --short=7 HEAD)+; else REV=$(git rev-parse --short=7 "$REF") || die 2 "bad ref $REF"; fi
PREV=$(heartbeat); [ -n "$PREV" ] && log "board now: fw=$(hb_field fw <<<"$PREV") uptime=$(hb_field uptime_s <<<"$PREV")s"
log "shipping firmware @ $REV to $FLASHER_HOST"
if [ $WORKTREE = 1 ]; then SRC=(tar --exclude=.pio --exclude=secrets.ini -cf - firmware); else SRC=(git archive "$REF" firmware); fi
fssh "mkdir -p $RDIR/firmware && cd $RDIR/firmware && find . -mindepth 1 -maxdepth 1 ! -name .pio -exec rm -rf {} + && cd .. && tar -x" < <("${SRC[@]}") \
  || die 11 "could not ship the firmware"
dec ini | { read -r first; if [[ $first == \[* ]]; then { echo "$first"; cat; }; else { echo "$first"; cat; } | base64 -d; fi; } \
  | fssh "umask 077; cat > $RDIR/firmware/secrets.ini" || die 11 "could not write secrets.ini on the flasher"
fssh "grep -q '^\[secrets\]' $RDIR/firmware/secrets.ini" || die 10 "secrets.ini on the flasher is not a valid file"

T0=$(date +%s)
log "build + upload (about 2-4 min)"
fssh "rm -f $RDIR/ota.log; cat > $RDIR/run.sh" <<RUN
cd $RDIR/firmware; export PATH=/root/.local/bin:\$PATH GIT_REV=$REV
for try in 1 2; do
  pio run -e atom-echo-ota -t upload --upload-port $BOARD_IP >> $RDIR/ota.log 2>&1; rc=\$?
  [ \$rc = 0 ] && break; echo "--- attempt \$try failed (\$rc)" >> $RDIR/ota.log; sleep 5
done
rm -f secrets.ini; echo EXIT=\$rc >> $RDIR/ota.log
RUN
fssh "setsid nohup bash $RDIR/run.sh </dev/null >/dev/null 2>&1 &" || die 11 "could not start the build on the flasher"
RC=""
while [ -z "$RC" ] && [ $(( $(date +%s) - T0 )) -lt "$UPLOAD_TIMEOUT" ]; do
  sleep 10; RC=$(fssh "sed -n 's/^EXIT=//p' $RDIR/ota.log" 2>/dev/null)
done
[ -n "$RC" ] || die 12 "no result after ${UPLOAD_TIMEOUT}s (see $RDIR/ota.log on $FLASHER_HOST)"
if [ "$RC" != 0 ]; then
  fssh "tail -n 12 $RDIR/ota.log | cut -c1-200" >&2
  die 12 "build/upload exit $RC (log above; also $RDIR/ota.log on $FLASHER_HOST)"
fi
T1=$(date +%s); log "upload OK in $((T1 - T0))s"

[ $VERIFY = 1 ] || { echo "FLASH OK (unverified) rev=$REV"; exit 0; }
while [ $(( $(date +%s) - T1 )) -lt "$VERIFY_TIMEOUT" ]; do
  sleep 10; HB=$(heartbeat) || continue
  [ -n "$HB" ] || continue
  UP=$(hb_field uptime_s <<<"$HB"); FW=$(hb_field fw <<<"$HB")
  if [ "$FW" = "$REV" ] && [ -n "$UP" ] && [ "$UP" -le $(( $(date +%s) - T1 + 30 )) ]; then
    echo "FLASH OK rev=$REV flasher=$FLASHER_HOST total=$(( $(date +%s) - T0 ))s board: fw=$FW uptime=${UP}s rssi=$(hb_field rssi <<<"$HB")"
    exit 0
  fi
done
die 13 "uploaded $REV but no heartbeat with fw=$REV and a fresh uptime within ${VERIFY_TIMEOUT}s (last: fw=${FW:-?} uptime=${UP:-?}); check the board"
