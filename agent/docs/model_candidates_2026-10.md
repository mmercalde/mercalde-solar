# Model candidates: serial A/B on KAMRUI, 2026-11-03

Gemma 4 E4B and Qwen3.5-4B against the live Qwen3-8B, one llama-server at a
time, on recorded ticks. Measured only; nothing was deployed. The filename keeps
the `2026-10` from the brief; the runs were on 2026-11-03, 18:16–19:26 PST.

Trimmed candidate logs are in `model_candidates_2026-10/`.

## Results

| model | build | GPU offload | prefill tok/s (cold / warm) | avg tokens re-prefilled per tick | avg tick wall time (all / warm) | gen tok/s | peak GTT | clean % | invalid | unsourced | missed | narrated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Gemma 4 E4B, 20 ticks | 10499 `6d0549831` | 43/43 layers; "no usable GPU" 0; CPU 19.5% avg, 55% max | 98.3 / 89.4 | 625 | 10.7 s / 8.7 s | 11.8 | 7.68 GB | 100 | 0 | 0 | 0 | 0 |
| Qwen3.5-4B, **7 of 20 ticks** | 10499 `6d0549831` | 33/33 layers; "no usable GPU" 0; CPU 18.8% avg, 47% max | 109.0 / 95.3 | about 930 (541 per request) | 25.6 s / 18.6 s | 10.7 | 5.89 GB | 100 | 0 | 0 | 0 | 0 |
| Qwen3-8B (incumbent), 20 ticks | 10499 `6d0549831` | 37/37 layers; "no usable GPU" 0; CPU mostly 0–2%, 50% max | 50.4 / 39.0 | 627 | 21.7 s / 17.8 s | 7.4 | 9.35 GB | 100 | 0 | 0 | 0 | 0 |

The same 7 ticks, for a fair look at the partial run:

| model | avg wall (all / warm) | tool calls | clean |
|---|---|---|---|
| Gemma 4 E4B | 14.4 s / 8.8 s | 0 | 7/7 |
| Qwen3.5-4B | 25.6 s / 18.6 s | 2 | 7/7 |
| Qwen3-8B | 28.5 s / 17.4 s | 0 | 7/7 |

How to read the table:

- **Ticks.** All runs used the same pinned ticks, 2026-11-03 1:28 pm to 6:13 pm.
  Qwen3.5-4B's 7 are the first 7 of those 20.
- **Tokens per tick.** The first tick is excluded as cold. Qwen3.5-4B's figure
  divides all warm prompt tokens by 7: its 6 completed warm ticks plus the tick
  lost to the kill. Those include tool-call rounds, so it runs high.
- **Peak GTT.** Raw readings. With llama-server stopped, GTT reads 1.73 GB, so
  the models account for about 5.95, 4.16 and 7.62 GB respectively.
- **Prompt size.** The cold prompts were 4,513–4,915 tokens, not the ~6,400 the
  brief assumed.
- **CPU%.** 100% is one core. Candidates are sampled over a cold prefill and the
  incumbent over a warm one, so the averages are not like for like. All three
  peak around half a core, far below the 657% recorded for the CPU fallback on
  2026-09-06.
- **Offload counts.** "offloaded" does not appear at the default log level, so
  the counts come from a load-only pass of each model at `-lv 4`.

## What these ticks cannot show

- **Tool calling is almost untested.** Gemma and the incumbent made 0 tool calls
  in 20 ticks; Qwen3.5-4B made 2 in 7.
- **`missed` cannot separate these models.** No POLICY rule fired on any of the
  20 ticks.
- **Clean scores are therefore weak evidence.** They mostly show that each model
  can say "no change" on a quiet afternoon. See the follow-up tick set below.
- **Not scored: answer length.** All three include a `recommend:` line, but
  Qwen3.5-4B averages 537 characters against 95 (Gemma) and 51 (incumbent), and
  often opens with prose before the line.

## Expected vs. measured

- **Qwen3.5-4B did not re-prefill every tick.** The brief expected `forcing full
  prompt re-processing`. It logged 0 such lines and re-prefilled about 541 tokens
  per request after the cold prefill. Because of that, `--swa-full` was not tried.
- **`--cache-reuse` is disabled on both candidates.** Both log `cache_reuse is not
  supported by this context, it will be disabled`, including the dense Gemma. The
  prompt prefix cache still works on both, which is what keeps warm ticks short.
- **Sampling flags are mostly overridden.** `llm.py` sends `temperature: 0.2` on
  every request, so `--temp 1.0` and `--temp 0.6` had no effect. `--top-k` and
  `--top-p` were not overridden.

