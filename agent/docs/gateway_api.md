# Conext Gateway (InsightLocal) HTTP API

How the agent gets battery history out of the Conext Gateway at
`https://192.168.3.131`.

**Status.** Everything in "How this was found", "Authentication" and the
chartdata path shape below is verified — by reading the UI's JavaScript and by
probing the live gateway unauthenticated. The response bodies (CSV headers,
which devices have charts, how far history reaches) need one authenticated run:

```
agent/venv/bin/python scrape_gateway.py --discover
```

That rewrites this file in place from observed responses. Run it once after
filling in `gateway.password` in `agent/config.json`.

## How this was found

The InsightLocal UI is an AngularJS app served as a single 5 MB bundle,
`/combox.js`. Reading it:

- The chart page's **Export CSV** button is
  `csvService.saveCsv("chart_data.csv", rows, {header})` built from
  `chart.config.data.datasets[].data[]`. It serialises a chart the browser
  already holds. **There is no server-side CSV export endpoint to call.**
- The chart is filled by
  `chartdataService.getChartData(device, instance, dateData)`, which issues a
  plain `GET` to a `chartdata/...` path with an `authToken` header and returns
  CSV text.
- `batterySummaryService` calls it as `getChartData("system", 0, ...)`, and
  `energyComparisonService` likewise uses `("system", "0")`. So the Battery
  Summary chart the SPEC describes is `device=system`, `instance=0`.
- Authentication is `queryService.login()` → `POST /auth`, storing the returned
  `session` value in `sessionStorage.authToken`.

Verified against the live gateway without credentials:

| Request | Response | Meaning |
|---|---|---|
| `GET /chartdata/system/0/years/` | 401 | path exists, needs auth |
| `GET /chartdata/system/0/years/2026/months/8/days/28/minutes` | 401 | path exists, needs auth |
| `GET /vars` | 400 | exists, wants POST |
| `GET /auth` | 400 | exists, wants POST |
| `GET /nonsense/path` | 404 | control: unknown paths really do 404 |

## Authentication

```
POST /auth
Content-Type: application/x-www-form-urlencoded

username=<user>&password=<password>&session=true
```

Returns `{"session": "<authToken>"}`. Send that value back as an `authToken`
**header** (not a cookie) on every subsequent request. `POST /logout` releases
the session.

### Session limit

The gateway caps concurrent sessions. Once they are used up it answers:

```json
{"status" : 429, "description" : "Maximum number of allowed users reached"}
```

This was observed live — an open InsightLocal browser tab, or the
`mercalde-solar.org` proxy, is enough to hold a slot. `scrape_gateway.py`
therefore logs out in a `finally` block on every path, including errors. If a
scrape fails with 429, close an InsightLocal tab and retry.

## Chart data — the history source

```
GET /chartdata/<device>/<instance>/years/<Y>/months/<M>/days/<D>/minutes
authToken: <session>
```

Returns CSV as text, **one local day per request** at one-minute resolution.
Lines beginning `#` are comments and blank lines are padding; the first
surviving line is the header. The SPEC's expected columns are
`Date, Volts(V), Current(A), Temperature(°C), State Of Charge(%)`; the parser
matches column names case-insensitively on a substring so minor spacing or
unit differences do not break it.

Other resolutions replace the tail of the path (from `getChartData`):

| Resolution | Path tail |
|---|---|
| minutes | `/years/<Y>/months/<M>/days/<D>/minutes` |
| hours | `/years/<Y>/months/<M>/days/<D>/hours` |
| days | `/years/<Y>/months/<M>/days/` |
| months | `/years/<Y>/months/` |
| years | `/years/` |

### Other devices

`getChartData` takes `device` and `instance` as free path segments, so the
Selection dropdown's other entries (XW units, MPPTs) are reachable by the same
pattern. `--discover` probes a candidate list and records which combinations
return a chart; scrape them with
`--day <date> --device <dev> --instance <n>`.

## Sysvars

A second, unrelated API used by the dashboard pages:

```
POST /vars
authToken: <session>
otk: <one-time key from the previous response's OTK field>

name=/SYS/PV_TOTAL/ENERGY_DAY,/SYS/LOAD/ENERGY_DAY
```

Returns `{"values": [{"name", "value", "quality"}], "OTK": "<next>"}`. The
`OTK` rotates on every response and must be echoed on the next call.
`POST /ns/get` is the unauthenticated variant.

Useful keys seen in the bundle, in
`{PV_TOTAL,LOAD,GRID_IN,GRID_OUT,GEN,BATT_INV,BATT_CHG}` ×
`ENERGY_{DAY,WEEK,MONTH,YEAR,LIFETIME}` form, e.g.
`/SYS/PV_TOTAL/ENERGY_LIFETIME`.

The agent does **not** depend on these: its energy counters come from Modbus
503 (see `energy_registers.md`). `--discover` records them only as a
cross-check.

## What the scraper stores

Minute rows are parsed and thrown away. Only per-hour aggregates reach the
`hourly` table (device `battery`, source `insightlocal`):

- `mean_v`, `min_v`, `max_v`, `mean_a`
- `wh_in` / `wh_out`, integrated as `V * A / 60` per minute row and split by sign

A `live` row always wins over a scraped row for the same hour, so a backfill can
be re-run safely over a period the agent has already sampled itself.

`--backfill` walks backwards one day at a time and stops after 3 consecutive
empty days, so a gap in the gateway's history does not end the walk early.
