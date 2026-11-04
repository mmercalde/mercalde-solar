#!/bin/bash
# The scheduled job. run.sh sleeps until 00:05 itself; this only starts the two
# processes, detached so they outlive the SSH session. `at` is not installed,
# and a crontab entry would be one more thing left on the box.
D=/tmp/abexam
setsid nohup python3 "$D/launch.py" "$D/run.sh" > "$D/run.out" 2>&1 < /dev/null &
R=$!
setsid nohup "$D/deadman.sh" "$R" > "$D/deadman.out" 2>&1 < /dev/null &
sleep 2
echo "runner pid=$R; dead-man pid=$!"
ps -o pid,etime,cmd -p "$R" -p "$!"
cat "$D/status"
