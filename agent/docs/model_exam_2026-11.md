# Question-path exam: Gemma 4 E4B against the incumbent Qwen3-8B

Run on KAMRUI on 2026-11-04, 18:25 to 19:45 PST. Both models answered through the agent's own `answer()`, in dry run, against one snapshot of the database taken at 18:25:56. Live is restored and verified, and the sudoers rule is gone.

Written by Claude Code from the run's results.

**In short: Gemma answered at least as reliably as the incumbent in this run, and in about half the time.** Its score was 3 of 9 against 0 of 9, but only the 3 voltage attempts are a fair test. The two models also share several wrong answers that the checks did not catch.

## Results

| Model | Built-in cases passed (of 4 × 3, excluding Python-answered) | Ungrounded retries | `status_sentence` fallbacks | Unsourced numbers | Avg / max wall time per answer | Avg tool calls per answer | Peak Tctl |
|---|---|---|---|---|---|---|---|
| Gemma 4 E4B | 3 of 9 | 0 | 0 | 2 | 28.0 / 144.4 s | 1.15 | 85.25 °C |
| Incumbent Qwen3-8B | 0 of 9 | 0 | 0 | 4 | 66.0 / 154.4 s | 1.15 | 85.5 °C |

- **Questions:** each model was asked all 29 (12 built-in, 17 extra). Python answered 3 for each, the `tonight_plan` repeats, leaving 9 graded.
- **Clean on both sides:** no model errors, no invalid tool calls, and no threshold proposals.
- **Limit kills:** none, so no question counts as "not asked (killed)".
- **Wall time** covers model-answered questions, so the Python answers don't pull it down.

### Reading the scores

Only one of the three graded cases measured the models. The other two fail correct answers on this snapshot, as explained under each case below. I have not changed the graders, and the table above is what they returned.

My reading of the same 9 replies against the database, separate from the graders:

| Case | Gemma | Incumbent |
|---|---|---|
| `kubota_twice` | 3 of 3 factually right | 2 of 3; #3 invents the starts |
| `runtime_24h` | 3 of 3 | 3 of 3 |
| `voltage_at_247` | 3 of 3 | 0 of 3 |
| **Total** | **9 of 9** | **5 of 9** |

## Built-in cases


### `kubota_twice`

> Why did the Kubota start twice yesterday morning?

| Model | Verdicts | Why it was marked down | Representative reply | Tools | Wall s |
|---|---|---|---|---|---|
| Gemma | FAIL FAIL FAIL | neither cites the raised start threshold nor checks the day | #1: The history for yesterday, midnight to midnight, shows no generator minutes for either generator. | `get_history(window=yesterday)`, `get_status()` | 75.4 / 23.5 / 17.3 |
| Incumbent | FAIL FAIL FAIL | neither cites the raised start threshold nor checks the day | #1: The Kubota did not start twice yesterday morning. The run_reason for the Kubota is "not_on," indicating it was not running. The generator's status shows it was not active during that time. | `get_status()` | 154.4 / 41.9 / 150.0 |

Fails for both on this snapshot, and the grader is the reason. It was written for a morning with two real starts. The snapshot has none: `starts_today` 0, `starts_yesterday` 0. A correct "it did not start" contains neither a threshold nor "today", so it cannot pass.

The replies still differ. Gemma's three are right on the facts, though #1 and #2 only state the history and don't take on the question. The incumbent's #1 and #2 are right, but rest on the current `run_reason`, not on history. **Its #3 invents the event**: "The Kubota started twice yesterday morning due to "dc_voltage_low" as per get_status. The generator ran to charge the pack when the voltage dropped below the start threshold." The database has no such start.

### `runtime_24h`

> How long did each generator run in the last 24 hours?

