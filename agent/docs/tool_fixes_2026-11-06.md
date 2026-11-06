# Tool fixes from the follow-up exam, and the daylight check

2026-11-06. Brief: `claude-code-brief-tool-fixes-2.md`. Findings: `model_exam_2026-11-followup.md`.

All five fixes are in and deployed, plus a sixth change you asked for after the first check: `bae9e72` went live at 7:53 am. The suite passes, 1,185 tests. The size, yesterday's-solar and exercise fixes all worked live. The "Not final" description change did not: Gemma still left the sentence out. So Python now adds it, and the daylight re-ask passes.

| Fix | Commit | Live result |
|---|---|---|
| 1. Tool result size | `02d8836` | Worked: 90 days answered at 5,986 characters, the result that used to overflow |
| 2. Yesterday's solar | `34f0c3c` | Worked: 31.6 kWh, English and Spanish |
| 3. Exercise runs | `811d5df` | Worked: "the Kubota ran for 14.0 minutes" |
| 4. `llm.py` labels | `a0fd65c` | Not seen live: no model request failed. Tested with mocks. |
| 5. "Not final" wording and description | `2c72a91` | Tool right, reply still wrong. See below. |
| 5b. Python adds the sentence | `bae9e72` | Worked: daylight re-ask passes |

## The caveat: the description wasn't enough, so Python adds it

**First check, 7:38 am, description change only.** The tool's verdict was:

> Shortfall of 4.4 kWh: the pack holds 14.2 kWh above 52.0 V and about 18.6 kWh is expected out of it by sunrise. Not final: solar still to come will cover load and charge the pack, so this will likely shrink; the top-up rule decides at 9:00 pm.

Gemma replied:

> A generator run is expected tonight because the pack is projected to reach the 52.0 V start threshold at 3:04 am, and the projected shortfall is 4.4 kWh.

The grader failed it: right figure, no caveat. That made five replies out of five without the sentence, counting last night's four, which came before the description told it to quote the verdict. I never established why it drops the sentence.

**The change (`bae9e72`), as you specified.** In `answer()`, if the answer called `get_load_forecast`, the last `tonight.verdict` says "Not final", and the reply doesn't already say it, Python appends the sentence. English uses the verdict's own sentence. Spanish uses two strings in `tools.py`, with the time and the long or short form taken from the verdict. The grader and `answer()` now share one test, `NOT_FINAL_RE`, for whether a reply already says it, extended to Spanish.

**Re-ask, 8:08:47–8:09:12 am, right after the 10.3 s tick, sun up.** `tonight_shortfall`, "Will a generator run tonight, and what's the shortfall?", called `get_load_forecast(hours=24)` (1,581 chars) and took 24.9 s:

> A generator run is expected tonight because the pack is projected to reach the 52.0 V start threshold at 4:07 am, and the shortfall is 3.1 kWh. Not final: solar still to come will cover load and charge the pack, so this will likely shrink; the top-up rule decides at 9:00 pm.

**Pass:** it matches the 3,119 Wh shortfall and carries the sentence. The final sentence is word for word what Python adds, and the first sentence is the same shape as the 7:38 reply. It's very likely Python's, but I didn't capture the log line that would prove it. This is one attempt, in English. The Spanish path is covered by tests but hasn't been seen live.

**Two side effects of the rule as specified:**

- It applies to any answer that called `get_load_forecast`, not only shortfall answers. A question about the next six hours' load, if it calls that tool before 9 pm, also gets the sentence.
- The sentence adds up to about 25 words on top of the prompt's 60-word limit, and nothing trims it, so a long reply can run past 60 words on Alexa.

Tests: `test_a_reply_that_leaves_out_the_caveat_has_it_added` (English long and short, no language given, Spanish long and short), `test_a_reply_that_already_says_it_is_left_alone` (three English and three Spanish wordings), `test_a_time_alone_is_not_the_caveat`, `test_a_verdict_with_no_caveat_adds_nothing`, `test_an_answer_that_never_read_the_forecast_adds_nothing`, `test_the_last_forecast_is_the_one_that_counts`, `test_a_forecast_that_failed_adds_nothing`, `test_an_empty_reply_is_still_the_apology_alone`, `test_the_verdicts_tonight_energy_writes_become_the_spanish_sentences`, `test_a_daylight_reply_in_spanish_can_carry_the_caveat`.

## Daylight check

This is the first check, on `34f0c3c`. It ran at 7:38:20–7:41:29 am, straight after the 10.5 s tick, with the next tick at 7:53. It finished about 11.5 minutes clear of that tick, and the agent logged no "model unavailable".

