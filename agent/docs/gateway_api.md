# Conext Gateway (InsightLocal) HTTP API

Discovered against `https://192.168.3.131` on 2026-08-28T18:53:44 by
`scrape_gateway.py --discover`. Everything below is an observed response,
not a guess. Re-run that command to refresh this file.

## How this was found

The InsightLocal UI is an AngularJS app served as one bundle, `/combox.js`.
Its "export CSV" button calls `csvService.saveCsv('chart_data.csv', ...)`
over `chart.config.data.datasets[]` - it serialises a chart the browser
already holds, so there is no server-side CSV endpoint to call. The chart
itself is filled by `chartdataService.getChartData(device, instance, date)`,
and `batterySummaryService` calls it as `getChartData("system", 0, ...)`.

## Authentication

```
POST /auth
Content-Type: application/x-www-form-urlencoded

username=<user>&password=<password>&session=true
```

Returns `{"session": "<authToken>"}`. Send that back as an `authToken`
**header** (not a cookie) on every later request. `POST /logout` releases it.

The gateway caps concurrent sessions and answers
`429 {"status": 429, "description": "Maximum number of allowed users reached"}`
once they are exhausted - an open InsightLocal browser tab is enough to
cause it. The scraper always logs out in a `finally` block, and waits a 429
out: it sleeps 10 minutes and retries up to
6 times before giving up, so a full session queue does
not throw away a long backfill.

## Chart data

```
GET /chartdata/<device>/<instance>/<path for the resolution>
authToken: <session>
```

Returns CSV as `text/plain`. Lines starting `#` are comments; the first
surviving line is the header, whose columns are named by sysvar path.
Each resolution truncates the path at a different depth - appending the
resolution to the full day path gives 400 for days, months and years:

| Resolution | Path tail |
|---|---|
| years | `/years/` |
| months | `/years/<Y>/months/` |
| days | `/years/<Y>/months/<M>/days/` |
| hours | `/years/<Y>/months/<M>/days/<D>/hours` |
| minutes | `/years/<Y>/months/<M>/days/<D>/minutes` |

Observed on this gateway:

| Resolution | OK | Columns | Rows |
|---|---|---|---|
| minutes | yes | 27 | 1440 |
| hours | yes | 25 | 24 |
| days | no | - | 400 Client Error: Bad Request for url: https://192.168.3.131 |
| months | no | - | 400 Client Error: Bad Request for url: https://192.168.3.131 |
| years | no | - | 400 Client Error: Bad Request for url: https://192.168.3.131 |

## Units

Columns are integers and the parenthesised unit label cannot be trusted.
Verified on 2026-08-27 by summing each hourly energy column and
integrating the matching minute power column over the same day:

| Column | Hourly sum | Minutes integrated | Ratio |
|---|---|---|---|
| `/SYS/LOAD/ENERGY_HOUR(kwh)` | 32469 | 32527 Wh | 0.998 |
| `/SYS/PV_TOTAL/ENERGY_HOUR(kwh)` | 31659 | 31670 Wh | 1.000 |
| `/SYS/GEN/ENERGY_HOUR(kwh)` | 4050 | 4053 Wh | 0.999 |

So **`ENERGY_*(kwh)` values are Wh**, despite the label. Likewise
`V(V)`, `I(A)` and `T(degC)` are scaled by 0.001 (raw 53400 is 53.4 V,
raw -23960 is -23.96 A), while `SOC(%)` and `P(W)` are already in their
stated units.

### Hours header as returned

```
TIME, /SYS/DC_IN/ENERGY_HOUR(kwh), /SYS/DC_OUT/ENERGY_HOUR(kwh), /SYS/GRID_IN/ENERGY_HOUR(kwh), /SYS/GRID_OUT/ENERGY_HOUR(kwh), /SYS/LOAD/ENERGY_HOUR(kwh), /SYS/GEN/ENERGY_HOUR(kwh), /SYS/PV/ENERGY_HOUR(kwh), /SYS/GT_LOAD/ENERGY_HOUR(kwh), /SYS/GT_GRID/ENERGY_HOUR(kwh), /SYS/BATT1_CHG/ENERGY_HOUR(kwh), /SYS/BATT1_INV/ENERGY_HOUR(kwh), /SYS/BATT2_CHG/ENERGY_HOUR(kwh), /SYS/BATT2_INV/ENERGY_HOUR(kwh), /SYS/BATT3_CHG/ENERGY_HOUR(kwh), /SYS/BATT3_INV/ENERGY_HOUR(kwh), /SYS/BATT4_CHG/ENERGY_HOUR(kwh), /SYS/BATT4_INV/ENERGY_HOUR(kwh), /SYS/BATT5_CHG/ENERGY_HOUR(kwh), /SYS/BATT5_INV/ENERGY_HOUR(kwh), /SYS/BATT_CHG/ENERGY_HOUR(kwh), /SYS/BATT_INV/ENERGY_HOUR(kwh), /SYS/INV_LOAD/ENERGY_HOUR(kwh), /SYS/INV_GRID/ENERGY_HOUR(kwh), /SYS/PV_TOTAL/ENERGY_HOUR(kwh)
```

