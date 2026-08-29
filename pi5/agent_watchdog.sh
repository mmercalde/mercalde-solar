#!/bin/bash
# agent_watchdog.sh - reset generator thresholds if the solar agent goes silent.
#
# Runs on the Pi5 from cron every 30 minutes. Nothing else on the Pi5 changes;
# app.py is not touched.
#
# Install (the owner does this by hand):
#   scp pi5/agent_watchdog.sh michael@192.168.3.10:/home/michael/solar_dashboard/
#   ssh michael@192.168.3.10 chmod +x /home/michael/solar_dashboard/agent_watchdog.sh
#   crontab -e   and add:
#
#   */30 * * * * /home/michael/solar_dashboard/agent_watchdog.sh >> /home/michael/solar_dashboard/agent_watchdog.log 2>&1
#
# WHEN IT ACTS
#
# Only after SIX HOURS OF CONTINUOUS SILENCE, measured from the last time the
# agent actually answered. "Unreachable right now" is not silence: an earlier
# version treated it that way and reset the live thresholds on its very first
# run, before the agent was even listening.
#
# The state file remembers the last time the agent answered, plus the learning
# gate and the default thresholds it reported. With no state file, the first
# run records the time and exits without touching anything.
#
# It also never resets while the agent's learning gate is closed. During the
# learning phase the guard refuses every write, so the agent cannot be the
# reason the thresholds are off the defaults - the owner is - and resetting
# would be overriding a person.
#
# HOW THE AGENT IS DETECTED
#
# SPEC section 9 suggests finding the agent's last /config write in an access
# log, marked either by an "X-Agent: solar-agent" header or by "&src=agent".
# Neither is available on this Pi5: gunicorn runs without --access-logfile
# (see solar-dashboard.service), so it writes no HTTP access lines at all, and
# enabling them would mean editing the unit and restarting the dashboard.
#
# So liveness comes from the agent itself: GET /plan on KAMRUI:8090 answers
# with the learning gate and the configured defaults whenever the agent is up.

set -uo pipefail

DASHBOARD="http://127.0.0.1:8080"
AGENT_PLAN="http://192.168.3.152:8090/plan"
SILENT_SECONDS=$((6 * 3600))
STATE_FILE="/home/michael/solar_dashboard/.agent_watchdog_state"

# Used only until the agent has told us what its config.json says. The agent
# reports defaults.start / defaults.stop on /plan and they are cached in the
# state file, so these are a cold-start fallback, not the source of truth.
FALLBACK_START="52.0"
FALLBACK_STOP="56.0"

# Telegram: reuse the dashboard's own credentials rather than storing a copy.
DASH_CONFIG="/home/michael/solar_dashboard/config.json"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

pyjson() {  # pyjson <file-or-"-"> <expression over d>   -> value, or "" on error
    python3 -c "
import json,sys
d = json.load(open(sys.argv[1])) if sys.argv[1] != '-' else json.load(sys.stdin)
try:
    v = $2
    print('' if v is None else v)
except Exception:
    print('')
" "$1" 2>/dev/null
}

write_state() {  # write_state <last_seen> <learning_open> <start> <stop>
    python3 - "$STATE_FILE" "$1" "$2" "$3" "$4" <<'PY'
import json, sys
path, seen, gate, start, stop = sys.argv[1:6]
state = {"last_seen": int(seen), "learning_open": gate == "true"}
if start:
    state["default_start"] = float(start)
if stop:
    state["default_stop"] = float(stop)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f)
import os
os.replace(tmp, path)
PY
}

send_telegram() {
    local text="$1" token chat
    [ -f "$DASH_CONFIG" ] || { log "no $DASH_CONFIG; cannot send Telegram"; return; }
    token=$(pyjson "$DASH_CONFIG" "d['telegram']['token']")
    chat=$(pyjson "$DASH_CONFIG" "d['telegram']['chatId']")
    if [ -z "$token" ] || [ -z "$chat" ]; then
        log "Telegram not configured in $DASH_CONFIG"
        return
    fi
    curl -s -m 15 -o /dev/null \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${text}" \
        "https://api.telegram.org/bot${token}/sendMessage" \
        && log "Telegram sent: ${text}"
}

now=$(date +%s)

# --- ask the agent ----------------------------------------------------------

plan_json=$(curl -s -m 10 "$AGENT_PLAN" 2>/dev/null)
answered="no"
gate=""
def_start=""
def_stop=""
if [ -n "$plan_json" ]; then
    gate=$(printf '%s' "$plan_json" | pyjson - "'true' if d['learning']['open'] else 'false'")
    def_start=$(printf '%s' "$plan_json" | pyjson - "d['defaults']['start']")
    def_stop=$(printf  '%s' "$plan_json" | pyjson - "d['defaults']['stop']")
    plan_ts=$(printf   '%s' "$plan_json" | pyjson - "int(d['ts'])")
    # A parseable answer is proof the agent is alive, whether or not it has
    # recorded a plan yet.
    if [ -n "$gate" ]; then
        answered="yes"
    fi
