# KAMRUI stability

The KAMRUI is the mini PC that runs `solar-agent` and `llama-server`. It froze
three times on 2026-08-30 and once on 2026-08-29. This is the record of what
was seen, so the week that follows can be judged against evidence rather than
recollection.

The Pi 5 was unaffected every time. It holds the generator thresholds and runs
the auto-start itself, so a dead KAMRUI costs the overnight top-up decision
and nothing else. The dashboard showed `{"online": false}` for the agent, as
designed.

## The freezes

Boot boundaries are from `journalctl --list-boots` on the KAMRUI. "Last kernel
line" is the final entry in that boot's `journalctl -k`.

| Boot | Ran | Ended | Last kernel line | Silent before death |
|---|---|---|---|---|
| −4 | Fri 2026-08-28 15:58:32 | Sat 2026-08-29 20:30:55 | 20:30:55 | none — an ordinary shutdown |
| −3 | Sat 2026-08-29 20:31:10 | **Sun 2026-08-30 04:01:27** | Sun 00:17:56 | **3 h 43 m** |
| −2 | Sun 2026-08-30 08:26:46 | **Sun 2026-08-30 15:04:49** | Sun 12:45:39 | **2 h 19 m** |
| −1 | Sun 2026-08-30 15:16:48 | **Sun 2026-08-30 15:19:13** | Sun 15:17:16 | **~2 m** |
| 0  | Sun 2026-08-30 15:29:08 | — | — | — |

### What was running each time

**04:01:27.** The 8B service and the agent, both idle-normal; no one was
working on the machine. This is the event first described as a "4:01 am
freeze"; the boot list shows it was a death, and the machine stayed off until
someone powered it on at 08:26:46. Its aftermath is the incident that produced
commit `e3cda6b`: on restart the old hourly heartbeat re-asserted stale stored
intent (Kubota 53.3/57.0) over the owner's 52/56 at 08:27 and 09:27, starting
the Kubota twice in full sun. That fault is fixed and is unrelated to the
freeze itself.

**15:04:49.** The 14B (`Qwen3-14B-Q4_K_M.gguf`) had been started on port 8080
in place of the 8B for a model A/B, `solar-agent` had been restarted against
it, and one warm-up question was in flight. Earlier in the same boot, at
**12:45:36**, the kernel logged a full out-of-memory dump — `Free swap = 0kB`
against `Total swap = 2097148kB`, followed by
`[drm:amdgpu_cs_ioctl] *ERROR* Not enough memory for command submission!` —
during a repository deployment and a `pytest` run. **The machine survived that
by 2 h 19 m.** The OOM is real and worth fixing, but it is not what killed the
box.

**15:19:13.** The 8B service only, on the stock unit, with the agent running an
ordinary tick about two and a half minutes after boot. 15 GB was free. No 14B
was running. This is the observation that rules out the 14B as the cause.

### The signature

Every death looks the same: the kernel logs normally, then stops, and the
machine goes minutes to hours later without another word. Across all three
there is **no panic, no OOM-kill at the moment of death, no `amdgpu` ring
timeout or GPU hang, and no MCE**. A software crash leaves a trace; these
leave none. That pattern is a power loss or an abrupt hardware halt.

### The blind spot

There is no temperature telemetry on the machine. `sensors` is not installed,
every boot logs
`pcie_mp2_amd 0000:03:00.7: Failed to discover, sensors not enabled`, and the
thermal zones read empty. A thermal cutout looks exactly like what was
observed, and cannot currently be distinguished from it. Installing
`lm-sensors` and running `sensors-detect` would attach a temperature to the
next death.

## The watchdog

The hardware watchdog was tested and **confirmed non-functional** — it did not
reset the machine at any of the three freezes, which is why each one needed a
manual power cycle. Its configuration has been removed rather than left in
place giving false assurance. *(Reported by the owner; not independently
verified here.)*

## The llama-server flag change

Made after the third freeze, in case the memory and GPU pressure of the old
flags was contributing.

| | Before | After |
|---|---|---|
| context | `-c 32768` | `-c 16384` |
| flash attention | `-fa on` | *(removed)* |
| KV cache | `-ctk q8_0 -ctv q8_0` | *(removed — unquantised)* |
| bind | `--host 0.0.0.0` | *(removed — binds localhost)* |
| unchanged | `-ngl 99 --port 8080 --jinja` | same |