Each question went to the live llama-server, using the deployed code on KAMRUI. The answers ran in a sandbox copy with `dry_run` on, so no write or Telegram message was possible. All five were answered by the model on its first try, with no fallback to Python.

| Question | Tool call | Result size | Wall time | Grade |
|---|---|---|---|---|
| `tonight_shortfall` | `get_load_forecast(hours=48)` | 2,188 chars | 30.0 s | **Fail**, no caveat (before `bae9e72`; the re-ask above passes) |
| `solar_yesterday_kwh` | `get_history(window="yesterday")` | 1,105 chars | 10.6 s | Pass, 31.6 kWh |
| `runtime_24h` | `get_gen_runtime(days=1)` | 4,160 chars | 36.6 s | Pass, Kubota 14 min |
| Three months' hours | `get_gen_runtime(days=90)` | 5,986 chars | 46.2 s | No overflow; figures right |
| Last threshold changes | `get_recent_actions(n=20)` | 7,663 chars | 63.5 s | No overflow; weak answer |

The replies, verbatim:

1. **"Will a generator run tonight, and what's the shortfall?"** Quoted in the section above.
2. **"How many kWh of solar did we make yesterday?"**
   > We made 31.6 kWh of solar energy between midnight and midnight yesterday.
3. **"How long did each generator run in the last 24 hours?"**
   > The Kubota generator ran for 14.0 minutes in the last 24 hours, while the MEP generator did not run.

   This is the 14-minute exercise from 6:48 pm on Nov 5, inside the 24-hour window.
4. **"How many hours did the generators run in the last three months?"**
   > The generators ran for a total of 79.2 hours over the last 90 days, with the Kubota running for 39.7 hours and the MEP for 39.4 hours.

   I checked these against `gen_runs`, exercise included: Kubota 2,385 minutes and MEP 2,367 minutes, 79.2 hours in all.
5. **"What were the last threshold changes and why?"**
   > The last threshold change was refused at 9:14 pm on 2026-10-31 because the owner changed the thresholds in the dashboard to MEP 53.5/57.0 V and Kubota 53.5/57.0 V. The previous change was refused at 6:49 pm on 2026-10-18 because the owner set MEP 52.0/55.0 V and Kubota 52.0/55.0 V by hand, which became the baseline.

   It answered within size, but it reads refused proposals as "changes" and skips the last write that actually went through, at 1:18 am on Oct 12. That entry was in the result.

**Which caveat branch fired:** sun up with a shortfall, the long form. The other daylight branch, sun up with no shortfall, is covered by a unit test but hasn't been seen live.

## Fix 1: tool result size

Sizes on a snapshot of the live database from this morning, tokenised by Gemma's tokenizer through the server's `/tokenize`:

| Result | Before: chars / tokens | After: chars / tokens |
|---|---|---|
| `get_gen_runtime(days=1)` | 3,333 / 1,456 | 4,160 / 1,846 |
| `get_gen_runtime(days=7)` | 5,154 / 2,489 | 5,996 / 2,879 |
| `get_gen_runtime(days=30)` | 8,957 / 4,645 | 5,514 / 2,738 |
| `get_gen_runtime(days=90)` | 26,158 / **14,333** | 5,986 / **3,018** |
| `get_gen_runtime(days=365)` | 26,160 / 14,335 | 5,442 / 2,697 |
| `get_recent_actions(n=20)` | 8,680 / 3,501 | 7,663 / 3,003 |
| `get_recent_actions(n=50)` | 21,276 / 8,670 | capped at 20 |

The 1- and 7-day results grew because they now carry exercise runs and fields. The tests also use a harder fixture: both generators every night for a year, with no empty fields. That measures 3,110 tokens at 90 days and 3,074 at 365. At 90 days it holds about twice the runs the real record does.

A worst-case question is now about 5,800 of prompt, 3,018 of result and 1,024 of reply allowance, roughly 9,840 against 16,384. Before, `days=90` needed about 21,000.

