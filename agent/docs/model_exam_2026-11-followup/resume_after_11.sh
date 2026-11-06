#!/bin/bash
# The questions left after the 8:34 pm stop, in the quiet just after a tick. The
# 12,000-token one must not share the server with one: it lost the 8:33 tick.
D=/tmp/abexam-followup
cd ~/mercalde-solar/agent || exit 1
START=$(date -d "2026-11-05 23:00:00" +%s)
while [ "$(date +%s)" -lt "$START" ]; do sleep 20; done
LAST=$(date -d "2026-11-05 23:30:00" +%s)
until journalctl -u solar-agent --since "2026-11-05 23:00:00" --no-pager | grep -q "tick done"; do
  [ "$(date +%s)" -lt "$LAST" ] || { echo "$(date '+%F %T') no tick after 11 pm; not resumed" >> $D/events.log; echo rc=no-tick > $D/exit; exit 0; }
  sleep 5
done
mkdir -p $D/aborted-2
cp $D/rows.jsonl $D/events.log $D/aborted-2/
grep -v '"answered_by": "model_error"' $D/aborted-2/rows.jsonl > $D/rows.jsonl
rm -f $D/exit
NOW=$(date +%s)
timeout -k 5 600 venv/bin/python /tmp/followup_exam.py --dir $D --url http://127.0.0.1:8080/v1/chat/completions \
  --stop-asking $((NOW + 420)) > $D/out2.log 2>&1
echo rc=$? > $D/exit
