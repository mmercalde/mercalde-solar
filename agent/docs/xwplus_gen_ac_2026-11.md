# XW+ 5548 (slave 11) generator-AC registers, 2026-11-08

This was read-only: no writes and no restarts.

**The question.** `pi5/app.py` reads three registers on slave 11. AC2 is the
Kubota's input.

| Register | Field | Scale |
|---|---|---|
| 0x00A2 | Gen AC voltage `xwPlusAcInVoltage` | uint32 × 0.001 V |
| 0x00A4 | Gen AC current `xwPlusAcInCurrent` | uint32 × 0.001 A |
| 0x00AC | Gen AC power `xwPlusAcInPower` | uint32 W |

The lifetime Generator Input energy (0x015C) was verified on 2026-09-04. The
live values had never been checked during a Kubota run. The Kubota ran this
morning.

## 1. Did anything record them during this morning's run?

**No.** The Pi5 reads all three every 5 s into `/data` for the XW+ card, but
nothing keeps them:

- **Agent `samples`:** no `xwPlus*` column, so nothing was stored.
- **Pi5 journal:** it logs writes and events, not readings. The only slave 11
  lines are the rate-cap writes.
- **Gateway minute CSV:** the only chart this gateway serves is `system`. Its
  `/SYS/GEN/P` is all three XW units together, and the MEP was running at
  the same time, so the Kubota's share cannot be taken out of it.

**No verdict is possible for 0x00A2, 0x00A4 or 0x00AC from this run.** Read
now, with the Kubota stopped, they are 0.0 V, 0.00 A and 0 W, which is what
an idle input should read.

### What was recorded

**The run**, from the Pi5 journal and agent samples:

| Time | Event |
|---|---|
| 05:09:07 | Auto start at 52.0 V for both generators. XW+ rate cap written at **65 %** (0x016F = 65). |
| 05:09:44 | AGS reports the Kubota running. |
| 05:12 | Charge current starts. The chargers qualify the AC first. |
| 06:13:31 | Stop at 55.84 V. The rate cap ramps 50 → 25 → 10 → 0 % by 06:15:01. |
| 06:15:31 | XW+ charger disabled. |
| 06:16:02 | Kubota stopped. |

**Energy counters** (agent `counters` table, Modbus lifetime kWh, hourly at
:37):

| Unit | 04:37 | 05:37 | 06:37 | Run total |
|---|---|---|---|---|
| XW+ (slave 11) Generator Input | 798.137 | 799.525 | 801.705 | **3.568 kWh** |
| XW master Generator Input (MEP) | 1541.058 | 1542.826 | 1545.976 | 4.918 kWh |
| XW slave Generator Input (MEP) | 1528.757 | 1530.540 | 1533.664 | 4.907 kWh |
| XW+ Energy To Battery | 2.245 | 2.245 | 2.245 | **0**. This counter does not move on this unit. |

**Expected values for the next run.** The XW+ took 3.568 kWh of AC over
about 63 minutes of charging, 05:12 to 06:15 including the ramp-down.

- **0x00AC (power):** about **3.4 kW** at the 65 % cap.

  The Kubota's charge rate is **65 %, set on purpose by Michael (Nov 2026).
  Do not change it.** Older text saying 70 % is out of date: the README,
  SPEC and `system.yaml` were updated on 2026-11-08.
 Per hourly window,
  that is 1.388 kWh over 25 min ≈ 3.3 kW, then 2.180 kWh over about 38 min
  ≈ 3.4 kW.
- **0x00A2 (voltage):** about **240 V**.
- **0x00A4 (current):** about **14 A**.
- **DC side** (0x005C × 0x0050, or `xwPlusChargePower`): about **3.0 kW**,
  that is about 55 A at 54–55 V, if the conversion is about 90 % efficient.

The XW+ Energy To Battery counter is stuck, so the DC side cannot be
checked from counters. The live DC registers below will check it instead.

**Gateway cross-check.** `/SYS/GEN/P` integrates to 13.03 kWh for the run.
The three Generator Input counters sum to 13.39 kWh, a ratio of 0.97, so the
system-wide figure agrees.

### Found along the way: the gateway's clock is about 12 minutes slow

`/SYS/GEN/P` is above zero from 05:26 to 06:28 in the gateway's timestamps,
while the run was 05:12 to 06:15. Matching the gateway's battery current to
the agent's minute by minute, the best lag is **12 min** for September and
October and **13 min** for November. The mean |ΔI| drops from 8.5 A at zero
lag to 2.05 A.

Its effects:

- **The battery analysis has been re-checked.** In
  `battery_soc_2026-11.md`, the merge for Aug 28 to Nov 8 interleaved
  shifted gateway rows with agent rows. It was redone with the gateway moved
  12 min and the duplicate minutes dropped.
  - The cycles came out at 774 / 781 / 707 / 795 / 668 Ah, against
    774 / 781 / 707 / 798 / 667 before.
  - The replay of usable.py's scanner is unchanged: 131 tops, median
    +4.5 Ah; 248 floors, median +0.7 Ah.
  - The fit is CEF 0.952 and window 801 Ah.
  - The shipped defaults of 805 Ah and 0.95 stand.
  - Top times quoted in that doc's sections 8a and 8b, which are in gateway
    time, are about 12 minutes late.
- **Not fixed: the `hourly` table is in gateway time.** `scrape_gateway.py`
  stamps it with the gateway's own clock, so its hours run about 12 minutes
  late. The learned Wh-vs-V curve is built from those rows. The effect is
  small, but the fix is to set the gateway's clock or have the scraper
  correct for the offset. Both are left for the owner to decide.

## 2. What changed so the next Kubota run confirms them

There are no new Modbus reads. The agent's once-a-minute sample now also
keeps eight fields that `/data` already carries (`agent/history.py`,
`SAMPLE_FIELDS`). Each is a new `samples` column, added to an existing
database by `MIGRATIONS`:

| /data key | samples column | Register |
|---|---|---|
| `xwPlusAcInVoltage` | `xw_gen_ac_v` | 0x00A2 |
| `xwPlusAcInCurrent` | `xw_gen_ac_a` | 0x00A4 |
| `xwPlusAcInPower` | `xw_gen_ac_w` | 0x00AC |
| `xwPlusChargeCurrent` | `xw_charge_dc_a` | 0x005C |
| `xwPlusChargePower` | `xw_charge_dc_w` | 0x005E (also `chargePower3`) |
| `xwPlusBatteryVoltage` | `xw_battery_v` | 0x0050 |
| `xwPlusChargerStatus` | `xw_charger_status` | 0x007B |
| `kubotaChargeRateLive` | `kub_charge_rate` | 0x016F |

Tests are in `tests/test_xwplus_samples.py`. They check that the fields are
stored, are NULL when `/data` lacks them, and that an existing database
gains the columns.

**After the next Kubota run,** run this on KAMRUI. It is read-only:

    agent/venv/bin/python agent/docs/xwplus_gen_ac_2026-11/check_run.py YYYY-MM-DD

It reports, for the steady minutes (above 500 W), with a verdict for each:

- the median of each register;
- the power factor, W / (V × A), expected 0.85 to 1.02;
- DC / AC, expected 0.80 to 0.97;
- the rate cap seen;
- the integral of 0x00AC against the Generator Input counter's rise,
  within ±10 %.

It does nothing until solar-agent has been restarted with this change.