| Model | Verdicts | Why it was marked down | Representative reply | Tools | Wall s |
|---|---|---|---|---|---|
| Gemma | FAIL FAIL FAIL | mep ran not at all and is not mentioned | #1: Based on the data, neither generator ran in the last 24 hours. | `get_gen_runtime(days=1)` | 27.3 / 27.4 / 27.4 |
| Incumbent | FAIL FAIL FAIL | mep ran not at all and is not mentioned | #1: The generators did not run in the last 24 hours. | `get_gen_runtime(days=1)` | 128.2 / 68.3 / 68.1 |

Fails for both, and every reply is correct. Neither generator ran in the 24 hours (`runs` 0 for both). The grader requires the literal word "mep", so "neither generator" and "the generators" fail.

### `tonight_plan`

> What is tonight's plan?

Verdicts: Gemma PASS PASS PASS; incumbent PASS PASS PASS. All answered by Python.

```
2026-11-04 6:13 pm  V 56.7  SOC 98%  load 1.7 kW
peak today: 57.7 V  (threshold 57.0 -> reached)
overnight Wh out of the pack: 17,754 — from last 14 nights (9 nights)
system overhead: 1.034x (pack out ÷ house in, 1.027-1.038 over 14 nights, last 14 nights)
projected 52.0 V at: not projected (not reached within 24 h)   sunrise 6:09 am
next daylight (Thu Nov 5): 51% cloud, est. solar 27.9 kWh (Nov clear-day 33.3)
kubota exercise: every 3 d at 19:00, last run 2026-11-01 8:35 pm, next 2026-11-05 7:00 pm (in 1 d)
mep exercise: every 5 d at 09:00, last run 2026-11-01 8:36 pm, next 2026-11-07 9:00 am (in 3 d)
POLICY 3 storm stop 57.0: no (next daylight (Thu Nov 5) 51% daylight cloud < 70%)
POLICY 3 pre-dawn stop 54.5: no (52 V not projected (not reached within 24 h))
POLICY 4 top-up: held (deficit -9,063 Wh; held until 9:00 pm; remaining solar today 0.0 kWh)
recommend: no change - no rule fires
applied: no change
```

Python answers this from the stored plan, so it says nothing about either model. Excluded from the counts as the brief says. Reply shown once, identical for both.

### `voltage_at_247`

> What was the battery voltage at exactly 2:47 am last night?

| Model | Verdicts | Why it was marked down | Representative reply | Tools | Wall s |
|---|---|---|---|---|---|
| Gemma | PASS PASS PASS | 54.49 matches the sample at 2026-11-04 2:47 am | #1: The battery voltage at 2:47 am was 54.49 V. | `get_voltage_at(timestamp=02:47 am)` | 14.4 / 6.5 / 14.4 |
| Incumbent | FAIL FAIL FAIL | stated 54.4 V; the sample at 2026-11-04 2:47 am reads 54.49 V | #1: The battery voltage at exactly 2:47 am last night was 54.4 V. | `get_voltage_at(timestamp=2026-11-03 2:47 am)` | 36.1 / 36.2 / 36.2 |

A fair test. The tool returned `voltage: 54.49`. Gemma quoted it three times out of three. The incumbent said 54.4 V three times out of three, 0.09 V off and outside the grader's 0.05 V tolerance. The unsourced check didn't flag 54.4, so that figure appears somewhere else in the model's inputs, though not in this reading.

## Extra questions

The Check line compares each reply with the tool's output. I re-ran the same tools on a private copy of the snapshot at 19:47. Live values such as load and AC voltage had moved by then, and those are marked.

### English

**1. Is a generator running right now, and what is the house drawing?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | No generator is running right now, and the house is drawing 1949 W. | `get_status()` | none | 15.5 | Right |
| Incumbent | No generator is running right now. The house is drawing 1,759 W. | `get_status()` | none | 37.0 | Right |

Check: Both correct. House load differs because each was read live, about 36 minutes apart.