- **`get_gen_runtime`:** up to 8 runs comes back as before. Past 8, `runs` holds the latest 7 in full, `run_periods` gives hours per generator and gallons per week (per month above 90 days), and `runs_summarised` says what happened and why. Totals, `fuel_window` and `charge_rates` still cover everything.
- **`get_recent_actions`:** capped at 20 in schema and code. An entry now has `at`, `tool`, `result`, `reason`, `values` and `proposed_because`. Half of each entry was `args`, JSON inside JSON. I kept the proposal's reason because on a permitted write the guard's reason is just "permitted". The saving is modest, 3,501 to 3,003 tokens, because the reasons are the bulk.
- **Safety net in `Tools.call`:** over 15,000 characters, the model gets the error and hint instead, and a WARNING is logged. The densest JSON measured is 1.74 characters a token, so that's at most about 8,600 tokens, with about 900 spare. Figures from a stopped result can't source a reply, because `self.calls` records what the model was shown.
- **What the net now stops:** of the calls measured, only `get_monthly_summary(months=100, detail=True)`, at 20,480 characters. `detail=True` alone, at 13,594, passes.
- **Tests:** `tests/test_tool_size.py`, including rendered sizes for 7, 30, 90 and 365 days and for 20 actions, and the net firing on an oversized fake tool. The tests have no tokenizer, so they convert characters at each tool's densest measured ratio. Their budgets are 3,250 for runtime and 3,300 for actions.

**Limits that remain:**

- The net limits each result, not their sum, and a question can make several calls. Two large results can still overflow, which now reports as an HTTP 500 with the server's reason.
- Stand-downs repeat every 15 minutes, and 17 of the last 20 audit entries are stand-down refusals. A long enough stand-down could push the last real write past the cap of 20.
- `get_gen_runtime` at about 6,000 characters sits right at the 6,000-character log warning, so that warning may appear in normal use.

## Fix 2: yesterday's solar

**The cause is "kWh", and finding 5 was wrong to call it English-only.** Replay through the real question path, before the fix:

| Question | Tool | Reply gave |
|---|---|---|
| "How many kWh of solar did we make yesterday?" (twice) | `get_monthly_summary(months=1)` | 156 kWh, the month so far |
| "How much solar did we make yesterday?" | `get_history(window="yesterday")` | **31.6 kWh, right** |
| "How many kilowatt hours of solar did we make yesterday?" | `get_monthly_summary(month="2026-10")` | 1,025 kWh |
| "How many kWh of solar did we make today?" | `get_mppt_detail` | 0.111 kWh |
| "¿Cuántos kWh de solar produjimos ayer?" | `get_monthly_summary(month="2026-10")` | 1,025 kWh |

Spanish failed this morning although it passed 3 of 3 last night, so the language isn't what separates them. Taking "kWh" out is what changed the tool. Each variant ran only once or twice, so this is strong evidence, not proof.

The monthly tool's description opened with "solar kWh", while `get_history` only mentioned kWh in a parenthesis. Each description now opens with the question it answers.

After the fix, on the same snapshot: English 3 of 3, Spanish 2 of 2, and the "no kWh" and "kilowatt hours" variants all called `get_history(window="yesterday")` and gave 31.6 kWh. "Today" also moved to `get_history`, giving 0.2 kWh. The worst-month question still used the monthly tool, and got January 2026, 613.1 kWh.

**Side observation, not fixed:** "How many kWh of solar did we make in October?" chose `month="2025-10"` and answered 812 kWh, clearly labelled as 2025. In November 2026 the owner means 2026. I didn't run that question before the fix, so I can't say whether the change caused it.

**Schema size, rendered prompt with no plan context:**

| | Tokens | Change |
|---|---|---|
| Before these fixes (`3c595d4`) | 5,346 | |
| After fixes 3 and 1 | 5,355 | +9 |
| After fix 5 | 5,372 | +17 |
| After fix 2 | 5,425 | +53 |
| **Total** | | **+79** |

Tests: `test_the_monthly_tool_rules_out_a_single_day_first`, `test_the_history_tool_claims_how_much_today_or_yesterday_first`.

## Fix 3: exercise runs

- `get_gen_runtime` includes them, each with its `kind`. Per-generator totals have `minutes` and `runs` for all runs, plus `exercise_minutes` and `exercise_runs`.
- `fuel_window` and `fuel` count every run, with the exercise share beside them. In `fuel` those fields appear only when non-zero, to save tokens.
- **Learning is unchanged.** `charge_rates` reads its own query (`loadmodel.py:940`) and `charge_samples` defaults to excluding exercise, and neither was touched. `test_charge_rate_learning_still_ignores_exercise` adds an exercise run with ten times the real output and asserts rates and samples don't move. I confirmed the test does its job by removing the filter from `loadmodel.py`: it failed, and I restored the filter.
- The `runtime_24h` key now counts exercise, and "neither generator ran" now fails against that key.

Tests: `test_runtime_totals_split_out_exercise_and_label_each_run`, `test_an_exercise_run_burns_fuel_and_its_share_is_shown`, `test_the_days_fuel_counts_an_exercise_run_and_shows_its_share`, `test_a_generator_that_only_exercised_did_run`, `test_the_runtime_key_counts_an_exercise_run`.

