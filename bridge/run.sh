#!/bin/sh
# Docker variant of run.cmd. The code runs from /data/code, so an update made by the bridge itself (bridge_update:
# it leaves stoeckie_bridge.next.py and exits with code 75) survives a container restart. A newer image (git pull,
# docker compose up -d --build) brings its own code: whichever VERSION is newer wins. If an update does not stay up
# for a minute (no update-ok.flag), the previous version is put back.
set -u
export STBRIDGE_RUNNER=sh
CODE="${STBRIDGE_DATA:-/data}/code"
mkdir -p "$CODE"
ver() { sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1; }
if [ ! -f "$CODE/stoeckie_bridge.py" ] || [ "$(printf '%s\n%s\n' "$(ver /app/stoeckie_bridge.py)" "$(ver "$CODE/stoeckie_bridge.py")" | sort | tail -n 1)" != "$(ver "$CODE/stoeckie_bridge.py")" ]; then
  cp /app/stoeckie_bridge.py "$CODE/stoeckie_bridge.py"
  echo "$(date '+%F %T') code from the image: $(ver /app/stoeckie_bridge.py)" >> "$CODE/run-restarts.log"
fi
cp /app/requirements.txt "$CODE/requirements.txt"
[ -f "$CODE/config.example.json" ] || cp /app/config.example.json "$CODE/"
cd "$CODE"
while :; do
  if [ -f stoeckie_bridge.next.py ]; then
    cp stoeckie_bridge.py stoeckie_bridge.prev.py
    mv -f stoeckie_bridge.next.py stoeckie_bridge.py
    rm -f update-ok.flag
    echo pending > update-pending.flag
  fi
  python stoeckie_bridge.py
  rc=$?
  if [ "$rc" -eq 75 ]; then continue; fi
  if [ -f update-pending.flag ] && [ -f stoeckie_bridge.prev.py ]; then
    cp stoeckie_bridge.prev.py stoeckie_bridge.py
    rm -f update-pending.flag
    echo "$(date '+%F %T') update did not stay up (code $rc), previous version back" >> run-restarts.log
    continue
  fi
  if [ "$rc" -ne 0 ]; then
    echo "$(date '+%F %T') bridge stopped with code $rc, start again in 30 s" >> run-restarts.log
    sleep 30
    continue
  fi
  exit "$rc"
done