**2. How much is each solar array making right now?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | The solar arrays are currently making 0 watts, with the total today's production across all controllers being 31.795 kWh. | `get_mppt_detail()` | none | 10.0 | Right |
| Incumbent | The south array is making 10.81 kWh today, the west array is making 10.88 kWh today, and the mppt80 array is making 10.10 kWh today. | `get_mppt_detail()` | 10.1, 10.88 | 44.1 | Wrong |

Check: It was dark: the tool shows 0 W on all three arrays. Gemma says so, though not array by array. The incumbent gives each array's kWh today, which isn't what was asked. Its unsourced 10.1 and 10.88 are fair roundings of 10.102 and 10.883.

**3. What was the lowest battery voltage last night?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | The lowest battery voltage last night, which covers the span from 2026-11-04 4:55 pm to 2026-11-04 6:37 pm, was 56.57 V. | `get_history(window=overnight)` | none | 20.9 | Wrong |
| Incumbent | The lowest battery voltage last night was 56.57 V. | `get_history(window=overnight)` | none | 40.9 | Wrong |

Check: **Both wrong, for the same reason.** After sunset `window: overnight` means tonight (4:55 pm onward), so 56.57 V is tonight's low. Last night's low was 54.11 V at 5:47 am. The tool does state the span, and Gemma repeats it, which at least exposes the problem. The incumbent presents the figure as last night's.

**4. How many hours have the generators run this week, and how much fuel did that use?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | The generators have run for a total of 417.0 minutes this week, using a total of 1.46 gallons of fuel. | `get_gen_runtime(days=7)` | 1.46, 417.0 | 41.9 | Wrong |
| Incumbent | The generators have run for 211 minutes (3.5 hours) by the MEP and 206 minutes (3.4 hours) by the Kubota this week, using 0.86 gallons for the MEP and 0.63 gallons for the Kubota. | `get_gen_runtime(days=7)` | 3.4, 3.5 | 110.6 | Partly |

Check: Tool totals are 211 + 206 = 417 min, so 417.0 is a correct sum. Gemma gives minutes where hours were asked, and 1.46 gal is in no tool output: the two fuel figures add to 1.49. The incumbent's hours are right. Both fuel figures come from month-to-date fields (`fuel_mtd_gal`), not the week, so the incumbent's 0.86 and 0.63 are mislabelled, and neither says "modelled" as the tool asks.

**5. Will a generator run tonight, and what's the shortfall?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | No generator run is expected tonight because the pack is not projected to reach the 52.0 V start threshold within 24 hours, and the projected shortfall is 26,164 Wh based on the learned Wh-vs-V curve. | `get_load_forecast(hours=24)` | none | 22.2 | Partly |
| Incumbent | The pack is not projected to reach the 52.0 V start threshold within 24 hours, so no generator run is expected tonight. The shortfall is 26,164 Wh, based on the learned Wh-vs-V data from 47 nights. | `get_history(window=overnight)`, `get_load_forecast(hours=24)` | none | 71.4 | Partly |

Check: "No run tonight" is right. **"Shortfall 26,164 Wh" is wrong in both**: that's `available_wh`, the energy available above 52 V. Tonight's draw is 17,754 Wh, so there is no shortfall.

**6. Why is the MEP start set where it is right now?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | The MEP start is set to 52.0 V because the owner manually set the thresholds to MEP 52.0/55.0 V and the current policy does not require a change from that baseline. | `get_status()` | none | 17.8 | Partly |
| Incumbent | The MEP start is set at 52.0 V because the owner's baseline thresholds are in place. The agent has not changed it, as no POLICY rule is currently active to adjust the thresholds. The default setting remains 52.0 V for the MEP start. | `get_status()` | none | 43.3 | Right |

Check: Both have the 52.0 V start, and no POLICY rule fired. Gemma quotes 52.0/55.0 while `get_status` showed a 57.0 V stop. 52.0/55.0 appears in a refused action from 18 Oct.