## Temperature

Temperature limited this test more than speed did.

- **Normal live ticks already run hot.** All 24 ticks from 11:00 to 17:00 today
  spent 13–17 s above 75 °C, peaking at 81.5 °C. A separate 13 s run at 16:00,
  outside any tick, reached 83.9 °C; its cause was not looked into.
- **The original limit could not be met.** At 75 °C for 10 s, Gemma was killed
  14 s into its first prefill. Michael raised the limit to 85 °C for 10 s.
- **Continuous load reaches about 85 °C.** Back to back, Gemma was killed at
  85 °C after 8 ticks.
- **Pauses were added to finish the runs.** Gemma and the incumbent ran with 30 s
  between ticks and Qwen3.5-4B with 45 s. Wall times exclude the pauses. This
  differs from the 15-minute gap in production, where the box has far longer to
  cool.
- **Qwen3.5-4B never finished.** Its first attempt was killed 28 s into the eval
  with no ticks scored, and the probe had left the box warm 11 s earlier. A
  cool-start retry reached 7 ticks before the second kill. I stopped there rather
  than retry again.

Peak Tctl in each scored run: Gemma 85.1 °C, Qwen3.5-4B 85.4 °C, incumbent 85.0 °C.
Whether a model that finishes its work sooner runs cooler per tick in production
was not measured.

Kill record, from the guard log:

```
18:16:53 tctl over limit: 77500
18:17:03 KILL pid=2252760: Tctl above 75 C for 10s (now 80750)
18:33:27 tctl over limit: 85125
18:33:36 tctl over limit: 85125
18:33:46 KILL pid=2278567: Tctl above 85 C for 10s (now 85125)
19:12:26 tctl over limit: 85125
19:12:36 KILL pid=2352030: Tctl above 85 C for 10s (now 85375)
19:24:10 tctl over limit: 85125
19:24:20 KILL pid=2357853: Tctl above 85 C for 10s (now 85375)
```

Swap stayed at or below 0.80 GiB during all runs. It read 1.93 GiB beforehand,
because the model downloads pushed idle pages out; 1.25 GB of that belonged to the
old live server and cleared when it stopped. Neither generator ran during the test.

## Live server impact

Live was stopped five times. Two agent ticks ran with the model down (18:43 and
19:13); each recorded a plan with an empty answer and "no change".

| down | up (`/health` ok) | why |
|---|---|---|
| 18:16:32 | 18:19:03 | Gemma, killed at 75 °C |
| 18:31:36 | 18:35:40 | Gemma, killed at 85 °C |
| 18:36:49 | 18:52:46 | Gemma, completed |
| 19:10:31 | 19:14:34 | offload probe, then Qwen3.5-4B, killed |
| 19:15:02 | 19:26:14 | Qwen3.5-4B retry, killed |

**Separate fix for later: each `systemctl start llama-server` waits 90 s.** The
first restart took 2.5 minutes in total. The unit has `After=` and `Wants=` on
`dev-dri-renderD128.device`, that device unit never appears on this box, and the
start job waits for systemd's device timeout: `systemctl list-jobs` showed the
device job running and the service waiting, and the start went through at exactly
90 s (18:17:18 to 18:18:48). The `ExecStartPre` poll is not what waits, since the
node already exists. `kamrui_stability.md` anticipated this device unit not
appearing. The unit was not changed.

Final state at 19:30: `llama_gpu_check` exit 0 (8,915 MiB GTT), `/health` ok, and
the agent's 19:28 tick completed in 97.8 s with a cold prefill of 4,748 tokens. GTT
reads 9.35 GB raw rather than the ≈7.2 GB in the brief, about 7.6 GB above the
1.73 GB idle level. It read 9.35 GB before the test began too, so the difference
predates this work. The unit file is unchanged, and the temporary sudoers rule is
removed.

## Method and deviations from the brief

- **No build.** Build 10499 is from 2026-08-19 and has both `gemma4` and `qwen35`,
  so candidates ran from the live binary as separate processes on port 8082.
- **Downloaded with `curl`.** `-hf` would have started a second llama-server, and
  `huggingface_hub` is not installed. Both SHA-256 checksums matched.
- **Run order changed by Michael:** Gemma, then incumbent, then Qwen3.5-4B.
- **Pinned ticks.** `model_eval.py` takes the newest N plans, and the agent records
  one every 15 minutes even while the model is down, so an uncommitted wrapper
  pinned all runs to plans at or before one timestamp and timed each tick.
  `model_eval.py` itself is unchanged.
