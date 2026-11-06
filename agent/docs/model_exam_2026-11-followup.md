# Follow-up exam: Gemma 4 E4B on the live server, after the tool fixes

Run 2026-11-05 on KAMRUI at commit `f1b7a46`, on the live server (port 8080). Compared against the Gemma column of [the first exam](model_exam_2026-11.md). Raw results are in [model_exam_2026-11-followup/](model_exam_2026-11-followup/).

**In short: the tool fixes worked. All five questions they targeted are now right, and Gemma scored 21 of 24 graded attempts.** The 3 failures are one case: asked in English for yesterday's solar, Gemma read October's total from the wrong tool all three times. Running the exam on the live server also caused real harm: **it failed one of the owner's Telegram questions and one real tick.** No write or POLICY decision was lost.

## Results

| | First exam | Follow-up |
|---|---|---|
| Questions | 29 | 44 |
| Graded attempts passed | 3 of 9 | 21 of 24 |
| Answered by Python | 3 | 3 |
| Ungrounded retries / `/data` fallbacks | 0 / 0 | 0 / 0 |
| Model errors in the final results | 0 | 0 (2 discarded attempts, see below) |
| Unsourced numbers | 2 | 0 |
| Invalid tool calls / threshold proposals | 0 / 0 | 0 / 0 |
| Avg / max wall time per answer | 28.0 / 144.4 s | 29.1 / 200.4 s |
| Tool calls per answer | 1.15 | 1.05 |
| Peak Tctl | 85.25 °C | 85.0 °C |

The wall times aren't like for like: this run shared its server with real ticks, and the first had a server to itself. The maximum is question 16, asked alone at 11:03 pm.

The two scores aren't like for like either. On the three cases both exams graded by model, 3 of 9 became 9 of 9, but 6 of those points come from correcting the graders, not from better answers. The real gains are in the new cases and extra questions below.

## How it ran

- **Not the brief's exact command.** `--exam` writes results only when the whole run ends, so a stop at 8:50 pm would have lost everything. [`followup_exam.py`](model_exam_2026-11-followup/followup_exam.py) calls the same `model_eval` functions in the same order, with `--repeat 3` and a 30 s pause, and saves each answer as it arrives. It lives outside the repo checkout, and no deployed code changed.
- **Stop rules I added.** It stops if the real agent logs "model unavailable". After the first stop I also made it wait for an idle server before each question. Heat stopped nothing, as asked.
- **Three sessions, one snapshot.** All questions were graded against the 7:50 pm snapshot.

| Session | Result |
|---|---|
| 7:50–7:52 pm | Stopped after the first question, which collided with the owner's Telegram question. Attempt discarded and asked again. |
| 7:54–8:34 pm | 42 answers, then question 16 collided with the 8:33 tick. Attempt discarded. Stopped by the rule, 16 minutes before the 8:50 limit. |
| 11:03–11:07 pm | The last 2 questions, started just after the 11:03 tick, by [`resume_after_11.sh`](model_exam_2026-11-followup/resume_after_11.sh). No failures. |

**Conditions that differ from the first exam:** the live unit has no `--temp 1.0 --top-k 64 --top-p 0.95` flags (the agent still sends temperature 0.2), and the server was shared.

**Not tested: the daylight caveat.** Every graded `tonight_shortfall` attempt ran after sunset, so the rule that a daylight reply must say "not final" never fired. It needs a daytime run.

## The fixes, on the questions they targeted