Halving the context reduces the KV cache; dropping `-ctk/-ctv q8_0` enlarges
each cache entry, so the two changes pull against each other and the net
footprint is worth measuring rather than assuming. Dropping `--host 0.0.0.0`
means the model server is no longer reachable from the LAN; the agent talks to
`127.0.0.1:8080` and is unaffected.

Observed tick time on the new flags: **66 s** *(reported by the owner)*, against
38–51 s previously. Worth watching: a slower tick is the expected cost of
unquantised KV, but a tick that keeps growing is its own signal.

## What would settle it

- A week without a freeze on the current flags. Until then, no model
  experiments on this box: the A/B was cancelled part-way, with Pass A
  (Qwen3-8B) scoring 2/4 on `model_eval.py --exam` and Pass B never run.
- `lm-sensors` installed, so the next death has a temperature attached.
- More swap than 2 GB, or none at all. 2 GB on a 22 GB machine is enough to
  thrash and not enough to save anything; it was fully exhausted at 12:45 on
  boot −2.
- If it freezes again on the 8B, disabling `llama-server` at boot and seeing
  whether the machine stays up separates the model server from the hardware.

## Running record

Add a line per event. An empty table after 2026-09-06 is the result we want.

| Date | Event | What was running | Kernel trace | Notes |
|---|---|---|---|---|
| 2026-08-29 | freeze 04:01:27 | 8B + agent, idle | none | manual power cycle |
| 2026-08-30 | freeze 15:04:49 | 14B + agent, A/B warm-up | none at death; OOM at 12:45 same boot | manual power cycle |
| 2026-08-30 | freeze 15:19:13 | 8B + agent, normal tick | none | manual power cycle; flags changed after |
| 2026-09-06 | freeze 14:16:23 | ended the 7-day boot; owner reports a 14B loaded beside the 8B | not yet captured | manual power cycle |
| 2026-09-06 | freeze 14:58:15 | owner reports normal stack only | not yet captured | survived 15 m from boot |
| 2026-09-06 | freeze 15:20:12 | owner reports normal stack only | not yet captured | survived 15 m from boot |
| 2026-09-06 | freeze 20:20:28 | not established | not yet captured | survived 14 m from boot |
| 2026-09-06 | freeze 20:28:33 | not established | not yet captured | died inside the same minute it booted |
| 2026-09-06 | freeze 20:38:07 | not established | not yet captured | survived 5 m; box still down at 20:52 |

## 2026-09-06 — six freezes, and the shape of them

The week the previous section asked for did not hold. The 2026-08-30 15:29:08
boot ran **seven days** and ended at 14:16:23. Five more deaths followed the
same day.

| Boot | Ran from | Ended | Survived |
|---|---|---|---|
| −5 | Sun 2026-08-30 15:29:08 | Sun 2026-09-06 14:16:23 | 7 days |
| −4 | 14:43:12 | 14:58:15 | **15 m** |
| −3 | 15:05:03 | 15:20:12 | **15 m** |
| −2 | 20:06:16 | 20:20:28 | **14 m** |
| −1 | 20:28:33 | 20:28:33 | **under 1 m** |
| 0 | 20:33:09 | 20:38:07 | **5 m** |

Two things in that table were not visible on 2026-08-30.

**It is accelerating.** August's survivals were 3 h 43 m, 2 h 19 m, 2 m.
Today's are 15 m, 15 m, 14 m, <1 m, 5 m.

**Survival tracks how long the box had been off, not what was running.** The
three boots that followed a gap of twenty minutes or more each lasted about
fifteen minutes. The two that followed a restart minutes after a death lasted
under one minute and five minutes. A configuration fault does not care how
warm the case is; that asymmetry is the single most discriminating fact in the
record so far, and it points at a threshold being reached — sooner when the
machine starts closer to it.

Against that: the owner holds that the box was well until today's model work
and that nothing was reverted after the reboot. That mechanism is real and is
**not yet excluded** — the live `llama-server` unit on the KAMRUI has not been
read since. It is the first thing to capture on the next boot. Note also that
the committed `agent/llama-server.service` carries `--host 0.0.0.0`, which the
section above records as having been *removed* after the third August freeze:
the tracked config and the written record already disagree, so drift on this
box is demonstrated rather than hypothetical.

Weighing against a purely-today cause: the 2026-08-29 04:01 freeze was 8B-only
and idle with nobody at the machine, a week before today's work.

### Not site power, and not the generators