- **`--timeout 600`** instead of 180, so one slow request could not abort a run.
- **The incumbent's eval had the 85 °C limit too.** It would have stopped the eval,
  not the server. It did not trigger.
- **A real agent tick fell inside the incumbent run** at 18:58, so that server was
  handling one extra request. Candidates had no such traffic.
- **`get_weather` answers for now, not for the tick**, as `model_eval.py` documents,
  so that one tool's results differ between runs.

## Faults

`model_eval.faults()` for each eval, with no limit on ticks, returned an empty list
for all three models, so there is nothing to quote.

Gemma's earlier unpaced attempt scored 8 ticks with 0 faults. Its rows are not in
the table.

## Verdict

Gemma 4 E4B is the only candidate that was both faster per tick and at least as
clean as the incumbent: 8.7 s against 17.8 s on warm ticks, 48 s against 95 s cold,
and 100% clean for both over 20 ticks, using about 1.7 GB less GTT. Qwen3.5-4B
completed only 7 ticks; on those it was 100% clean and quicker cold (68 s against
95 s), but no faster warm (18.6 s against 17.4 s), so it does not meet the test on
this evidence. The evidence is limited: these were quiet afternoon ticks with no
rule firing and almost no tool calls, so tool-calling and rule-handling, which are
what would most justify a change, remain untested. Whether to deploy anything is
Michael's call.

## Follow-up: pinned tick set for Gemma vs. the incumbent

Not run. Requested for another day.

**Only 1 tick qualifies, so 20 could not be picked from that pool.** Of 1,342 plans
in the last 14 days, 1 had a POLICY rule fire, and the same tick made the only write
and the only tool call by a tick. The other 14 logged tool calls in that period came
from questions, not ticks. Tool use comes from the `INFO tools tool` lines in the
`solar-agent` journal, because plans do not store it.

The set is that tick plus 19 evening ticks that do **not** meet the criteria, chosen
as the closest available: 14 with both generators running, and 5 at low pack voltage
just before a run or at the lowest point of a quiet night.

| # | `plans.ts` | when (local) | V | class |
|---|---|---|---|---|
| 1 | 1793419109 | 10-30 20:58 | 53.74 | low voltage, before a run |
| 2 | 1793420009 | 10-30 21:13 | 53.58 | generators running |
| 3 | 1793420909 | 10-30 21:28 | 55.56 | generators running |
| 4 | 1793421809 | 10-30 21:43 | 56.05 | generators running |
| 5 | 1793422709 | 10-30 21:58 | 56.47 | generators running |
| 6 | 1793423609 | 10-30 22:13 | 56.91 | generators running |
| 7 | 1793505509 | 10-31 20:58 | 53.69 | low voltage, before a run |
| 8 | 1793506409 | 10-31 21:13 | 53.63 | **qualifies: POLICY 4 top-up fired, `set_gen_thresholds` called, write made** |
| 9 | 1793507309 | 10-31 21:28 | 53.57 | low voltage, just before a run |
| 10 | 1793508209 | 10-31 21:43 | 55.30 | generators running |
| 11 | 1793509109 | 10-31 21:58 | 55.69 | generators running |
| 12 | 1793510009 | 10-31 22:13 | 56.24 | generators running |
| 13 | 1793510909 | 10-31 22:28 | 56.62 | generators running |
| 14 | 1793589209 | 11-01 19:13 | 53.74 | low voltage, before a run |
| 15 | 1793590109 | 11-01 19:28 | 53.88 | generators running |
| 16 | 1793591009 | 11-01 19:43 | 55.46 | generators running |
| 17 | 1793591909 | 11-01 19:58 | 55.95 | generators running |
| 18 | 1793592809 | 11-01 20:13 | 56.41 | generators running |
| 19 | 1793593709 | 11-01 20:28 | 56.84 | generators running |
| 20 | 1793689109 | 11-02 22:58 | 54.88 | lowest voltage of a quiet night |

Things to settle before running it:

- **The prompts expire.** `eval_retention_days` is 14, so tick 1 loses its prompt on
  **2026-11-13 at 19:58 PST**, and the qualifying tick on 2026-11-14 at 20:13 PST. Run it
  before then, or raise the retention first.
- **It still has only one fired rule.** `missed` will rest on a single tick, and the
  generator runs were all logged as `manual`. Waiting for more nights where a rule
  fires would give a stronger test, but those would be different ticks.
- **Blackout window.** The follow-up must run outside 20:30–23:00, like this one.
- **Thermal limit.** At 85 °C for 10 s it needs the same pauses between ticks.