## Fix 4: `llm.py` labels

`Timeout` gives "llama-server timed out after N s". `HTTPError` gives "llama-server returned HTTP 500: Context size has been exceeded." `ConnectionError` gives "llama-server unreachable at <url>". Callers still get `LLMError`.

Two details: a `ConnectTimeout` is also a `ConnectionError`, so `Timeout` is caught first. And `requests`' own JSON error is also a `RequestException`, so a 200 with a bad body used to be called "unreachable"; it now says "non-JSON". Anything else `requests` raises says "request failed".

Tests: `tests/test_llm.py`, using mocked `requests.post`.

## Fix 5: the caveat

The tool side works. The description change alone didn't fix the reply, which is why `bae9e72` exists.

| When | Verdict ends |
|---|---|
| Sun up, shortfall | "Not final: solar still to come will cover load and charge the pack, so this will likely shrink; the top-up rule decides at 9:00 pm." |
| Sun up, no shortfall | "Not final: the top-up rule decides at 9:00 pm." |
| Sunset until the window opens | "Not final: the top-up rule decides at 9:00 pm." |
| After the window opens | Nothing |

`get_load_forecast`'s description now says to quote `tonight.verdict` in full. A forecast that can't say how much sun is left keeps the long form when there's a shortfall, as before.

Tests: `test_sun_up_with_a_shortfall_says_it_will_likely_shrink`, `test_sun_up_with_no_shortfall_has_nothing_to_shrink`, `test_from_sunset_until_the_window_opens_only_the_rule_is_left`, `test_after_the_window_opens_the_verdict_stands_on_its_own`, `test_the_forecast_tells_the_model_to_quote_the_verdict_whole`.

## Deploy

| Time | Event |
|---|---|
| 7:22:52 am | `/data`: both generators off, `mep803aAction` and `kubotaAction` both 10. KAMRUI's tree clean. |
| 7:23:05 am | `git pull --ff-only` to `34f0c3c`, `solar-agent` restarted |
| 7:23:07 am | Prompt cache warmed in 0.8 s |
| 7:23:58 am | First tick, 52.6 s, no change |
| 7:38:16 am | Next tick, 10.5 s, no change |
| 7:53:16 am | Tick after the first check, 10.4 s, no change |
| 7:53:31 am | `/data`: both generators off, both 10. `bae9e72` pushed. |
| 7:53:34 am | `git pull --ff-only` to `bae9e72`, `solar-agent` restarted. Cache warmed in 1.1 s. |
| 7:53:45 am | First tick, 10.9 s, no change |
| 8:08:44 am | Next tick, 10.3 s, no change. The re-ask followed. |

The warm-ups took about a second instead of the usual 105 s because my replay of fix 2 had already cached the new prefix on the server. A later deploy that changes the schemas should expect the cold tick again. The agent logged no "model unavailable" at any point in this work.

## Decisions the brief didn't cover

I took the cautious option on each. Any of them can be reversed:

1. **7 runs kept, not about 10.** Ten full runs took 90 days to 3,673 tokens, so I cut to 7 to stay near 3,000. To make room, `fuel` shrinks to its summary sentence once the list is summarised, and `run_periods` has one gallons figure per period, not one per generator.
2. **`get_monthly_summary` still excludes exercise,** and says so. The brief's fuel rule was about `get_gen_runtime`. As a result the two tools can differ on a month's hours and gallons by the exercise share. Tell me if monthly should count them too.
3. **The morning report still excludes exercise** (`agent.py:950`). It wasn't in the brief.
4. **`EvalTools.get_gen_runtime` in `model_eval.py` is unchanged.** It's the tick replay's own copy, still says "exercise runs are excluded", and doesn't have the summarised form. The question path, which the exam uses, has the new code.
5. **The check ran in a dry-run sandbox, not through live `POST /ask`.** The live path holds `self.lock`, so it can't collide with a tick, but it could write or send Telegram while you're at work. The sandbox can't, so I kept it and gated each question instead: none started within 5 minutes of a tick.
6. **Live replay questions beyond the check's five.** Fix 2 asked me to replay the case, so I ran 16 more questions with the same gating: 6 before the fix, right after the 7:03 tick, and 10 after it, right after the 7:18 tick. Each batch finished within 4 minutes, well before the next tick. The 7:18 tick took 10.8 s.
7. **Removed `allowed` from audit entries,** because `result` says the same in words and also covers "missed".

## Left on the machines

KAMRUI is on `bae9e72`. I deleted the check's temporary files there. `llama-server.service` wasn't touched, in line with dropping finding 3.
