"""Alarm when llama-server comes up without the GPU.

llama.cpp does not fail when it cannot open the render node. It prints

    warning: no usable GPU found, --gpu-layers option will be ignored

exits zero and serves happily on the CPU, so `systemctl status` reports
`active` while prefill runs at roughly a fiftieth of the speed. On 2026-09-06
that state had been reached on every boot since at least 2026-08-28 without
anything surfacing it: the agent's ticks timed out, retried, and held the
package at 84 C from boot. See docs/kamrui_stability.md.

The cause was that `/dev/dri/renderD128` is `root:render` and the service user
was in neither `render` nor `video`; access came only from the logind session
ACL, which does not exist yet when a boot-time service starts. The fix is
group membership plus `SupplementaryGroups=` on the unit. This script is the
tripwire that says so out loud if it ever regresses.

Two independent signals are checked, because either alone can mislead: the
warning may age out of the journal, and GTT can read low for a moment while
the model is still loading.
"""

import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import telegram  # noqa: E402

WARNING = "no usable GPU found"

# The 8B at -c 16384 settles around 7.2 GB of GTT. Anything under a gigabyte
# means the weights are not on the GPU: a CPU fallback parks at ~89 MiB.
GTT_FLOOR_BYTES = 1 << 30

# Loading the model off disk takes a couple of seconds warm, longer cold.
WAIT_SECONDS = 180
POLL_SECONDS = 5


def gtt_used_bytes():
    """Bytes of GTT in use, or None when the node cannot be read.

    Globs `card[0-9]*` rather than `card*` so DRM connector directories
    (card1-DP-1 and friends), which have no device/mem_info_* files, are not
    mistaken for the device itself.
    """
    for path in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/mem_info_gtt_used")):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def invocation_id():
    """The systemd invocation id of the running llama-server, or None."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "InvocationID", "--value", "llama-server"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def journal_says_fallback():
    """True when the *running* llama-server logged the fallback warning.

    Scoped to the current invocation, not the boot. A restart that fixes the
    problem leaves the old warning in the boot's journal, so matching on
    `-b` alone reports a fallback that has already been repaired.
    """
    invocation = invocation_id()
    if invocation is None:
        return None
    try:
        out = subprocess.run(
            ["journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return WARNING in out.stdout


def wait_for_model():
    """Give the server time to load before judging GTT. True if it got there."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        used = gtt_used_bytes()
        if used is not None and used >= GTT_FLOOR_BYTES:
            return True
        time.sleep(POLL_SECONDS)
    return False


def main():
    on_gpu = wait_for_model()
    used = gtt_used_bytes()
    fallback = journal_says_fallback()

    if on_gpu and not fallback:
        print(f"llama-server is on the GPU ({used / (1 << 20):.0f} MiB GTT)")
        return 0

    mib = "unknown" if used is None else f"{used / (1 << 20):.0f} MiB"
    why = []
    if fallback:
        why.append(f"the journal carries '{WARNING}'")
    if not on_gpu:
        why.append(f"GTT in use is {mib} after {WAIT_SECONDS}s")

    text = (
        "llama-server is running on the CPU.\n\n"
        + "; ".join(why)
        + ".\n\nPrefill drops from ~38 to ~35 tok/s. That sounds survivable "
        "and is not: it is enough to push the first tick past the agent's "
        "timeout, and a cancelled tick never populates the prompt cache, so "
        "every tick after it faces the full ~6400-token prefill and also "
        "times out. The retries hold every core busy and the package sits at "
        "84 C. Check that the service user is in the render and video groups "
        "and that the unit carries SupplementaryGroups=render video."
    )
    print(text, file=sys.stderr)

    cfg = config.load()
    if telegram.configured(cfg):
        telegram.send(cfg, telegram.escape(text))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
