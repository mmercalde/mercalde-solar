# Battery Monitor SOC at the 52.0 V start, 2026-11-08

Read-only investigation. Nothing was written to the monitor, no config was
changed, and no service was restarted. The history numbers come from a
read-only snapshot of KAMRUI's `data/history.sqlite`, taken 2026-11-08
05:56 PST while both generators were running.

**Question.** The Conext Battery Monitor (slave 191) read 69 % at this
morning's 52.0 V generator start (05:09). Since 2026-10-08 its sync point is
57.5 V with an 18 A tail (`battmon_sync_config.md`), at 1800 Ah and CEF 98 %.
The hypothesis is that 52.0 to 57.5 V is only about 550 to 650 Ah of the
1800 Ah scale, so 52 V reads about 65 to 70 %.

> **Update, same day (sections 7 to 9).** The 570 Ah below is the span from
> the monitor's 57.4 V sync down to 52 V, and that is not the pack's real
> window. On sunny days the pack charges to about 60.2 V and tapers there.
> The monitor clips at 100 % above 57.4 V, so it cannot count that top
> part. Measured over 18 months of gateway minute data, the real window is
> **about 775 Ah (42.5 kWh) at the stated 98 % charge efficiency**, from the
> tapered top at about 59.8 V down to the 52.0 V start. With the 95 %
> efficiency the shunt actually shows, it is **about 805 Ah (43.7 kWh)**.
> 57.4 V sits at about 72 % of the real window, not 100 %. Options 6a, 6b
> and 6c below are superseded by section 9.

**Answer (first pass).** The hypothesis is confirmed. Between 57.5 V and the 52.0 V start
the pack gives up about **570 Ah, or 30 to 31 kWh**. That is 32 % of 1800 Ah,
so a sync at 57.5 V followed by a run down to 52.0 V leaves the monitor at
about 68 %. Every 52.0 V start since the sync point moved reads 66 to 69 %.
The low reading comes from the scale. Coulomb drift is a secondary effect,
and a second mechanism, described below, causes most of the 3-point spread.

## 1. What the history database holds