fi

if [ "$answered" = "yes" ]; then
    if [ -n "${plan_ts:-}" ]; then
        log "agent answered; learning gate open=${gate}; newest plan $(( (now - plan_ts) / 60 )) min old"
    else
        log "agent answered; learning gate open=${gate}; no plan recorded yet"
    fi
    write_state "$now" "$gate" "$def_start" "$def_stop"
    rm -f "${STATE_FILE}.notified"
    exit 0
fi

log "agent did not answer $AGENT_PLAN"

# --- how long has it been silent? -------------------------------------------

if [ ! -f "$STATE_FILE" ]; then
    # First ever run, or the state was cleared. We have no idea how long the
    # agent has been down, so start the clock and do nothing.
    log "no state file yet; recording the time and taking no action"
    write_state "$now" "false" "" ""
    exit 0
fi

last_seen=$(pyjson "$STATE_FILE" "int(d['last_seen'])")
if [ -z "$last_seen" ]; then
    log "state file unreadable; resetting the clock and taking no action"
    write_state "$now" "false" "" ""
    exit 0
fi

silence=$(( now - last_seen ))
if [ "$silence" -lt "$SILENT_SECONDS" ]; then
    log "silent for $(( silence / 60 )) min; threshold is $(( SILENT_SECONDS / 60 )) min"
    exit 0
fi

gate_known=$(pyjson "$STATE_FILE" "'true' if d.get('learning_open') else 'false'")
if [ "$gate_known" != "true" ]; then
    # The guard refuses every write while the gate is shut, so any non-default
    # thresholds are the owner's, not the agent's. Do not override a person.
    log "silent for $(( silence / 3600 )) h, but the learning gate was closed at last contact; not resetting"
    exit 0
fi

# --- thresholds -------------------------------------------------------------

DEFAULT_START=$(pyjson "$STATE_FILE" "d.get('default_start')")
DEFAULT_STOP=$(pyjson  "$STATE_FILE" "d.get('default_stop')")
[ -n "$DEFAULT_START" ] || DEFAULT_START="$FALLBACK_START"
[ -n "$DEFAULT_STOP" ]  || DEFAULT_STOP="$FALLBACK_STOP"

cfg=$(curl -s -m 10 "${DASHBOARD}/config" 2>/dev/null)
if [ -z "$cfg" ]; then
    log "could not read ${DASHBOARD}/config; leaving thresholds alone"
    exit 1
fi

mep_start=$(printf '%s' "$cfg" | pyjson - "d['config']['mep803a']['startVoltage']")
mep_stop=$(printf  '%s' "$cfg" | pyjson - "d['config']['mep803a']['stopVoltage']")
kub_start=$(printf '%s' "$cfg" | pyjson - "d['config']['kubota']['startVoltage']")
kub_stop=$(printf  '%s' "$cfg" | pyjson - "d['config']['kubota']['stopVoltage']")

if [ -z "$mep_start" ] || [ -z "$kub_stop" ]; then
    log "could not parse thresholds from /config; leaving them alone"
    exit 1
fi

at_default=$(python3 -c "
vals = [float('$mep_start'), float('$mep_stop'), float('$kub_start'), float('$kub_stop')]
want = [float('$DEFAULT_START'), float('$DEFAULT_STOP')] * 2
print('yes' if all(abs(a - b) < 0.05 for a, b in zip(vals, want)) else 'no')
" 2>/dev/null)

if [ "$at_default" = "yes" ]; then
    log "agent silent $(( silence / 3600 )) h, but thresholds already at ${DEFAULT_START}/${DEFAULT_STOP}; nothing to do"
    exit 0
fi

log "agent silent $(( silence / 3600 )) h and thresholds are MEP ${mep_start}/${mep_stop}, Kubota ${kub_start}/${kub_stop} - resetting to ${DEFAULT_START}/${DEFAULT_STOP}"

reset=$(curl -s -m 15 -G "${DASHBOARD}/config" \
    --data-urlencode "mep.startVoltage=${DEFAULT_START}" \
    --data-urlencode "mep.stopVoltage=${DEFAULT_STOP}" \
    --data-urlencode "kub.startVoltage=${DEFAULT_START}" \
    --data-urlencode "kub.stopVoltage=${DEFAULT_STOP}" \
    --data-urlencode "src=watchdog" 2>/dev/null)

if [ -z "$reset" ]; then
    log "reset request failed"
    exit 1
fi

new_mep=$(printf '%s' "$reset" | pyjson - "d['config']['mep803a']['startVoltage']")
log "thresholds reset; MEP start now ${new_mep}"

# One message per silent episode, not one every 30 minutes.
if [ ! -f "${STATE_FILE}.notified" ]; then
    send_telegram "Agent silent 6 h — thresholds reset to ${DEFAULT_START} / ${DEFAULT_STOP}"
    date +%s > "${STATE_FILE}.notified"
else
    log "already notified for this episode; not repeating"
fi