**7. Why did the MEP fail to start last night?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | The MEP did not start last night because the battery voltage never dropped below the 52.0 V start threshold during the overnight period. | `get_status()`, `get_history(window=overnight)` | none | 23.8 | Right |
| Incumbent | The MEP failed to start last night because its run_reason was "not_on," indicating it was not triggered by any policy rule or threshold. The system thresholds were set to 52.0 V start and 57.0 V stop, but the pack did not fall below the start threshold, so the generator did not activate. | `get_status()` | none | 44.9 | Partly |

Check: A false premise: nothing failed, since the pack never neared 52 V. Gemma corrects it. The incumbent repeats "failed", leads with `run_reason`, then gives the right cause.

**8. What was the battery voltage at 3:15 am on January 5, 2024?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | There is no sample near enough for the battery voltage at 3:15 am on January 5, 2024. | `get_voltage_at(timestamp=2024-01-05 03:15 am)` | none | 15.3 | Right |
| Incumbent | The battery voltage at 3:15 am on January 5, 2024, is not available. The nearest sample is from 2026-08-28 6:53 pm. | `get_voltage_at(timestamp=2024-01-05 3:15 am)` | none | 37.4 | Right |

Check: No sample exists, and both say so. The incumbent also gives the tool's `nearest_sample` correctly.

**9. How cloudy is it going to be tomorrow?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | Tomorrow is forecast to have 17% cloud cover during the day. | `get_weather()` | none | 15.7 | Right |
| Incumbent | Tomorrow is 17% cloudy with 12% cloud during daylight, estimated solar yield of 32,000 Wh. | `get_weather()` | none | 40.4 | Right |

Check: Correct when asked, as far as can be checked. Re-run at 19:47 the tool gave 17% cloud, 11% in daylight and 32,105 Wh. Gemma calls the whole-day 17% "during the day". The 18:13 plan in the snapshot said 51% cloud, so the forecast moved; I did not look into why.

**10. How many more years will this battery pack last?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | The battery pack is projected to last 28.2 years based on the cycling mechanism, which is the dominant factor. | `battery_health()` | none | 34.9 | Right |
| Incumbent | The battery pack is projected to last 28.2 years based on cycling, with a combined projection of 14.5 years if both cycling and calendar fade are considered. The dominant mechanism is cycling. | `battery_health()` | none | 111.3 | Right |

Check: `headline_years` is 28.2, and the tool calls the 14.5-year combined figure a floor, not the expected life. Gemma gives the headline. The incumbent gives both, without saying 14.5 is a floor.

**11. Which month this year had the worst solar?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | The month with the worst solar was December 2025 with 541.7 kWh. | `get_monthly_summary()` | none | 25.3 | Wrong |
| Incumbent | The worst solar month this year was December 2025 with 541.7 kWh. | `get_monthly_summary(detail=False)` | none | 64.0 | Wrong |

Check: **Both wrong.** The tool's `worst_solar_month` ranks the whole record, and December 2025 isn't this year. In its own table the worst complete month of 2026 is January, 613 kWh.

### Spanish

**12. ¿Cuál es el voltaje de la batería ahora?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | El voltaje de la batería en ese momento no está registrado en una muestra cercana, pero la muestra más cercana es a las 6:25 pm. | `get_voltage_at(timestamp=2026-11-04 6:45 pm)` | none | 15.4 | No answer |
| Incumbent | El voltaje de la batería no se puede leer en ese momento exacto. La muestra más cercana está disponible a las 6:25 p. m. del mismo día. | `get_voltage_at(timestamp=2026-11-04 7:28 pm)` | none | 36.2 | No answer |

Check: Neither gives a voltage. Both called `get_voltage_at` for the current time, and the snapshot ends at 18:25:29, so no sample was near. Part of this comes from the exam itself: against the live database the same call would probably have found one. `get_status` would have answered it either way.

**13. ¿Cuántos kWh de solar produjimos ayer?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | Según el registro, ayer entre medianoche y medianoche se produjeron 33377 kWh de solar. | `get_history(window=yesterday)` | none | 17.9 | Wrong |
| Incumbent | Produjimos 33,377 kWh de solar ayer. | `get_history(window=yesterday)` | none | 41.5 | Wrong |