Checked while the box was down, so it need not be checked again:

- `pve-zeus` (.128) and `vm101` (.177) both held **1 d 6 h** uptime across
  every one of today's freezes, with no reboots.
- The Pi 5 held **78 days**. Generator control was never at risk.
- Other LAN addresses reading dark are expected: `.127` is dark because Zeus
  is booted into Proxmox at `.128`, and the migrated rigs' bare-metal
  addresses are empty by design.

### Found: llama-server was running on the CPU

Resolved 2026-09-06 21:32. The cause was not the model, the RAM or the
cooling. `llama-server` was doing **CPU inference**, and had been since every
boot today.

```
warning: no usable GPU found, --gpu-layers option will be ignored
prompt processing, n_tokens = 2048, t = 58.68 s / 34.90 tokens per second
W srv          stop: cancel task, id_task = 0
```

`-ngl 99` was silently ignored.

The speed difference alone does not explain the damage. Measured prefill is
**34.9 tok/s on the CPU against 38.5 tok/s on the GPU** — the Vega iGPU in
this part is barely faster, and an earlier claim in this file of "roughly 50x"
was wrong. What matters is the **prompt cache**. A full agent prompt is
~6 400 tokens. On the GPU the first tick finishes, the cache populates, and
every tick after it evaluates only ~360 new tokens and completes in ~43 s. On
the CPU the first tick ran past the agent's timeout and was cancelled — and a
cancelled tick never populates the cache, so the next tick faced the whole
prefill again and also timed out. The retries never stopped, which is what
held every core busy and took the package to **84 °C**.

The margin is thin: 38.5 against 34.9 tok/s. This failed because it was just
slow enough to miss the first tick. If prompts keep growing, it can cross the
same cliff again *with* the GPU. Tick time is the early warning; ~43 s now,
against the 38–51 s recorded before.

**Why the GPU was unreachable.** `/dev/dri/renderD128` is `root:render` and
`michael` is in neither `render` nor `video`. Access came only from a
`logind` session ACL (`user:michael:rw-`), granted when someone logs in at the
console. The unit is `After=network.target`, so it starts *before* the
graphical login exists — no ACL, no device, CPU fallback for the life of the
process.

**Why the week of stability.** `/etc/systemd/system/llama-server.service` is
dated 2026-08-30 15:35:19, six minutes after the 15:29 boot: the flags were
changed and the service restarted **by hand from the desktop session**, where
the ACL already existed. It got the GPU, ran cool, and survived seven days.
Every boot after today's first crash auto-started it at boot instead.

**The fix.**

```bash
sudo usermod -aG render,video michael
sudo systemctl restart llama-server
```

Measured immediately after:

| | CPU fallback | GPU |
|---|---|---|
| GTT used | 89 MiB | **7 218 MiB** |
| llama-server CPU | **657 %** | **8.8 %** |
| Model load | ~35 s | **2.8 s** |
| Tctl | **84 °C** peak | **50.1 °C** |
| Load average | 7.94 | 0.10 |

### Also changed today, and one of them should be reconsidered

- **`amdgpu.gttsize=16384` removed** from `/etc/default/grub` (backup:
  `/etc/default/grub.bak-*`). This was done while GTT use read as 89 MiB and
  the parameter looked like an unreverted leftover. With the model actually on
  the GPU it uses **7.2 GB of GTT**, so 16384 was a reasonable setting for
  running a 14B. Default GTT is now 11 747 MiB, which is ample for the 8B.
  **Put it back before running the 14B again.**
- **Swap 2 GB → 10 GB** (`/swapfile8`, in fstab). Addresses the item flagged
  above. Untouched so far under normal load.
- **Flight recorder installed** — `kamrui-flightrec.service`, 1 Hz, fsynced to
  `/var/log/kamrui-flight.log`, enabled at boot. Closes the temperature blind
  spot permanently.

### What is not yet proven

The OOM at 14:11:31 is a real, separate event: `llama-server` killed holding
9.86 GB, the 14B, five minutes before the 14:16:23 death. That one is memory.
The other six deaths had no OOM and no trace, and are attributed to the CPU
fallback on the strength of the 84 °C peak, the sustained 650 % load, and the
cold-boot-versus-hot-restart survival asymmetry — not on a captured death.
**The recorder will settle it: if the box now runs indefinitely at 50 °C, the
attribution holds; if it freezes again, the log carries the last second.**
