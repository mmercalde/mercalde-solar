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
