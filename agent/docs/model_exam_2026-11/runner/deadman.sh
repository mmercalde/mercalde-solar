#!/bin/bash
# Separate from the runner, so it still acts if the runner is kill -9'd.
# At 20:20: if live is not healthy, stop the test and start it regardless.
# Then the sudoers rule goes, whatever happened.
D=${ABX_DIR:-/tmp/abexam}
RULE=/etc/sudoers.d/llama-abtest
AT=${ABX_DEADMAN:-$(date -d "2026-11-04 20:20" +%s)}
RUNNER_PID=$1
say() { echo "$(date '+%F %T') dead-man: $*" >> "$D/events.log"; }
live_ok() { [ "$(curl -s -m 3 http://127.0.0.1:8080/health)" = '{"status":"ok"}' ]; }

say "armed for $(date -d "@$AT" '+%F %T'), watching runner pid=$RUNNER_PID"
while [ "$(date +%s)" -lt "$AT" ]; do
  # The runner finished with live up and has removed the rule: nothing to do.
  if [ -e "$D/done" ] && live_ok; then say "runner done and live healthy; standing down"; exit 0; fi
  # Gone without finishing, so no trap ran (kill -9, OOM): nobody is coming back.
  if ! kill -0 "$RUNNER_PID" 2>/dev/null && [ ! -e "$D/done" ]; then say "runner gone without finishing"; break; fi
  sleep "${ABX_DM_POLL:-20}"
done

if live_ok && [ -e "$D/done" ]; then
  say "20:20: live healthy"
elif live_ok && kill -0 "$RUNNER_PID" 2>/dev/null; then
  say "20:20: live healthy, runner still going; leaving it to finish on its own"
  for _ in $(seq 1 180); do [ -e "$D/done" ] && break; sleep 20; done   # up to an hour
  [ -e "$D/done" ] || { say "runner still not done at 21:20; stopping it"; touch "$D/STOP"; kill -TERM "$RUNNER_PID" 2>/dev/null; sleep 300; }
else
  say "stopping the test and starting live (live_ok=$(live_ok && echo yes || echo no))"
  touch "$D/STOP"
  kill -TERM "$RUNNER_PID" 2>/dev/null
  for _ in $(seq 1 12); do kill -0 "$RUNNER_PID" 2>/dev/null || break; sleep 5; done
  kill -KILL "$RUNNER_PID" 2>/dev/null
  rm -f "$D/guard.run"
  pids=$(ss -ltnpH 'sport = :8082' 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
  for p in $pids; do kill -9 "$p" 2>/dev/null; done
  for try in 1 2; do
    live_ok && break
    sudo -n systemctl start llama-server
    for _ in $(seq 1 "${ABX_HEALTH_POLLS:-60}"); do live_ok && break; sleep 3; done
  done
  if live_ok; then say "live UP"; else say "live STILL DOWN after two starts"; fi
fi

if sudo -n /usr/bin/rm "$RULE"; then say "sudoers rule removed"; else say "sudoers rule NOT removed"; fi
{ echo "state=done"; echo "detail=closed by the dead-man; live=$(live_ok && echo up || echo DOWN)"; echo "updated=$(date '+%F %T')"; } > "$D/status"
touch "$D/done"
