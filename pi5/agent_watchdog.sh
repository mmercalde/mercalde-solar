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
# with the learning gate, the configured defaults, and the four thresholds
# the agent last wrote, whenever the agent is up.
#
# WHAT IT WILL RESET
#
# Only thresholds the agent itself put there. /plan reports what it last
# wrote and that is cached with the rest of the state; if the live values
# differ from it, the owner moved them, and the watchdog logs "owner-set,
# leaving alone" and does nothing. A silent agent is a reason to undo the
# agent, never a reason to undo a person.

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

# Absolute limits on the pack, the same two numbers as agent/guard.py's
# HARD_START_FLOOR and HARD_STOP_CEILING. Deliberately a second copy: this
# script exists to act when the agent is not answering, so it cannot ask the
# agent what its limits are, and a watchdog that trusted the thing it watches
# would be no watchdog. A reset outside these is refused, whatever /plan said.
HARD_START_FLOOR="52.0"
HARD_STOP_CEILING="57.0"

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

write_state() {  # write_state <last_seen> <learning_open> <start> <stop> [intended-json]
    python3 - "$STATE_FILE" "$1" "$2" "$3" "$4" "${5:-}" <<'PY'
import json, sys
path, seen, gate, start, stop, intended = sys.argv[1:7]
state = {"last_seen": int(seen), "learning_open": gate == "true"}
if start:
    state["default_start"] = float(start)
if stop:
    state["default_stop"] = float(stop)
# What the agent last wrote to /config. A reset is only ever a reset of the
# agent's own thresholds; anything else in force is the owner's.
if intended:
    try:
        state["intended"] = json.loads(intended)
    except ValueError:
        pass
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f)
import os
os.replace(tmp, path)
PY
}

# intended_matches_live <state-file> <config-json>
#
# 0  every live threshold is what the agent last wrote - the agent's own
# 1  at least one differs - the owner's, and not the watchdog's to undo
# 2  the agent's last write is not recorded, so nothing can be shown
#
# This is the whole difference between resetting the agent's leftovers and
# overriding a person. On 2026-08-30 the owner set Kubota 54.6/56.6 by hand
# at 9:24 pm; six silent hours later a watchdog that only compared with the
# defaults would have written 52.0/56.0 over it and sent a Telegram saying
# the agent had gone quiet, which would have been true and beside the point.
intended_matches_live() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    live = json.loads(sys.argv[2])["config"]
except (OSError, ValueError, KeyError):
    sys.exit(2)
want = state.get("intended")
if not want:
    sys.exit(2)
try:
    have = {"mep_start": live["mep803a"]["startVoltage"],
            "mep_stop": live["mep803a"]["stopVoltage"],
            "kub_start": live["kubota"]["startVoltage"],
            "kub_stop": live["kubota"]["stopVoltage"]}
    sys.exit(0 if all(abs(have[k] - float(want[k])) < 0.05 for k in have)
             else 1)
except (KeyError, TypeError, ValueError):
    sys.exit(2)
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

within_hard_limits() {  # within_hard_limits <start> <stop>  -> 0 if both are inside
    python3 -c "
import sys
try:
    start, stop = float(sys.argv[1]), float(sys.argv[2])
except (ValueError, IndexError):
    sys.exit(1)
sys.exit(0 if start >= $HARD_START_FLOOR - 1e-9
              and stop <= $HARD_STOP_CEILING + 1e-9 else 1)
" "${1:-}" "${2:-}" 2>/dev/null
}

# Sourcing with WATCHDOG_LIB_ONLY=1 stops here, so the helpers above can be
# exercised without any of the network calls below.
if [ -n "${WATCHDOG_LIB_ONLY:-}" ]; then
    return 0 2>/dev/null || true
fi

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
    intended=$(printf  '%s' "$plan_json" | pyjson - "json.dumps(d['intended'])")
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
    write_state "$now" "$gate" "$def_start" "$def_stop" "${intended:-}"
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

# The agent reported these on /plan and they were cached; they are still not
# trusted past the hard limits. Refusing is right where clamping is not: a
# value outside them means the agent or the state file is wrong about
# something, and the thresholds already in force are safer than a guess.
if ! within_hard_limits "$DEFAULT_START" "$DEFAULT_STOP"; then
    log "refusing to reset to ${DEFAULT_START}/${DEFAULT_STOP}: outside the hard limits (start >= ${HARD_START_FLOOR}, stop <= ${HARD_STOP_CEILING}); leaving the thresholds alone"
    exit 1
fi

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

# Whose thresholds are these? Only the agent's own are the watchdog's to
# reset. Anything else in force was put there by the owner, and a silent
# agent is no reason to undo it.
intended_matches_live "$STATE_FILE" "$cfg"
case $? in
    0) ;;
    1)  log "agent silent $(( silence / 3600 )) h, but MEP ${mep_start}/${mep_stop}, Kubota ${kub_start}/${kub_stop} are not what it last wrote: owner-set, leaving alone"
        exit 0 ;;
    *)  log "agent silent $(( silence / 3600 )) h, and its last write is not recorded, so these thresholds cannot be shown to be its own: owner-set, leaving alone"
        exit 0 ;;
esac

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