`samples` table: one row per dashboard poll, about every 60 s, purged after
90 days. The monitor columns are `batt_soc` (%), `batt_current` (A, + =
charging), `batt_ah_remaining` (Ah), `batt_power` (W), `batt_min_to_discharge`,
`batt_monitor_online`, and `battery_v` (the monitor's voltage). All of them are
populated from the first row, **2026-08-28 18:53**, to now (101,408 rows).
The monitor's own Ah remaining divided by SOC gives 1800 throughout.

## 2. Monitor SOC at the 52.0 V crossings

There were only **two** 52.0 V generator starts in the last 30 days. The
other starts in that period were top-ups at 53.5 to 53.9 V, or exercises.
The table lists every downward crossing of 52.0 V on record, grouped by the
sync setting in force at the time:

| Crossing (PST) | V / I at the sample | SOC | Ah remaining | Sync regime |
|---|---|---|---|---|
| 08-29 05:07 | 52.00 V / −25 A | 75 % | 1353 | 60.4 V (never reached, drifting) |
| 09-07 07:45 | 51.94 V / −69 A | 56 % | 1010 | 60.4 V |
| 09-09 07:00 | 51.81 V / −35 A | 58 % | 1032 | 60.4 V |
| 09-27 15:33 | 51.93 V / −77 A | 75 % | 1348 | 60.4 V |
| 10-03 05:49 | 51.99 V / −49 A | 66 % | 1191 | 60.4 V, after recent top-ups |
| 10-03 20:28 | 52.00 V / −46 A | 67 % | 1205 | 60.4 V |
| 10-05 05:48 | 51.96 V / −62 A | 69 % | 1231 | 60.4 V |
| **10-11 08:49** | 51.99 V / −32 A | **66 %** | 1180 | **57.5 V** |
| **11-08 05:08** | 52.00 V / −30 A | **69 %** | 1233 | **57.5 V** |

- **Last 30 days (n = 2):** min 66 %, median 67.5 %, max 69 %.
- **Since 10-03 (n = 6, both regimes):** min 66 %, median 67.5 %, max 69 %.
  The spread is tight, as expected when the low reading comes from the scale.
- **The September values support the scale explanation.** Under the 60.4 V
  sync point, 100 % means the top of the pack. 52 to 60 V is about 600 Ah
  below 57.5 V plus about 145 Ah above it, roughly 745 Ah. That is 41 % of
  1800, so 52 V should read about 59 %. September 7 and 9 read 56 and 58 %.
  Under each setting, the floor reading matches the window measured below.
  The 75 % readings (08-29, 09-27) came from the drifted counter that the
  10-08 change was made to fix.

## 3. Syncs

The `batt_ah_remaining` column shows two different things that can be told
apart:

- **Counter saturation.** The counter climbs to about 1788 to 1799 Ah while
  charging and stops there. Many "99 → 100 %" steps are this, for example
  10-09 13:56 at 56.89 V and +86 A, which is nowhere near the sync condition.
  These are not syncs.
- **Real re-anchors.** These are jumps to exactly 1800 Ah from well below,
  with the voltage at 57.5 V or higher:

| Date | Ah before → 1800 | V | Correction |
|---|---|---|---|
| 10-16 15:52 | 1760 | 59.3 | 40 Ah (2.2 %) |
| 10-18 15:44 | 1708 | 58.4 | 92 Ah (5.1 %) |
| 10-24 16:35 | 1718 | 58.4 | 82 Ah (4.6 %) |
| 10-26 16:12 | 1748 | 58.8 | 52 Ah (2.9 %) |
| 11-03 14:20 | 1634 | 57.5 | 166 Ah (9.2 %), after 5 days without one |
| 11-04 14:53 | 1783 | 57.6 | 17 Ah |
| 11-05 14:44 | 1793 | 57.6 | 7 Ah |

- **Last re-anchor: 2026-11-05 14:44, about 2.6 days before the snapshot.**
  The pack has not reached 57.5 V on 11-06, 11-07 or 11-08 so far.
- **Days with the sync condition held** (V ≥ 57.5 and 0 ≤ I < 18 A for at
  least 4 min continuously) in the last 30 days: **19 of 30**. These were 10-09,
  10-13, 10-14, 10-16 to 10-19, 10-21 to 10-29, and 11-03 to 11-05. Gaps
  without one: 10-30 to 11-02 (4 days) and 11-06 onward (3 days so far).
  Every qualifying day after 10-13 was a sunny day, an exercise day, or both.
  The policy ceiling is 57.0 V, so a generator top-up never syncs it.
- **Why most corrections are large.** Above 57.5 V the counter sits at its cap
  while the pack keeps charging. Night-discharge bands put about **145 Ah
  (8.4 kWh) between 59.0 and 57.5 V** (see section 4). A day charged to
  58.5 to 59 V therefore ends with uncounted charge. The monitor starts
  counting down from 100 % at 59 V and is already about 5 to 8 % low when the
  pack passes 57.5 V. The re-anchors after the 58 to 59 V days (10-18, 10-24)
  are this size. The 3-point spread at 52 V (66 against 69 %) is mostly this
  effect. True coulomb drift is smaller: the 166 Ah on 11-03 covers 5 days
  that included a 59.7 V day.
- **CEF check.** On the four cycles in section 4, the monitor's own ΔAh exceeds
  the integrated net shunt current by 5, 7, 8 and 6 Ah. 2 % of the charge Ah
  in those spans is 5.5, 7, 7 and 6.3 Ah. The monitor applies CEF 98 % as set,
  and otherwise agrees with the shunt to within about 1 %.

## 4. The usable window: what 57.5 V to 52.0 V holds

### 4a. Per cycle: from a near-rest top (≥ 57.0 V, |I| < 20 A) down to the 52.0 V crossing

Battery current and power are integrated by trapezoid over the 60 s samples.
Gaps longer than 300 s are skipped. Every cycle includes a generator run or
some solar, so the result is the net Ah between two states. The endpoint at
52.0 V is under a 30 to 50 A load. That is the voltage the AGS starts on.

| Top | → 52.0 V crossing | Net Ah | Net kWh | Out / in Ah | Monitor ΔAh |
|---|---|---|---|---|---|
| 10-01 18:37, 57.32 V | 10-03 05:49 | 571 | 30.9 | 845 / 274 | 576 |
| 10-01 18:37, 57.32 V | 10-03 20:28 | 555 | 29.6 | 1146 / 591 | 562 |
| 10-09 17:27, 57.59 V | 10-11 08:49 | 594 | 31.8 | 943 / 349 | 602 |
| 11-06 16:22, 57.00 V | 11-08 05:08 | 541 | 29.1 | 855 / 314 | 547 |

- **Median: 563 Ah, 30.3 kWh. Range: 541 to 594 Ah.**
- One more candidate, 09-05 59 V → 09-07 52 V (800 Ah), contains a 4.8 h data
  gap and is left out. Its 800 Ah also covers the band above 57.5 V.
- The 11-06 cycle starts at 57.0 V rather than 57.5 V. Adding the
  57.5 → 57.0 V band (about 41 Ah) puts it at about 580 Ah.

### 4b. Per 0.5 V band (clean night discharges, last 90 days)

Only night segments are used: no generator, total PV below 50 W, current
below +5 A. A band edge counts as crossed at the first sample at or below it
with **|I| < 40 A**. Night loads are 25 to 50 A, so a lower threshold leaves
almost no points. The learned curve is `LoadModel.energy_above` run on the
same snapshot. It is built from the scraped hourly ENERGY_HOUR counters, a
different source from the shunt.

| Band | n | Shunt Ah (median) | Shunt Wh | Learned curve Wh (nights, tier) |
|---|---|---|---|---|
| 57.5–57.0 | 17 | 41 | 2371 | 2280 (4, last 14 nights) |
| 57.0–56.5 | 24 | 33 | 1857 | 1862 (5, last 14) |
| 56.5–56.0 | 29 | 33 | 1866 | 1928 (7, last 14) |
| 56.0–55.5 | 32 | 42 | 2365 | 2286 (9, last 14) |
| 55.5–55.0 | 47 | 54 | 2979 | 2930 (8, last 14) |
| 55.0–54.5 | 52 | 69 | 3776 | 3628 (11, last 14) |
| 54.5–54.0 | 44 | 96 | 5233 | 4686 (9, last 14) |
| 54.0–53.5 | 27 | 81 | 4361 | 3748 (22, last 60 days) |
| 53.5–53.0 | 14 | 61 | 3260 | 2857 (12, last 60 days) |
| 53.0–52.5 | 7 | 55 | 2915 | 2617 (7, last 60 days) |
| 52.5–52.0 | 2 | 37 | 1946 | 1763 (10, Nov in prior years) |
| **Total** | | **604 Ah** | **32.9 kWh** | **30.6 kWh** (`energy_above(52, 57.5)` in one call: **30.2 kWh**) |

- The shunt and the learned curve agree band by band to within about 10 %.
  Below 54.5 V the shunt is higher, and those bands also have the thinnest
  shunt samples.
- The band totals are sums of per-band medians, not the median of whole
  cycles. They include outliers: 55.5 to 55.0 V reaches 120 Ah and 54.0 to
  53.5 V reaches 103 Ah. That is why they run about 6 % above 4a.
  **Headline: about 570 Ah, about 30.5 kWh** between 57.5 V and the 52.0 V
  start.
- For comparison, `energy_above(52, 59.5)` = 41.1 kWh (3 nights), against
  the 40.9 kWh `measured_kwh_59_5_to_52_0` in `system.yaml`.
  `energy_above(52, 58.0)` = 33.0 kWh. 57.5 → 59.5 V = 10.9 kWh.
- On the 1800 Ah scale, 570 Ah is 31.7 %, so the expected reading at 52 V
  after a clean 57.5 V sync is **68 %**. The observed readings are 66 % and
  69 %.
- Pack resistance from about 300 one-minute current steps of more than 80 A:
  **4.4 mΩ** median (IQR 4.1 to 4.6). The 52.0 V start at −30 A is about
  52.13 V with the resistance drop removed. 230 A of generator charge lifts
  the terminal about 1.0 V. A 180 A load drops it about 0.8 V.

## 5. Live monitor registers

Read on 2026-11-08 at 06:18 PST, after both generator actions read 5
(stopped). The read went through the Pi5 dashboard's own client at
`http://192.168.3.10:8080/readreg`, one register per call, 1 s apart.
Nothing was written.

| Reg | Name | Raw | Meaning |
|---|---|---|---|
| 0x0046 | Voltage | 54690 | 54.69 V |
| 0x0048 | Current | 21290 | +21.3 A (charging, from solar) |
| 0x004C | SOC | 82 | 82 % |
| 0x0058 | Capacity Remaining | 1460 | 1460 Ah, which is 81.1 % of 1800 |
| 0x006A | Capacity Removed | 0 | 0. Either reset or not maintained by this unit |
| 0x006C | Capacity Returned | 0 | 0. Same as 0x006A |
| 0x0068/69 | Deepest Discharge | 0xF28B / 0xD8F0 | Not decoded. As s32 it is −225,847,056, which is not plausible. As two s16 values it is −3445 / −10000. Check the 9906278A spec before relying on it |
| 0x006E | Number of Charge Cycles | 91 | |
| 0x006F | Number of Synchronizations | 218 | No earlier reading to compare against. Read it again after the next sunny day: an increase confirms a sync |
| 0x0092 | Battery Capacity | 1800 | 1800 Ah |
| 0x008A | Charged Voltage | **57400** | **57.4 V** |
| 0x008B | Charged/Tail Current | **20** | **2.0 % = 36 A** |
| 0x008D | Auto Sync Time | 10 | 240 s (enum, per `docs/battery-monitor.md`) |
| 0x008E | Auto Sync Sensitivity | 5 | |
| 0x0094 | Charge Efficiency Factor | 98 | 98 % |

**These do not match `battmon_sync_config.md`.** That doc records writing
57500 and 10 (57.5 V, 18 A) on 10-08. The unit reads 57.4 V and 36 A. Either
the values were changed afterwards or the doc is wrong. The data agrees with
36 A: syncs fired with the polled current at 20 to 31 A (10-24, 11-03).

Effect on the findings above: very small.
- Counting with the real condition (V ≥ 57.4, 0 ≤ I < 36 A for 240 s), there
  were sync days on **20 of 30** days instead of 19. 10-20 is added.
- 57.4 V instead of 57.5 V moves the window by about 8 Ah.

## 6. Options (none applied)

### 6a. Monitor option: rescale so 52 V ≈ 0 % and 57.5 V = 100 %

Writes, one register at a time from the Pi5 (`SchneiderModbusTCP.write_single_register_16`
or `/writereg`), each read back, then a Refresh:

| Step | Reg | Now (rollback value) | New |
|---|---|---|---|
| 1 | 0x0092 Battery Capacity | 1800 | **575** (Ah) |
| 2 | 0x008B Charged/Tail Current, 0.1 % of 0x0092 | **20** (2.0 % = 36 A, as read 11-08) | **63** (6.3 % × 575 = 36 A, keeps today's behaviour), or **31** (17.8 A, the value the 10-08 doc intended) |
| 3 | 0x00AC Refresh Configuration | — | 1 |

**Rollback:** 0x0092 = 1800, 0x008B = 20, 0x00AC = 1. These are the values
read live on 11-08. Do not use the 10 from the 10-08 doc. 0x008A (57400),
0x0094 (98), 0x008D (10) and 0x008E (5) do not change.

0x008B is a percentage of capacity. Changing 0x0092 alone would turn the
36 A tail into 11.5 A, and syncs would mostly stop. Step 2 is required.

Problems with this option:

1. **Syncs are seasonal.** They happened on 19 of the last 30 days, but every
   one came from sun or an exercise, and the gaps are getting longer
   (4 days, then 3 days so far). On a 575 Ah scale, one day's drift of the
   size seen here (20 to 40 Ah) is 3 to 7 points.
2. **Saturation counts three times as much.** About 145 Ah sits between
   57.5 and 59 V. On a 575 Ah scale that is 25 points. After a 59 V day the
   monitor would read 0 % at about 53 V, hours before the start. On the
   current 1800 Ah scale the same effect is 8 points. This option makes the
   number look better after a clean sync and worse after every sunny day.
3. **It clamps at 0 %.** Most starts happen at 51.9x V under load. 09-07
   went about 190 Ah below 52 V. Everything below 52 V would read 0 %.
4. **Changing the scale breaks code that learns from it.** The agent
   decides nothing on SOC (see `guard.py:345` and `soc_scale_unreliable`
   in `system.yaml`), but these learn from it:
   - `loadmodel.capacity_ah` / `capacity_wh` take Ah remaining ÷ SOC over the
     last 2000 samples. That would move to about 575 Ah / 31 kWh within a
     day and a half, mixing both scales on the way.
   - `voltage_soc_curve` / `soc_for_voltage` would be built from 90 days of
     samples on mixed scales.
   - `soc_disagreement` (`agent.py:1714`) would compare against the wrong
     capacity.
   - The dashboard's minutes-to-discharge, the Alexa status sentence and
     `model_eval` would all change meaning.

   None of these breaks a decision. All of them would quote wrong numbers
   until 90 days of samples age out.

**Alternative sync point.** Choose a sync voltage the pack actually reaches
by charging and tapering, and set capacity to the window up to that
voltage. At 59.0 V the window is about 715 Ah (0x0092 = 715, 0x008B = 25
for 17.9 A). But 59 V was reached on only 8 days in October and on none in
November, so syncs would stop for the winter. 57.5 V is already the highest
setting that still syncs in November.

### 6b. Software option: "usable %" from the learned curve

```
usable_pct = energy_above(52.0, V_est) / energy_above(52.0, V_top) × 100
```

with V_top = 57.5 V (30.2 kWh now). The policy ceiling is 57.0 V
(27.9 kWh). Values above 100 % are allowed: 59.5 V would read about 136 %,
which is the truth. The agent already has a variant of this:
`health._resting_band_pct`, against the measured 52 to 59.5 V span of
40.9 kWh.

Getting V_est is the hard part, because terminal volts follow current:

- **At rest:** when |I| < 20 A for at least 10 min, no generator for 30 min
  (the agent's `SOLAR_PEAK_SETTLE_SECONDS` = 1800), and PV not charging,
  V_est = V − I·R with R = 4.4 mΩ (a correction of 0.1 V or less). Each rest
  sets an **anchor**: (time, V_est, Wh above 52 from the curve).
- **Between anchors,** during charging or heavy load, do not read the
  voltage. Count energy from the anchor instead:
  Wh_now = Wh_anchor + ∫ batt_power dt, with charge Wh × 0.98. Then
  usable_pct = Wh_now / Wh_top. This is the same coulomb counting the monitor
  does, but on a learned scale with a re-anchor at every rest. A typical
  night has several rests, compared with the monitor's one 57.5 V sync on a
  sunny day.
- **Light load (20 to 100 A):** V_est = V − I·R is good enough to show
  between anchors. It is off by up to about 0.2 V in the worst bands, which is
  about 4 % of 30 kWh. Above 100 A, or within 30 min of a generator stopping,
  hold the counted value.
- **Below 52 V:** report 0 % and the Wh below the floor. Do not clamp
  silently.

The weaknesses: the curve below 54 V comes from "last 60 days" and "Nov in
prior years" (7 to 22 nights) rather than recent nights, and the 52.0 to
52.5 V band has only 2 shunt points. Each future 52 V start adds a night
there.

### 6c. Recommendation

**Use 6b. Leave the monitor's scale alone.**

- The agent already treats the monitor's SOC as display-only and plans from
  `energy_above`. 6b turns that existing basis into a percentage. 6a would
  change a device that other code learns from.
- 6a only gives 52 V ≈ 0 % on the day after a clean 57.5 V sync. The
  saturation effect becomes 25 points instead of 8, syncs are getting rarer
  into winter, and the tail-current coupling adds a second register that is
  easy to get wrong. 6b re-anchors at every rest instead.
- The user-facing problem is that Alexa says "69 per cent" at the start
  (`agent.status_sentence`, `agent.py:1537`). 6b fixes that without
  touching hardware, and rolling it back is a code revert.
- If a monitor-side fix is still wanted for the Insight/dashboard display,
  apply 6a with its tail write and rollback as given above, and accept the
  drift after sunny days.

---

# Follow-up: the real window, from the true top (2026-11-08)

This was also read-only. Minute data came from the gateway's Battery Summary
chart. It was fetched on KAMRUI with `scrape_gateway.Gateway`, using the
login in the agent config, with one session that was logged out at the end,
saved to `/tmp/battwin/csv` there. This is the endpoint the UI's CSV export
serialises: `GET /chartdata/system/0/.../minutes`. Nothing was written to
the history database, the monitor, or any config.

## 7. Data

- **Gateway:** 556 days of minute rows, **2025-05-02 to 2026-11-08**
  (798,357 rows). That is as far back as it keeps: 2025-04-26 to 05-01 are
  empty. Columns used: `/SYS/BATT1/V`, `I`, `T`, `SOC`, plus `/SYS/GEN/P`
  and `/SYS/PV_TOTAL/P`.
- **Agent samples:** 2026-08-28 to now, used only for minutes the gateway
  lacks.
- **Same sensor:** on the 101,348 minutes both sources cover, the gateway's
  BATT1 matches the agent's monitor reading. The median ΔV is +0.03 V, the
  median ΔI is −0.1 A (IQR −3.8 to +3.4 A), and the median ΔSOC is 0. BATT1
  is the Battery Monitor shunt, so the current below is the same current the
  agent records.
- **Temperature:** 28.3 to 30.7 °C over the whole period (median 29.4). It
  is too stable to matter and is not modelled.

## 8. Results

### 8a. True tops: days at or above 59.5 V with the charge tapering

**135 days** reached 59.5 V or higher and tapered. The test for a taper was
that the current at or above 59.5 V fell to below the 36 A tail at some
point: the charger held voltage until net current went to zero or negative.
The **peak is 60.1 V or higher on 112 of the 135**, mostly 60.2 to
60.27 V. That is the charge controllers' absorption setpoint, and the
60.25 V day is one of many.

| Period | Days | Peak V |
|---|---|---|
| 2025-05-08 to 2025-08-12 | 68 | 59.53 to 60.27 |
| 2026-04-09 to 2026-07-08 | 61 | 59.58 to 60.27 |
| 2026-09-03, 09-04 | 2 | 59.92, 60.25 |
| 2026-10-14, 10-15, 10-27, 10-28 | 4 | 59.58, 60.25, 59.66, 60.01 |

The full date list is reproducible with the scripts listed under Reproduce.

There were **no true tops from 2025-08-13 to 2026-04-08**, about eight
months. The last one was **2026-10-28** at 60.01 V.

Five days reached 59.5 V but never tapered, with current still at 43 to
121 A, so they are excluded: 2025-05-26, 2025-08-09 (generator),
2026-04-30, 2026-10-16 and 2026-10-22.

The **top point** of each day is the minute at or above 59.4 V where the
integrated charge peaked. Its median is 59.8 V, at −10 to +5 A.

### 8b. Top → next 52.0 V crossing, per cycle

Only five cycles reach 52.0 V before another true top. In summer the sun
refills the pack before it gets that low.

The endpoint is the pack's first loaded crossing of 52.0 V. From Jan 2026
on, this is the 52.0 V generator start. In 2025 the start threshold was
about 51.2 V, so the 52.0 V crossing comes slightly before the start.

Signed shunt current is integrated by trapezoid, with gaps longer than
300 s skipped (there were none in these spans). **Charge Ah is multiplied by
0.98**, the monitor's CEF, as requested. Wh is the same integral with V × I.
The fitted 0.95 is shown alongside; see 8d.

| Top | → 52.0 V | Hours | Out Ah | In Ah | **Net Ah @ 0.98** | kWh @ 0.98 | Net Ah @ 0.95 | 57.4 → 52 Ah @ 0.98 (kWh) |
|---|---|---|---|---|---|---|---|---|
| 2025-06-02 17:09, 59.75 V | 06-07 02:24 | 105 | 1466 | 706 | **774** | 42.5 | 795 | 547 (29.6) |
| 2025-08-12 17:17, 59.90 V | 08-17 05:41 | 108 | 1802 | 1042 | **781** | 42.7 | 812 | 583 (31.2) |
| 2026-07-08 15:40, 59.94 V | 07-18 02:39 | 227 | 3899 | 3258 | **707** | 36.8 | 804 | 538 (28.7) |
| 2026-09-04 15:01, 59.77 V | 09-06 19:48 | 53 | 1139 | 348 | **798** | 44.0 | 808 | 616 (33.6) |
| 2026-10-28 16:32, 59.77 V | 11-08 05:08 | 254 | 4223 | 3629 | **667** | 33.8 | 776 | 549 (29.6) |
| **Median** | | | | | **774 Ah** | **42.5 kWh** | **804 Ah** (43.7 kWh) | **549 Ah** (29.6 kWh) |

- **At 0.98 the cycles spread from 667 to 798 Ah,** and the spread follows
  charge throughput. The two cycles with 3,000+ Ah of top-up charge come out
  lowest. **At 0.95 they tighten to 776 to 812 Ah.** That is one sign the
  0.98 is wrong for this shunt.
- **The 57.4 → 52 V span is 549 Ah,** which matches the 563 to 570 Ah of the
  first pass. It is **71 % of the real window**: on the true scale, 57.4 V is
  about 71 to 72 %. The monitor calls it 100 %.
- **The 2025 monitor readings (100 → 53 % and 100 → 55 %) point the same
  way but are not evidence.** Before 2026-07-29 the monitor synced at 57.0 V
  with automatic CEF, which had drifted to 127 %, and
  `docs/battery-monitor.md` says to ignore its SOC from that period.

### 8c. Ah per 0.5 V band, 60 → 52 V

**Nights:** clean night discharges over the full 18 months (no generator,
PV below 50 W, I below +5 A). A band edge counts as crossed at the first
sample at or below it with |I| < 40 A. **5 cycles:** the same measurement
inside the five 8b cycles, with charge counted at 0.98.

| Band | Nights n | Ah median (IQR) | Wh | 5 cycles Ah | Cumulative from 52.0 V (nights) |
|---|---|---|---|---|---|
| top (~59.8) → 59.5 | 135 tops | 9 (6–13) | 531 | — | 771 Ah |
| 59.5–59.0 | 111 | 36 (33–39) | 2131 | 35 | 762 Ah, 42.3 kWh |
| 59.0–58.5 | 112 | 46 (43–50) | 2713 | 46 | 726 Ah, 40.2 kWh |
| 58.5–58.0 | 143 | 49 (47–53) | 2861 | 54 | 680 Ah, 37.5 kWh |
| 58.0–57.5 | 165 | 50 (48–53) | 2913 | 50 | 631 Ah, 34.6 kWh |
| 57.5–57.0 | 174 | 43 (41–45) | 2450 | 43 | 581 Ah, 31.7 kWh |
| 57.0–56.5 | 191 | 36 (33–38) | 2053 | 37 | 538 Ah, 29.3 kWh |
| 56.5–56.0 | 180 | 33 (31–35) | 1858 | 34 | 502 Ah, 27.2 kWh |
| 56.0–55.5 | 121 | 41 (39–44) | 2310 | 41 | 469 Ah, 25.4 kWh |
| 55.5–55.0 | 110 | 54 (48–57) | 2982 | 52 | 427 Ah, 23.0 kWh |
| 55.0–54.5 | 140 | 69 (59–74) | 3758 | 72 | 373 Ah, 20.1 kWh |
| 54.5–54.0 | 201 | 91 (68–99) | 4928 | 90 | 305 Ah, 16.3 kWh |
| 54.0–53.5 | 266 | 83 (74–91) | 4468 | 88 | 214 Ah, 11.4 kWh |
| 53.5–53.0 | 315 | 54 (43–60) | 2885 | 57 | 131 Ah, 6.9 kWh |
| 53.0–52.5 | 288 | 41 (31–47) | 2181 | 50 | 77 Ah, 4.0 kWh |
| 52.5–52.0 | 198 | 35 (31–38) | 1841 | 42 | 35 Ah, 1.8 kWh |

Where the capacity sits:

- **190 Ah (25 %) is above 57.5 V**, which the monitor cannot count.
- **The curve is flattest at 54.5 to 53.5 V,** with about 87 to 91 Ah per
  0.5 V. That is 23 % of the window in one volt.
- **56.5 to 55.5 V is steep,** at 33 to 41 Ah per 0.5 V.
- **The sum of night bands from 59.5 to 52.0 V is 762 Ah / 42.3 kWh.** It
  matches the 8b cycles and the 40.9 kWh `measured_kwh_59_5_to_52_0` in
  `system.yaml`.

### 8d. Effective charge efficiency: the shunt counts about 5 % more in than out

The proposed counter in 9a was replayed over all 18 months. It sets 805 or
775 Ah at each tapered top and 0 at each 52.0 V crossing, and records the
counter's error just before each re-anchor.

| CEF | W | Top anchors, error median (IQR) | Bottom anchors, error median (IQR) | Bottom within ±5 % |
|---|---|---|---|---|
| 1.00 | 775 | +83 (27 to 183) | +21 (8 to 31) | 198/244 |
| 0.98 | 775 | +50 (8 to 120) | +14 (2 to 22) | 219/244 |
| 0.96 | 775 | +22 (−12 to 66) | +5 (−4 to 13) | 241/244 |
| **0.95** | **805** | **+1 (−30 to 24)** | **+1 (−8 to 8)** | **242/244** |
| **0.95** | **805**, top rule as in 9a (I < 36 A of either sign, anchor once) | **+4 (−3 to 12)**, n = 133, 130/133 within ±5 % | **+1 (−8 to 9)** | **242/244** |

The first four rows use a stricter top rule: 0 ≤ I < 36 A, re-fired every 4
minutes while it held. That caught only 32 tops. The last row is the rule
proposed in 9a.

- Most bottom anchors follow another bottom anchor, so they test CEF only
  and are independent of W. They centre on zero at **0.95**.
- Because NMC's real coulombic efficiency is over 99 %, the extra 4 % is
  most likely shunt gain asymmetry or a small charge-side offset rather than
  chemistry. The software counter does not need to know which: it learns
  the factor.
- **The error grows with time since the last anchor,** at CEF 0.95 and
  W 805:

  | Days since anchor | n | Median error (IQR) |
  |---|---|---|
  | 0 to 1 | 154 | +1 Ah (−9 to 9) |
  | 1 to 3 | 62 | −2 Ah (−6 to 4) |
  | 3 to 7 | 22 | +5 Ah (−4 to 16) |
  | 7 to 30 | 6 | +18 Ah (−9 to 43) |

- **Gain or offset? This data cannot tell them apart.** Over 239
  bottom → bottom spans, charge Ah and elapsed hours correlate at 0.997.
  - Gain only: CEF 0.946, rms 12.2 Ah.
  - Offset only: the shunt reads +0.92 A high, rms 10.2 Ah.
  - Both together: rms 9.9 Ah.

  A constant offset of about +0.9 A fits as well as, or slightly better
  than, the 0.95 gain. Section 9c's learner should therefore fit an offset
  term as well as CEF.
- **This revises section 3.** The ~1 % agreement in section 3 was the
  monitor against the same shunt, at the same 0.98. Against the anchors, the
  monitor over-credits charge by about 3 % of throughput. At about 600 Ah of
  daily cycling that is roughly 18 Ah/day, on top of the saturation effect,
  and it accounts for part of the 40 to 166 Ah corrections in section 3.

- The monitor's own 0x0094 = 98 is part of why its count runs high between
  syncs. Leave the monitor alone anyway. Nothing here proposes a write.

## 9. Proposed design: software amp-hour counter with its own anchors

**Not implemented. For decision.**

The counter uses the monitor's **current** and never its SOC or Ah
remaining. It integrates the shunt current on its own scale, which runs
from the measured top to the 52 V start.

### 9a. Definition

```
usable_pct = 100 × Ah_above_floor / W
W   = 805 Ah  (config: battery.window_ah; measured, see 8b/8d)
CEF = 0.95    (config: battery.counter_cef; learned, see 9c)
Ah_above_floor(t) = Ah at last anchor + ∫ I dt   (charge × CEF, discharge × 1)
```

- **100 % = top anchor.** Set Ah = W when V ≥ 59.5 and I < 36 A (either
  sign) for at least 4 continuous minutes. This is the same taper test as
  8a: it found 133 anchors in the replay, against 135 days in 8a. It fires
  **once per top**, at first qualification, and does not fire again until
  6 h later. After that the counter runs freely, so it is not held at 100 %
  while the absorption hold continues.
- **0 % = bottom anchor.** Set Ah = 0 at the 52.0 V generator start, which is
  the AGS's own trigger: the first loaded crossing of 52.0 V after at least
  2 h above 52.3 V. The window was measured to this loaded crossing, so no
  IR correction is needed.
- **Report both kWh and %.** kWh above the floor comes from the cumulative
  table in 8c, or from a parallel Wh counter with the same anchors, where the
  top is 43.7 kWh.
- **No clamping.** Below 0 % (the AGS starts below 52 V) or above 100 %
  reads as such, with a flag. Clamping is what hid the top of the pack.

### 9b. Why voltage being pushed up or dragged down does not matter

The counter never reads voltage except to detect the two anchor events.
Both are defined by the condition itself:

- **Top:** a taper means current below 36 A, so the IR rise is at most
  36 A × 4.4 mΩ ≈ 0.16 V.
- **Bottom:** the AGS trigger is by definition the loaded voltage.

A generator pushing 230 A, or a 180 A load sagging 0.8 V, only changes ∫ I
dt, which is what is wanted. There is no rest-voltage lookup on the main
path, so there is nothing to compensate.

**Optional soft anchor, for winter.** Use this only if anchors become rarer
than about once a week. When at rest (|I| < 20 A for 30 min, no generator for
30 min, PV below 50 W), compare V − I × 4.4 mΩ against the 8c table and nudge
the counter by at most 25 % of the difference. The bands' IQR is about ±10 %,
which is why it is only a nudge.

The winter question is real: there were no true tops for eight months in
2025–26. The data is present for every month, and the monthly peak was
57.8 V in Sep 2025, 55.7 to 57.0 V from Nov 2025 to Feb 2026, and 58.2 V in
Mar 2026. In winter, though, the 52 V start happens most nights, so the
bottom anchor alone keeps the error near 5 to 20 Ah (8d).

### 9c. Learning W and CEF from the counter's own errors

Each anchor logs `(time, kind, counter_Ah_before_reset, Ah_charged,
Ah_discharged since the previous anchor)`.

- **Bottom → bottom pairs** fit the charge-side bias: the counter should
  read 0 at the second one. Fit over the last 60 days either as a CEF
  (bounded to 0.90–1.00) or as a current offset in A (about +0.9 A today).
  8d cannot tell the two apart. Start with the offset form, which fit
  slightly better, and switch only if a long rest period, where hours grow
  without charge, separates them.
- **Top → bottom pairs** with charge-in below 1500 Ah measure W, as in 8b.
  Keep the median of the last several.
- **Report drift** in `battery_health`: the median anchor error and the
  current fitted CEF. That answers "is the shunt drifting" on its own
  evidence.

### 9d. Where it lives (proposal)

- **New module `agent/usable.py`, stateless.** On each call it finds the most
  recent anchor in `samples` (60 s rows, current and voltage only) and
  integrates forward from there. A restart cannot lose state, and any past
  value can be recomputed. The gateway minute history can seed it before
  late August.
- **Missing data:** gaps longer than 300 s are not integrated. The result
  carries `uncertain_since` and the Ah of the gap, estimated at the night
  load rate, until the next anchor.
- **Uses:**
  - the Alexa/status sentence (`agent.status_sentence`, `agent.py:1537`),
    which today speaks the monitor's %;
  - `soc_disagreement`, which compares against this instead;
  - optionally the dashboard.
- **Nothing decides on it.** Planning stays on volts and `energy_above`.
- **Tests:** replay fixtures of the 2025-06-02 and 2026-10-28 cycles.
  Assert the anchor errors are within 5 % at CEF 0.95, W 805.

### 9e. What this does not do

- No writes to the monitor.
- The monitor's own SOC stays as it is, on its 1800 Ah scale with the 57.4 V
  sync. If it is ever rescaled, the numbers above say the window up to its
  sync is about 550 Ah, and the top part is still invisible to it.
- No deploy. The investigation scripts are in the session scratchpad and are
  not committed.

### Reproduce

The scripts are in `agent/docs/battery_soc_2026-11/` (not committed). Run
them in this order:

1. `fetch.py` runs on KAMRUI and fetches the gateway CSVs into
   `/tmp/battwin/csv`. That location is temporary, so re-fetch when needed.
2. `load.py` and `win.py <csvdir> <history.sqlite copy>` build
   `merged.pkl`.
3. `tops.py` finds the true tops and the 52 V starts.
4. `cyc2.py` integrates the cycles.
5. `bands3.py` computes the bands.
6. `replay.py <CEF> <W>` replays the counter with the strict top rule.
   `LO=-1000 replay2.py 0.95 805` replays it with the 9a rule.

## 10. Implemented, 2026-11-08

The owner approved section 9 with these rules, and it is implemented in
`agent/usable.py`.

**The anchors**

- **0 % is the pack reaching 52.0 V, not a generator start.** The agent can
  raise a start threshold as high as 56 V, and a start at a raised threshold
  must not reset the counter.
- **0 % has a load guard.** The anchor only fires when the discharge current
  is −100 A or less. A heavier load sags the terminal voltage by up to
  0.8 V (4.4 mΩ), so a dip to 52 V under such a load is not the floor.
- **0 % re-arms** only after 2 h above 52.3 V.
- **100 % is ≥ 59.5 V with |I| < 36 A for 4 min.** This is the absolute
  current, as specified; 9a's replay accepted any discharge current. It
  fires once per 6 h.

**Replay of the module's own `scan()`.** It ran over the same 18 months of
gateway and agent minutes, at CEF 0.95 and W 805:

| Anchor | n | Median error (IQR) | Within ±40 Ah |
|---|---|---|---|
| Top | 131 | +4.5 Ah (−3 to 14) | 128 of 131 |
| Floor | 248 | +0.7 Ah (−8 to 8) | 246 of 248 |

Calibration on the result gives CEF 0.953 and a window of 800 Ah.

**Calibration**

- **Efficiency** comes from floor → floor pairs over the last 60 days. A pair
  needs at least 100 Ah of charge in, no gap, and a span of 14 days or less.
  It needs 3 pairs, and the value is held to 0.90–1.00.
- **Window** comes from a top followed directly by a floor, with no gap and
  1500 Ah or less of charge in. It takes the median of the last 5, needs 2
  pairs, and is held to 700–900 Ah.
- **Defaults** stand when there are too few pairs: 805 Ah and 0.95.
- **Every anchor is logged** in `battery_anchors`, with the counter error,
  the raw Ah in and out, any gap, and the calibration in force.

**Uncertain** is set when there has been no anchor for 7 days or more. It is
also set when more than 1 h of samples is missing since the last anchor,
because those minutes were not counted. Before the first anchor there is
no usable figure, and only the monitor's is shown.

**Endpoint.** `GET http://192.168.3.152:8090/battery` on the ask server
returns `usable_pct`, `ah_above_floor`, `window_ah`, `cef`, `last_anchor`,
`last_top`, `last_floor`, `recent_anchors`, `uncertain`,
`uncertain_reason`, `monitor_soc_pct` and `text.en` / `text.es`. It is
read-only and runs on a read-only connection.

**Shown as** "Battery 41% usable (monitor 82%)", or in Spanish "Batería 41 %
útil (monitor 82 %)". It appears in:

- `get_status`, as `battery_line` and `battery_line_es`;
- `get_battery_detail`;
- the plan record's first line and its stored data;
- the model's NOW block;
- the Telegram digest;
- the deterministic `/ask` status sentence;
- Alexa speech, both the battery and status intents;
- Alexa screens, where the big % and the battery fill use the usable value,
  with a "usable · monitor 82%" line under it. If the agent cannot be
  reached, the monitor's figure stands and the screen says "monitor".

`policy.py`, `guard.py`, `topup.py` and the Pi5 do not read it, and a test
checks that. The monitor's settings are unchanged, including its 98 % CEF.

**Day series, for the dashboard chart.** Call
`GET /battery?date=YYYY-MM-DD&step=5`, or `step=1`.

- **What comes back:** everything above, plus `day`, which holds:
  - `points`: the last sample in each step, as `ts`, `time` (12-hour,
    America/Tijuana), `usable_pct`, `ah_above_floor`, `voltage` and
    `monitor_soc_pct`;
  - `anchors`: each with `kind`, `time`, `voltage`, `error_ah` and `label`
    ("100% (tapered top)" / "0% (52.0 V floor)").
- **How it is computed:** the series is replayed from the last anchor before
  that day, using the calibration in force now.
- **What the chart will show:** a step at an anchor is the counter's error
  being corrected, and the anchor marks show where.
- **Errors:** a bad date or step returns 400.