Check: **Both wrong by a factor of 1,000.** The tool says `solar_wh: 33377`, which is 33.4 kWh. The unsourced check can't catch this, because the digits match.

**14. ¿Qué inversores y generadores tenemos instalados?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | Los inversores instalados son el Schneider XW Pro 6848 (esclavos 10 y 12) y el Schneider XW+ 5548 (esclavo 11). Los generadores instalados son el MEP-803A (AGS esclavo 51) y el Kubota (AGS esclavo 50), además de un Magnum MS4048 que no está en Modbus. | `get_system_specs()` | none | 39.0 | Right |
| Incumbent | Tienes instalados los siguientes inversores y generadores:<br><br>**Inversores:**<br>- **xw_pro_master**: Schneider XW Pro 6848, alimenta la carga AC y se conecta en paralelo con el inversor esclavo.<br>- **xw_plus_5548**: Schneider XW+ 5548, alimentado por el generador Kubota.<br>- **xw_pro_slave**: Schneider XW Pro 6848, conectado en paralelo con el inversor maestro a través del PDP.<br><br>**Generadores:**<br>- **MEP-803A**: 10 kW, carga al 100%, alimenta los inversores maestro y esclavo.<br>- **Kubota**: 7 kW, carga al 70%, alimenta el inversor XW+ 5548.<br>- **Magnum MS4048**: No está en Modbus, solo se mide la corriente del generador Kubota.<br><br>Los generadores se usan para mantener la batería en el rango de carga óptimo y asegurar el suministro eléctrico. | `get_system_specs()` | none | 118.2 | Right |

Check: Both match `get_system_specs`. The incumbent is much longer and adds a closing sentence of its own about why generators are used.

**15. ¿Cuántos amperios-hora le quedan a la batería?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | Según los datos de la herramienta, le quedan 1741 amperios-hora a la batería. | `get_battery_detail()` | none | 16.2 | Right |
| Incumbent | La batería tiene 1717 amperios-hora restantes. | `get_battery_detail()` | none | 37.9 | Right |

Check: Grounded in `get_battery_detail`. A live value, so not re-checkable: `get_status` read 1,710 Ah at 19:47.

**16. ¿Cuál fue el último cambio de los umbrales del generador y por qué se hizo?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | El último cambio de umbrales fue el 31 de octubre de 2026 a las 9:14 pm, y fue rechazado porque el propietario cambió los umbrales en el panel de control a MEP 53.5/57.0 y Kubota 53.5/57.0. | `get_recent_actions(n=50)` | none | 144.4 | Right |
| Incumbent | El último cambio de los umbrales del generador fue a las 9:14 pm, cuando el dueño cambió los umbrales en el panel de control. Se hizo para ajustar los umbrales según la política 4, debido a un déficit de 6,508 Wh para alcanzar el voltaje de 52.0 V antes del amanecer. | `get_recent_actions(n=5)` | none | 68.9 | Wrong |

Check: The log's newest entry is a *refused* agent write at 9:14 pm on 31 Oct, and the reason is that the owner had changed the thresholds. Gemma reports that correctly, at the cost of asking for 50 actions (21,276 chars, 144 s). The incumbent gives no date and joins the agent's refused POLICY 4 reason onto the owner's change, which is not why the owner changed it.

**17. ¿Cómo están el voltaje y la frecuencia de CA en cada inversor?**

| Model | Reply | Tool calls | Unsourced | Wall s | Check |
|---|---|---|---|---|---|
| Gemma | El voltaje de CA en el inversor maestro es 120.16 V y la frecuencia es 60.0 Hz, mientras que en el inversor esclavo es 119.94 V y la frecuencia es 60.0 Hz. | `get_ac_diag()` | none | 18.4 | Right |
| Incumbent | El voltaje de CA en cada inversor está estable en aproximadamente 119.45 V para L1 y L2. La frecuencia del inversor maestro es 60.0 Hz, mientras que la del inversor esclavo es 59.99 Hz. La potencia del inversor maestro es de 693 W y la del inversor esclavo es de 944 W. La diferencia en la frecuencia es mínima y no representa un problema. | `get_ac_diag()` | none | 48.2 | Right |