| # | Question | First exam | Follow-up | Result |
|---|---|---|---|---|
| 3 | Lowest voltage last night | 56.57 V (tonight's low) | 54.07 V, sunset to sunrise | **Fixed**, matches the database |
| 4 | Generator hours and fuel this week | 417 minutes, 1.46 gal | 7.0 h, "a modelled 4.54 gallons" | **Fixed**, matches the tool; see finding 2 |
| 5 | Will one run tonight, and the shortfall | "26,164 Wh" shortfall | No run, 0.0 kWh shortfall | **Fixed**: 24.7 kWh held, 14.1 kWh expected |
| 11 | Worst solar month this year | December 2025 | January 2026, 613.1 kWh | **Fixed** |
| 13 | Yesterday's solar, in Spanish | 33377 kWh | 31.3 kWh | **Fixed** |

## Graded cases

| Case | First | Follow-up | Note |
|---|---|---|---|
| `kubota_twice` | 0/3 | 3/3 | Grader corrected. No invented start this time. |
| `runtime_24h` | 0/3 | 3/3 | Grader corrected. **A pass the grader shouldn't give: see finding 2.** |
| `tonight_plan` | Python | Python | Says nothing about the model. |
| `voltage_at_247` | 3/3 | 3/3 | 54.56 V each time. |
| `last_night_low` | new | 3/3 | 54.07 V. |
| `tonight_shortfall` | new | 3/3 | Correct, after dark only. All three replies drop the caveat. |
| `worst_month_this_year` | new | 3/3 | January 2026. |
| `solar_yesterday_kwh` | new | **0/3** | 1025 kWh, see finding 5. |
| `solar_yesterday_kwh_es` | new | 3/3 | 31.3 kWh. |

## The other extra questions

| # | Question | First | Follow-up reply, shortened | Follow-up |
|---|---|---|---|---|
| 1 | Generator running, house draw | Right | Not running, 1756 W | Right; live value, not re-checked |
| 2 | Each array right now | Right | 0 W, plus two PV voltages | Right on the 0 W; not array by array |
| 6 | Why the MEP start is where it is | Partly | 52.0 V, owner's baseline 52.0/57.0, no rule fired | **Improved to right**: `guard_state.json` has that baseline |
| 7 | Why the MEP failed to start | Right | Never reached 52.0 V | Right, corrects the false premise |
| 8 | Voltage in January 2024 | Right | No sample within 5 minutes | Right |
| 9 | Cloud tomorrow | Right | 3% | Right, matches the tool |
| 10 | Years left on the pack | Right | 28.2 years, cycling dominant, calendar 29.7 | Right, matches the tool |
| 12 | Voltaje ahora | No answer | 56.2 V, "no sample exactly at 8:28 pm" | **Improved**, though the wording is awkward and it used `get_voltage_at` rather than `get_status` |
| 14 | Inversores y generadores | Right | Lists both, plus the Magnum MS4048 | Right; the tool lists the Magnum under the Kubota |
| 15 | Amperios-hora restantes | Right | 1706 Ah | Right; live value, not re-checked |
| 16 | Último cambio de umbrales | Right | 31 Oct 9:14 pm, refused, owner had changed them | Right, same answer, 200.4 s alone |
| 17 | CA en cada inversor | Right | 119.54 V and 119.53 V, 60.0 Hz | Right; live values, not re-checked |

## The two failures on the live server

| | 7:52 pm | 8:33 pm |
|---|---|---|
| Large request | The owner's Telegram "check the log" | Exam question 16 |
| Second request | Exam question 1 | The real tick |
| Tokens reached by each when it failed | 12,148 and 4,673 | 12,172 and 4,671 |
| Who lost | Both | Both |

**Effect on the system.** The owner's question failed. The 8:33 tick got no model reply and recorded "recommend: no recommendation returned; applied: no change". No POLICY rule fired on that tick, and the top-up was still held until 9:00 pm, so no write or decision was lost. Neither failure touched the 9 pm tick, which took 10.2 s.

**Cause: a shared context pool, and the exam was the second client.** The server runs 4 slots sharing one pool of 16,384 tokens (`n_slots = 4`, `kv_unified = 'true'`). Each pair needed more than that, and the server returned a 500 to both requests with "Context size has been exceeded".

**These were not read timeouts.** I checked, because a queued request running past `llm.py`'s 180 s was a reasonable explanation. Both log lines read `500 Server Error: Internal Server Error`. The requests were being processed at once, not queued, and the longest had run for 80 s. Since the switch, the only "timed out" lines in the agent's log are two unrelated Telegram polling timeouts.

**The agent can't do this to itself.** Ticks and questions share `self.lock` (`agent.py:620`, `agent.py:1050`), so the agent never sends two model requests at once. The collision needs a second client: the exam, the unlocked prompt-cache warm-up at startup (`agent.py:1466`), or anything else calling port 8080. `llm_probe.py` is another, when someone runs it by hand.

### How close one question gets

"Check the log" called `get_recent_actions(n=50)`, which returned 21,276 characters, or 8,670 tokens, the most that tool can return today. The 12,148 above is only how far the prompt had got when the pool ran out. Rendered in full:

| Tool result | Full prompt | With the 1,024-token reply allowance | Against 16,384 |
|---|---|---|---|
| `get_recent_actions(n=50)` | 14,387 | 15,411 | 973 to spare |
| `get_gen_runtime(days=7)` | 8,217 | 9,241 | 7,143 to spare |
| `get_gen_runtime(days=30)` | 10,533 | 11,557 | 4,827 to spare |
| `get_gen_runtime(days=90)`, also 180 and 365 | 20,065–20,071 | about 21,090 | **about 4,700 over** |

These are token counts of rendered prompts from the snapshot, not live requests. **The over-limit row is a prediction**: no oversized request was sent to the server.

## Ticks during the exam

| Tick | Time | Note |
|---|---|---|
| Usual, 2 pm to 7:45 pm | Median 9.8 s, max 10.5 s | 23 ticks |
| 7:48 pm | 106.8 s | After the restart, 105.4 s of it cache warm-up. Completed normally. |
| 8:03 pm | 56.2 s | Overlapped question 10 of the run, which took 64.3 s against about 15 s |
| 8:18 pm | 13.8 s | Overlapped |
| 8:33 pm | **Failed**, 41.6 s | Above |
| 8:48 pm | 50.2 s | No exam running. Probably the prefix lost in the failure; not confirmed. |
| 9:03 pm to 11:03 pm | 9.4–10.4 s | 9 ticks, all normal |

## Temperature

Logged from the flight recorder, as asked. Nothing stopped on heat.

| Window | Avg Tctl | Peak Tctl | Samples at 85 °C or above | Peak edge |
|---|---|---|---|---|
| 7:54–8:34 pm | 60.8 °C | 85.0 °C at 7:55:31 | 16 of 4,631 | 87 °C |
| 11:00–11:30 pm | 52.8 °C | 85.0 °C at 11:05:38 | 5 of 2,249 | 88 °C |

## Findings to fix later

None of these has been changed.

1. **`llm.py` calls every request failure "unreachable".** `except requests.RequestException` catches timeouts, HTTP errors and refused connections alike. Timeouts should say timeout. So should the real case tonight: a 500 with the server's reason, on a server that was reachable.
2. **`get_gen_runtime` leaves out exercise runs, so "neither generator ran" was wrong.** The Kubota's exercise ran for 14 minutes, starting at 6:48 pm (the plan lists its last run as 7:02 pm). All three `runtime_24h` replies said neither ran, and the grader passed them because its answer key excludes exercise too. Runtime should include exercise runs, labelled as exercise. `history.gen_runs` already has `include_exercise`, but three things must move with it: exercise runs must stay out of charge-rate learning, the fuel totals need a deliberate decision, and the `runtime_24h` key must change. It also touches question 4: the week is 417 minutes without exercise and 431 with it.
3. **Recommended, not done tonight: `-np 1` in `llama-server.service`,** so concurrent requests queue instead of getting a 500. Two costs to measure: ticks and questions would share one cache and may push out each other's prefix (a cold prefill took 105 s tonight), and a queued request could reach the 180 s limit, so timeouts become possible where tonight there were none.
4. **Tool results have no size limit.** `tools.py` only logs a warning above 6,000 characters. By the table above, `get_gen_runtime(days=90)` should fail with no second client, which `-np 1` won't fix. `get_recent_actions(n=50)` also leaves too little room for a second large tool call.
5. **Yesterday's solar in English fails 3 of 3.** "How many kWh of solar did we make yesterday?" called `get_monthly_summary(month=2026-10)` each time and gave October's 1025 kWh as yesterday's. The same question in Spanish called `get_history(window=yesterday)` and got 31.3 kWh 3 of 3. I haven't established why the two differ.
6. **Gemma drops the "Not final" caveat.** The tool's verdict at this hour ends "Not final: the top-up rule decides at 9:00 pm." None of the four replies to the shortfall question carried it. That's allowed after dark, but it suggests the daylight rule may fail when it's finally tested.
7. **The sun-up caveat reads oddly on a day with no shortfall** ("this will likely shrink", with nothing to shrink). This is an open wording decision for the owner.

## Lesson for the next exam

Don't run an exam on the live server while it can overlap a large question or a tick. Giving it a separate server, as the first exam had, or timing questions between ticks avoids this. Waiting for an idle server wasn't enough: it stopped the exam from starting on top of a tick, but not a tick from starting on top of the exam.
