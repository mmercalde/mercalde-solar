#!/bin/bash
# The limits, whatever the runner is doing: Tctl above 85 C for 10 s, swap past 2 GB.
D=${ABX_DIR:-/tmp/abexam}
FLIGHT=${ABX_FLIGHT:-/var/log/kamrui-flight.log}
SWAP_LIMIT_KB=1953125
TCTL_LIMIT=85000
HOT_SECONDS=10
MEMINFO=${ABX_MEMINFO:-/proc/meminfo}
CUT=${ABX_CUT:-$(date -d "2026-11-05 03:25" +%s)}   # leaves the dead-man a clean look at 03:30
hot_since=0; last_line=""; last_change=$(date +%s); tick=0

# Read directly, as run.sh does. The flight log is only for Tctl and GTT.
swap_kb() { awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print t-f}' "$MEMINFO"; }
cand_alive() { [ -e "$D/cand.pid" ] && kill -0 "$(cat "$D/cand.pid")" 2>/dev/null; }

kill_cand() {
  cand_alive || return 0
  local pid; pid=$(cat "$D/cand.pid")
  echo "$(date '+%F %T') KILL pid=$pid: $1" | tee -a "$D/events.log" > "$D/KILLED"
  kill "$pid"
  for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -9 "$pid" 2>/dev/null
  # Live back at once. The exam only has failures left to record, which can
  # take minutes at 30 s apart, and live should not wait for that.
  systemctl is-active --quiet llama-server || sudo -n systemctl start llama-server
}

while [ -e "$D/guard.run" ]; do
  now=$(date +%s); tick=$((tick+1))
  line=$(tail -1 "$FLIGHT")
  [ "$line" != "$last_line" ] && { last_line="$line"; last_change=$now; }
  [[ $line =~ tctl=([0-9]+) ]] && tctl=${BASH_REMATCH[1]} || tctl=0
  [[ $line =~ gtt=([0-9]+) ]] && gtt=${BASH_REMATCH[1]} || gtt=0
  swap=$(swap_kb)
  echo "$(date +%T) phase=$(cat "$D/phase" 2>/dev/null) cand=$(cand_alive && echo 1 || echo 0) tctl=$tctl swap_kb=$swap gtt=$gtt" >> "$D/guard.log"

  if cand_alive; then
    [ $((now-last_change)) -gt 30 ] && kill_cand "flight recorder silent for 30 s, limits cannot be read"
    [ "$swap" -gt $SWAP_LIMIT_KB ] && kill_cand "swap ${swap} kB past 2 GB"
    if [ "$tctl" -gt $TCTL_LIMIT ]; then
      [ $hot_since -eq 0 ] && { hot_since=$now; echo "$(date '+%F %T') tctl over limit: $tctl" >> "$D/events.log"; }
      [ $((now-hot_since)) -ge $HOT_SECONDS ] && kill_cand "Tctl above 85 C for ${HOT_SECONDS} s (now $tctl)"
    else
      hot_since=0
    fi
    [ "$now" -ge "$CUT" ] && kill_cand "03:25 backstop"
  else
    hot_since=0
  fi

  # A generator starting mid-run: the current model finishes, then no more.
  if [ $((tick%30)) -eq 1 ]; then
    data=$(curl -s -m 8 http://192.168.3.10:8080/data)
    if [ -z "$data" ]; then
      echo "$(date '+%F %T') generator state unreadable" >> "$D/events.log"
    elif echo "$data" | grep -qE '"(kubotaAction|mep803aAction)":9[,}]'; then
      [ -e "$D/GEN_FLAG" ] || echo "$(date '+%F %T') generator running: $(echo "$data" | grep -oE '"(kubotaState|mepState)":"[^"]*"' | tr '\n' ' ')" | tee -a "$D/events.log" > "$D/GEN_FLAG"
    fi
  fi
  sleep 1
done
echo "$(date '+%F %T') guard exit" >> "$D/events.log"