Check: Grounded in `get_ac_diag`. Live values, so not re-checkable.

### Summary line

- **English (11):** all replies grounded in at least one tool call and in English, for both models. Gemma 6 right, 2 partly, 3 wrong. Incumbent 5 right, 3 partly, 3 wrong.
- **Spanish (6):** all replies grounded in at least one tool call and in Spanish, for both models. Gemma 4 right, 1 wrong, 1 no answer. Incumbent 3 right, 2 wrong, 1 no answer.
- **Total (17):** Gemma 10 right, 2 partly, 4 wrong, 1 no answer. Incumbent 8 right, 3 partly, 5 wrong, 1 no answer. These are my judgments, not grader output.

**Unsourced numbers:** Gemma's two are a correct sum (417.0) and a wrong one (1.46). The incumbent's four are all fair roundings or conversions. The count flags arithmetic, not error: it missed the 33,377 "kWh" unit error and the incumbent's 54.4 V.

## Times and limits

| Event | Time (PST) |
|---|---|
| Snapshot taken, live stop requested | 18:25:56 |
| Gemma server up, exam start | 18:26:13 |
| Gemma exam end (rc=0) | 18:52:29 |
| Live start requested | 18:52:30 |
| Live `/health` ok, `llama_gpu_check` rc=0 | 18:54:09 to 18:54:10 |
| Live stop requested for the incumbent | 18:54:10 |
| Incumbent server up, exam start | 18:54:27 |
| Incumbent exam end (rc=0) | 19:37:10 |
| Live start requested | 19:37:11 |
| Live `/health` ok, `llama_gpu_check` rc=0 | 19:38:51 |
| Real agent tick completed (98.5 s, no change) | 19:45:08 |
| Sudoers rule removed | 19:45:11 |
| Dead-man stood down | 19:45:15 |

- **Live was down for about 73 minutes**: 28:13, a gap of about 1 second, then 44:41. Each restart took about 100 s, which includes the unit's device wait.
- **Five ticks ran with live down** (18:28, 18:43, 18:58, 19:13, 19:28). Each logged `model unavailable` and ended in about 1 to 2 s with "no change". This is what the brief expected. Those ticks had no model, and no generator ran in the window.
- **Exam length:** Gemma 26:15, incumbent 42:43, each including 14 minutes of 30 s pauses.

| | Gemma | Incumbent |
|---|---|---|
| Thermal kills | none | none |
| Peak Tctl | 85.25 °C | 85.5 °C |
| Readings over 85 °C | 5 of 1,565 | 9 of 2,540 |
| Longest stretch over 85 °C (kill at 10 s) | 1 reading | 3 readings |
| Median Tctl | 74.4 °C | 83.4 °C |
| Peak swap (kill at 2 GB) | 858,880 kB | 925,440 kB |
| GTT with the model up (idle 1.69 GB) | 7.61 GB | 9.28 GB |
| Prefill, cold 5,800 to 6,000 token prompt | 96.5 tok/s, 60 s | 44.9 tok/s, 133 s |
| Generation, last answer | 11.0 tok/s | 6.2 tok/s |

**GPU offload:** the logs have no "offloaded" count, because that line needs `-lv 4` (see `model_candidates_2026-10.md`). The GTT rise and the prefill rates are the evidence that both models ran on the GPU. Neither log contains "no usable GPU".

## Deviations from the brief

All of these were Michael's decisions except where marked as mine.