### Minutes header as returned

```
TIME, /SYS/LOAD/P(W), /SYS/GRID_NET/P(W), /SYS/GEN/P(W), /SYS/PV_TOTAL/P(W), /SYS/BATT1/V(V), /SYS/BATT1/I(A), /SYS/BATT1/T(degC), /SYS/BATT1/SOC(%), /SYS/BATT2/V(V), /SYS/BATT2/I(A), /SYS/BATT2/T(degC), /SYS/BATT2/SOC(%), /SYS/BATT3/V(V), /SYS/BATT3/I(A), /SYS/BATT3/T(degC), /SYS/BATT3/SOC(%), /SYS/BATT4/V(V), /SYS/BATT4/I(A), /SYS/BATT4/T(degC), /SYS/BATT4/SOC(%), /SYS/BATT5/V(V), /SYS/BATT5/I(A), /SYS/BATT5/T(degC), /SYS/BATT5/SOC(%), /SYS/GRID_IN/P(W), /SYS/GRID_OUT/P(W)
```

`/SYS/PV/ENERGY_HOUR` and `/SYS/PV_TOTAL/ENERGY_HOUR` carry identical
values; the scraper reads `PV_TOTAL`. Five battery banks always appear in
the minute header and the unused ones are all zeros, so the scraper takes
the first bank with a non-zero voltage rather than assuming `BATT1`.

The `days` response is month-scoped and repeats each date about 25 times,
so it is not a shortcut for a whole backfill; `hours` per day is the source.

## Devices that return charts

| device | instance | columns | rows (yesterday) |
|---|---|---|---|
| `system` | `0` | 27 | 1440 |
| `system` | `1` | 27 | 1440 |
| `system` | `2` | 27 | 1440 |
| `system` | `3` | 27 | 1440 |

## Sysvars

```
POST /vars
authToken: <session>
otk: <one-time key from the previous response's OTK field>

name=/SYS/PV_TOTAL/ENERGY_DAY,/SYS/LOAD/ENERGY_DAY
```

Returns `{"values": [{"name", "value", "quality"}], "OTK": "<next>"}`.
The agent does not depend on these - its energy counters come from Modbus
503 - but `--discover` records them as a cross-check. Observed:

```json
{
  "/SYS/PV_TOTAL/ENERGY_DAY": 30652,
  "/SYS/LOAD/ENERGY_DAY": 26980,
  "/SYS/BATT_CHG/ENERGY_DAY": 18985,
  "/SYS/BATT_INV/ENERGY_DAY": 12714,
  "/SYS/GEN/ENERGY_DAY": 4504
}
```

## What the scraper stores

`--backfill` walks backwards one day at a time. For each day:

1. **hours** is fetched first and is the primary source for `hourly`.
   These are the gateway's own energy figures, not our re-integration of
   power samples. Columns map to device rows as:

   | Column | Row | Field |
   |---|---|---|
   | `/SYS/LOAD/ENERGY_HOUR` | `load` | `wh_out` |
   | `/SYS/PV_TOTAL/ENERGY_HOUR` | `solar` | `wh_in` |
   | `/SYS/GEN/ENERGY_HOUR` | `gen` | `wh_in` |
   | `/SYS/BATT_CHG/ENERGY_HOUR` | `battery` | `wh_in` |
   | `/SYS/BATT_INV/ENERGY_HOUR` | `battery` | `wh_out` |

2. **minutes** is fetched for what the energy rows cannot give:
   per-hour mean, minimum and maximum pack voltage and mean current,
   and the voltage-to-SOC histogram. Minute rows are parsed and
   discarded; they are never stored.

### The voltage-to-SOC curve

`/SYS/BATT<n>/SOC(%)` is the only place the pack's charge curve can be
learned from years of history. Live sampling reaches the start threshold
so rarely that the 52 V projection would stay unavailable for weeks.

Minutes where the pack was discharging (`I` below zero) with no
generator running (`/SYS/GEN/P` at zero) are counted into a histogram of
(voltage bin, SOC) pairs in the `soc_curve` table - counts, not rows, so
SPEC section 5's rule against storing minute data still holds while the
distribution survives exactly. Charging minutes are excluded because a
charging pack sits well above its resting voltage.

`day` is part of the key, so re-scraping a day replaces its contribution
rather than double-counting it. `--soc-only` refills just this curve,
skipping the hours request for a history already backfilled.

A day whose energy columns are all zero is treated as empty: the minute
request is skipped, and the walk stops after
3 consecutive empty days so a gap in the gateway's
history does not end it early.

A `live` row always wins over a scraped row for the same hour, so a
backfill can be re-run over a period the agent already sampled itself.

