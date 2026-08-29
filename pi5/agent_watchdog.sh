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
# HOW THE AGENT IS DETECTED
#
# SPEC section 9 suggests finding the agent's last /config write in an access
# log, marked either by an "X-Agent: solar-agent" header or by "&src=agent".
# Neither is available on this Pi5: gunicorn runs without --access-logfile
# (see solar-dashboard.service), so it writes no HTTP access lines at all, and
# enabling them would mean editing the unit and restarting the dashboard.
#
# So liveness is taken from the agent itself. The agent serves GET /plan on
# KAMRUI:8090 and records a plan every tick (every 15 minutes). If that
# endpoint is unreachable, or its newest plan is older than 6 hours, the agent
# is silent. The agent still sends src=agent and the X-Agent header on every
# write, so if access logging is ever switched on, LOG_CHECK below will find
# them and report; it is diagnostic only.

set -uo pipefail

DASHBOARD="http://127.0.0.1:8080"
AGENT_PLAN="http://192.168.3.152:8090/plan"
SILENT_SECONDS=$((6 * 3600))
DEFAULT_START="52.0"
DEFAULT_STOP="54.5"
STATE_FILE="/home/michael/solar_dashboard/.agent_watchdog_state"

# Telegram: reuse the dashboard's own credentials rather than storing a copy.
DASH_CONFIG="/home/michael/solar_dashboard/config.json"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

json_get() {  # json_get <file-or-"-"> <python-expression over d>
    python3 -c "
import json,sys
d = json.load(open(sys.argv[1])) if sys.argv[1] != '-' else json.load(sys.stdin)
try:
    print($2)
except Exception:
    print('')
" "$1" 2>/dev/null
}

send_telegram() {
    local text="$1" token chat
    [ -f "$DASH_CONFIG" ] || { log "no $DASH_CONFIG; cannot send Telegram"; return; }
    token=$(json_get "$DASH_CONFIG" "d['telegram']['token']")
    chat=$(json_get "$DASH_CONFIG" "d['telegram']['chatId']")
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

# --- is the agent alive? ----------------------------------------------------

now=$(date +%s)
plan_json=$(curl -s -m 10 "$AGENT_PLAN" 2>/dev/null)
plan_ts=""
if [ -n "$plan_json" ]; then
    plan_ts=$(printf '%s' "$plan_json" | json_get - "int(d['ts'])")
fi

if [ -n "$plan_ts" ]; then
    age=$(( now - plan_ts ))
    if [ "$age" -lt "$SILENT_SECONDS" ]; then
        log "agent alive: newest plan is $((age / 60)) min old"
        rm -f "$STATE_FILE"
        exit 0
    fi
    log "agent reachable but its newest plan is $((age / 3600)) h old"
else
    log "agent did not answer $AGENT_PLAN"
fi

# Diagnostic only: if gunicorn access logging is ever enabled, say when the
# agent last wrote. Absence of these lines is expected and is not the trigger.
LOG_CHECK=$(journalctl -u solar-dashboard --since "6 hours ago" --no-pager 2>/dev/null \
            | grep -c 'src=agent' || true)
log "agent-marked writes seen in the last 6 h of the journal: ${LOG_CHECK:-0}"

# --- thresholds -------------------------------------------------------------

cfg=$(curl -s -m 10 "${DASHBOARD}/config" 2>/dev/null)
if [ -z "$cfg" ]; then
    log "could not read ${DASHBOARD}/config; leaving thresholds alone"
    exit 1
fi

mep_start=$(printf '%s' "$cfg" | json_get - "d['config']['mep803a']['startVoltage']")
mep_stop=$(printf  '%s' "$cfg" | json_get - "d['config']['mep803a']['stopVoltage']")
kub_start=$(printf '%s' "$cfg" | json_get - "d['config']['kubota']['startVoltage']")
kub_stop=$(printf  '%s' "$cfg" | json_get - "d['config']['kubota']['stopVoltage']")

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
    log "agent silent, but thresholds are already at the defaults; nothing to do"
    rm -f "$STATE_FILE"
    exit 0
fi

log "agent silent and thresholds are MEP ${mep_start}/${mep_stop}, Kubota ${kub_start}/${kub_stop} - resetting"

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

new_mep=$(printf '%s' "$reset" | json_get - "d['config']['mep803a']['startVoltage']")
log "thresholds reset; MEP start now ${new_mep}"

# One message per silent episode, not one every 30 minutes.
if [ ! -f "$STATE_FILE" ]; then
    send_telegram "Agent silent 6 h — thresholds reset to ${DEFAULT_START} / ${DEFAULT_STOP}"
    date +%s > "$STATE_FILE"
else
    log "already notified for this episode; not repeating"
fi