- **Timing:** run at 18:25 in the evening, not overnight. The first armed runner, set for 00:05 on 11-05, was cancelled at 18:13 with TERM. Live was not stopped by that, and its folder is `/tmp/abexam-cancelled-1813`.
- **Order:** Gemma first, then the incumbent.
- **Limit kills:** a thermal kill would have ended only that model. Other kills would have stopped the run. Neither happened.
- **Between the models:** the real-tick wait was removed, so `/health` and `llama_gpu_check` were the gate. The incumbent's exam started about 2 minutes after Gemma's ended, with no other cool-down. See the caveat below.
- **Swap source:** the guard reads `/proc/meminfo`, not the flight log.
- **Unattended runner (mine, reviewed by Michael):** `run.sh`, `guard.sh`, `deadman.sh`, `launch.py`, `arm.sh`, in `model_exam_2026-11/runner/` with the self-test and its output (91 of 91, with services stubbed). This run was the first real test of its stop, start and `rm`, and all three worked.
- **Harness additions (mine):** each answer gained `asked_at` and `ended_at`, to apply the killed-question rule. `--log-timestamps` is on for the candidate servers.
- **Sampling:** `llm.py` sends `temperature: 0.2` on every request, so Gemma's `--temp 1.0` was overridden. Its `--top-k 64` and `--top-p 0.95` applied. The incumbent ran with the unit's own arguments.
- **Unchanged:** `agent.py`, `tools.py`, `llm.py`, `config.json`, the unit files and `pi5/`. `llama-server-abliterated` was already inactive and was not touched.

## Caveats

- **One snapshot, and a quiet one.** There were no generator starts in the graded windows, so the generator-history cases never met a real event.
- **The incumbent ran on a warm box.** It started right after Gemma, so its median Tctl of 83.4 °C and its speed may be somewhat worse than from cold. The roughly 2.4× time difference is close to the roughly 2× seen in the tick A/B, so order doesn't look like the main cause, but this run can't separate the two.
- **My judgments are mine.** The Check lines and "my reading" are one reviewer's calls against tool output. The live-value answers could not be re-checked.

## Problems found, none fixed here

1. **Graders:** `kubota_twice` assumes two starts exist, and `runtime_24h` needs the literal word "mep". Both failed correct answers.
2. **`overnight` window:** after sunset it means tonight, so "last night" questions get tonight's figures. Both models fell for it.
3. **`available_wh`:** both models called it the shortfall.
4. **`worst_solar_month`:** it ranks the whole record, so "this year" gets a 2025 month. Both models fell for it.
5. **`solar_wh`:** both models reported it as kWh in Spanish. The number check can't see units.
6. **`llama_gpu_check`:** its 1 GB GTT floor can't fail on this box, where idle GTT is 1.69 GB. It returned rc=0 at 18:53 with `llama-server` stopped. Only the journal warning can still trip it, so its comment about 89 MiB at idle is out of date.
7. **Exam snapshot:** "now" questions that use `get_voltage_at` hit the end of the snapshot and get no sample.
8. **`/data` clock:** `lastUpdate` read 17:02:37 at 19:48:30 on KAMRUI. It was advancing, so the data isn't stale, but that clock is about 2 h 46 min behind. Not investigated. **Dropped on 2026-11-06, not a bug:** `lastUpdate` is uptime by design (`pi5/app.py`, in `poll_modbus`), and `clockTime` is the clock.

## Verdict

On this run, Gemma answers questions at least as reliably as the incumbent. It passed the one fair graded test three times out of three where the incumbent failed all three, it did not invent an event where the incumbent once did, and it was right on 10 of the 17 extra questions against 8, while taking 28 s per answer against 66 s under the same limits. Its advantage is real but narrow. On the extras the incumbent was better on the weekly hours and the reason for the MEP threshold, and Gemma produced the run's one clearly wrong sum. Both models were equally grounded by the numbers, with no retries or fallbacks, yet both gave the same confident wrong answers on last night's low, the shortfall, the worst month and the Spanish kWh figure, so neither answers these reliably as the tools stand. This is one snapshot with few real generator events. Whether to deploy is Michael's call.
