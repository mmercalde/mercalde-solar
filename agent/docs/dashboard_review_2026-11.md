# Dashboard review, 2026-11-08

Scope: `mercalde-solar.org/dashboard`, served by `pi5/app.py` (Flask under
gunicorn, 1 worker x 4 threads, port 8080 on the Pi5, reached from the VPS over
WireGuard). The deployed `app.py` on the Pi5 is byte-identical to `4c98716`
(md5 `4020e212...`), so everything below is what is live.

The same process runs the Modbus poll (every 5 s) and the automatic generator
loop. Nothing in this review proposes a change to either.

## 1. What the page shows today

All live values come from one endpoint, `GET /data`, which returns the
`system_data` dict the poll thread fills every `POLL_INTERVAL = 5 s` with ~35
Modbus reads against the gateway at 192.168.3.131:503. The page polls it every
5 s. `/config` (thresholds + last 50 events) is polled every 15 s and
`/agent/plan` (proxied to KAMRUI:8090) every 60 s.

| Panel | Shows | Source | Refresh |
|---|---|---|---|
| 3D stage | three.js model of the house, arrays and gen building; per-array glow from live PV watts, battery modules lit by SOC, sun position scrubber, shadows | `/data` + `SITE` geometry in the page | 5 s |
| HUD top-left | site name | static | - |
| HUD top-right | clock time of the last poll, poll error count, agent badge (dot + plan), "Ask the agent" box with Plan/Status shortcuts, links to Registers and AC Diagnostic | `/data.clockTime`, `/data.pollErrors`, `/agent/plan`, `POST /agent/ask` | 5 s / 60 s |
| HUD bottom | Solar W and % of array, battery SOC ring + volts + "time left", load W + battery flow | `/data` | 5 s |
| HUD gen chips | MEP / KUB running / auto / off | `mep803aAction`, `kubotaAction`, modes | 5 s |
| Overview: Total Solar | sum of the three charge controllers, % of nameplate, bar, 28-bar sparkline | `mppt80PVPower + southArrayPVPower + westArrayPVPower` | 5 s |
| Overview: Battery | SOC ring, volts, net battery W | `batterySOC` (battery monitor 191, reg 0x004C), `batteryVoltage` (191, 0x0046), `battPower` (shunt I x V) | 5 s |
| Overview: AC Load | XW Pro 10 + 12 AC output W, "AC charge" W | `acPower1+acPower2`, `chargePower1+2+3` | 5 s |
| Inverters | XW Pro master (10), slave (12): AC W, A, charge W. XW+ 5548 (11): charge W, stage, DC A, V, Kubota AC-in, rate cap, alarm | inverter regs | 5 s |
| Solar Arrays | MPPT 80, MPPT 150 south (gen building), MPPT 150 west (house): W, charge state, PV V, bar vs per-array nameplate | charger regs | 5 s |
| Generators | MEP-803A and Kubota: mode, AGS action + on-reason / fault, live charge rate, Off/On/Auto selector + Set | AGS 51 / 50 regs | 5 s |
| Control & Settings | auto-gen enable, start/stop V, charge rate, max runtime, cooldown per gen; ramp-down; Telegram | `/config` (writes `config.json` on the Pi5) | 15 s |
| Event & Error Log | last 50 `log_event` lines, plus agent ask/answer blocks | `/config.events` (in memory) | 15 s |
| Footer | "uptime" | `/data.lastUpdate` | 5 s |

Other pages: `/registers` (raw Modbus read/write tool) and `/diagnostic` (AC
diagnostic snapshot / stream / log).

### Broken, slow or misleading

1. **"Peak -- kW" never fills in.** `id='capKw_value'` appears twice (HUD
   bottom-left and the Overview "Total Solar" card). `DOMContentLoaded` sets it
   with `getElementById`, which returns only the first, the HUD one. The
   Overview card keeps its placeholder `--` forever. *Fixed on this branch*:
   both spans are now `class='capKw'` and set together.
2. **"Peak" is not a peak.** The number is nameplate: panel count x 225 W from
   the page's `SITE` model (`CAPS.total`), not anything measured. Once it
   shows, "Peak 12.4 kW" reads like today's best. *Fixed on this branch*:
   relabelled "Array"; today's measured peak is in the History section.
3. **The SOC % is the battery monitor's own SOC and nothing else.**
   `batterySOC` and `battSocBM` are both register 0x004C on device 191, read
   twice per poll. That number is the monitor's coulomb count and drifts until
   it re-syncs on a full charge (the separate battery SOC investigation is
   looking at exactly this; not touched here). Two presentation problems that
   are *not* the monitor's fault:
   - A failed read becomes `0`, so a Modbus hiccup shows **0 %** and an empty
     red ring rather than "--". Same for battery volts (0.0 V).
   - Nothing on the page says the figure is the monitor's estimate, or how
     long since it last synced. Next to "52.3 V" it reads as fact.
   *Fixed later on this branch (owner's request)*: the poll adds
   `batteryVoltageOk` / `batterySocOk` to `/data` (the 0 values themselves are
   unchanged for every other reader) and the page shows "--" and a dimmed ring.
   The second point is for the battery SOC work: the card will show its
   "usable %" next to the monitor's figure.
4. **The sparkline is 2 minutes 20 seconds.** 28 bars at one per 5 s poll,
   in memory, empty after every page load. It looks like a day's curve and is
   not. The History section replaces the need; the sparkline could go or be
   relabelled "last 2 min".
5. **"uptime" in the footer and `lastUpdate` in `/data` are process uptime**,
   wrapping at 24 h (`(elapsed//3600)%24`). Known and kept for the ESP32 and
   Alexa; the header correctly uses `clockTime`. The 24 h wrap makes "uptime
   03:12:00" ambiguous after the first day.
6. **The 3D view depends on the internet.** The page asks for
   `/vendor/three.min.js` first, but `pi5/vendor/` is gitignored and absent on
   the Pi5 (`ls /home/michael/solar_dashboard/vendor` -> no such directory),
   so `/vendor/three.min.js` is a 404 and the page falls back to cdnjs. On the
   LAN with the uplink down the 3D stage shows "Loading renderer..." and an
   error. *Fixed on this branch*: three.js r128 is committed in `pi5/static/`
   and `/vendor/three.min.js` serves it from there (no nginx change); cdnjs is
   only the second fallback.
7. **"AC charge" on the Load card is DC charge power.** It is
   `chargePower1+2+3`, register 0x005E "charge DC power", the generators'
   contribution into the pack through the inverters. Label says AC.
   *Fixed on this branch*: "DC charge power".
8. **Event log is in memory.** `log_event` keeps the list in the process;
   every restart of `solar-dashboard` empties it, so after a deploy "what
   happened last night" is blank on the dashboard (the agent's DB still has
   runs and actions).
9. **Times are 24-hour** in the header clock (`%H:%M:%S`) while everything
   the agent writes is 12-hour. The History section uses 12-hour,
   America/Tijuana throughout.
10. **No history at all.** The Pi5 keeps nothing across restarts; every
    number on the page is "now". This is the main gap (sections 2 and 3).
11. Minor: the page declares `const history=...` at top level, shadowing
    `window.history`; the generator Set buttons give no confirmation; the
    poll-error banner counts AGS-offline reads the same as gateway failures.

Speed: `/data` is a dict copy under a lock, sub-millisecond. The page itself
is ~190 KB of inline HTML/JS per load (no caching, it is a Python string), plus
three.js from the CDN. On a phone over the VPS that first load is the slow
part; `/data` is not.

## 2. InsightLocal (gateway 192.168.3.131) vs ours

Sourced from `agent/docs/gateway_api.md` and the InsightLocal UI; no gateway
session was opened for this review (the gateway caps sessions and a stray one
pushes the agent's scraper into 10-minute 429 waits).

| InsightLocal | What it does | Ours today |
|---|---|---|
| Battery Summary | Volts, Current, Temperature, SOC for a chosen day at 1-min resolution; drag-zoom; legend toggles; export PDF / PNG / CSV (CSV is the browser serialising the chart) | nothing |
| Power Flow | live flow diagram PV -> battery -> load, generator in | live numbers + 3D view (equivalent or better) |
| Battery Comparison | two days' battery curves overlaid | nothing |
| Energy | daily/monthly bars: PV, load, generator, battery charge/discharge | nothing |
| Energy Comparison | two periods' energy side by side | nothing |

What we lack, in order of value:

1. A day view of battery V / A / SOC (temperature: see below).
2. Day view of power: per-array solar, load, generator, battery.
3. Daily energy bars for a month.
4. Day-vs-day comparison.
5. Export (CSV is easy; PNG via the canvas; PDF is the browser's print).

What InsightLocal lacks and we can add because the agent knows it: generator
start/stop thresholds as lines on the voltage chart, generator runs as shaded
bands (and which generator, and why it started), sunrise/sunset, and
per-array solar (InsightLocal only has PV_TOTAL).

## 3. Where history lives

**Pi5:** nothing durable. `system_data` (current values), `auto_gen_state`
events (in memory), `config.json` (settings), `ac_diag_log.json` (AC
diagnostic captures only).

**KAMRUI** `~/mercalde-solar/agent/data/history.sqlite` (65 MB, WAL), written
by `solar-agent`:

| Table | Grain | Span (2026-11-08) | Has |
|---|---|---|---|
| `samples` | one `/data` poll per 60 s | 2026-08-28 -> now, purged at 90 days (101k rows) | battery V, monitor SOC, shunt A and W, Ah remaining, AC out 1+2, PV per array, gen action/mode/on-reason/start-origin, AGS online, poll errors |
| `hourly` | per hour per device | `insightlocal` rows 2025-05-03 -> 2026-08-28; `live` rows (rolled up from samples) after | battery mean/min/max V, mean A, Wh in/out; solar, load, gen Wh; per-array Wh (live only) |
| `daily` | per local day | 556 days | solar Wh, load Wh, MEP and Kubota minutes, peak and min V |
| `gen_runs` | per run | 92 runs | gen, start/stop, start/stop V, mean A, load W, gross W, fuel, on-reason, kind (auto/manual/exercise) |
| `counters` | gateway energy registers, hourly at :37 | 2026-08-28 -> now | per-device and system pv, load, generator, battery_in/out (today/month/year/lifetime kWh) |
| `soc_curve` | histogram | backfill | voltage-bin -> SOC counts |

**Not recorded anywhere we own:**

- **Battery temperature.** Only in the gateway's minute chart CSV
  (`/SYS/BATT1/T`). The Pi5 does not read the monitor's temperature register,
  and adding it is battery-monitor work, so it is out of scope here.
  Decision (2026-11-08): the pack is climate-controlled (28-31 °C in the
  gateway logs), so the History section leaves temperature out rather than
  showing an empty series. If it is wanted later: read the monitor
  temperature in the poll and add it to `SAMPLE_FIELDS`.
- **Generator charge power.** `chargePower1/2/3` are in `/data` but not in
  `SAMPLE_FIELDS`. The History API estimates it the way `gen_runs.gross_w`
  does: on minutes a generator is running, `battery W + load W - solar W`.
  Labelled "est." Follow-up: add `chargePower1..3` to `SAMPLE_FIELDS` (an
  agent change; the column is cheap).
- Minute data before 2026-08-28 (only hourly from the InsightLocal backfill),
  and minute data older than 90 days going forward.

## 4. Recommendation: how the dashboard gets history

**A small read-only history API on KAMRUI, as its own process, proxied by the
Pi5.**

- `agent/history_api.py`, its own systemd unit `solar-history.service`,
  bound to `192.168.3.152:8091`. It opens the database `mode=ro` per request
  and never writes. Being a separate process means deploying or crashing it
  never touches `solar-agent` (whose restart costs a ~50 s prompt-cache
  rebuild and sits next to the guard and top-up logic), and `solar-agent`
  restarts never take history down.
  - Not inside `ask_server.py` on 8090: same data, but every history change
    would ship as a `solar-agent` restart.
  - Not on the Pi5: the data is on KAMRUI, so a Pi5 API would either copy the
    database across (a second writer of truth) or still have to call KAMRUI.
    It would also put more code into the process that runs the generator loop.
- The Pi5 gets thin proxy routes `/history/api/*` (like `/agent/plan`): 5 s
  timeout, never raises, answers `{"online": false}` with 503 when KAMRUI is
  down. They live in `pi5/history_routes.py` (a Flask blueprint) so `app.py`
  only gains two lines and the proxy is testable without starting the poll.
- One URL prefix, `/history/...`, for the API and the static files
  (`/history/static/uplot.min.js`), so the VPS needs exactly one new nginx
  `location /history` block. Without it the request falls through to
  `location /` and lands on InsightLocal.
- **If KAMRUI is down** the History section says so in one line ("History is
  on the KAMRUI and it is not answering. Live values above are unaffected.")
  and keeps the date controls so it retries on the next change. The live
  dashboard, the poll and the generator loop do not depend on it in any way.
  Not done: a Pi5-side ring buffer of today's minutes as a fallback. It would
  mean touching the poll thread, and KAMRUI being down is rare enough that the
  banner is the right first answer.

## 5. What branch `dash-graphs` adds

- `agent/history_api.py` + `agent/solar-history.service`: the API above,
  `GET /history/api/{health,day,energy}`. A day is <= 1440 points (one per
  minute, later sample wins, a null row across any gap > 5 min); days before
  the minute samples come back `resolution: "hour"` from the InsightLocal
  backfill; a day with nothing is `resolution: "none"`. Energy is per local
  day for up to 400 days.
- `pi5/history_routes.py`: blueprint with `/history/static/*` (from
  `pi5/static/`) and the `/history/api/*` proxy (5 s timeout, allow-listed
  names and arguments, gzip for large answers).
- `pi5/static/`: uPlot 1.6.31 (MIT, licence alongside), `history.js`,
  `history.css`.
- `pi5/app.py`: the History section (closed by default; its files load the
  first time it is opened and it remembers open/closed per browser), the
  `capKw` fix and "Peak" -> "Array". Two lines register the blueprint. Nothing
  else in the file changed.
- `vps/nginx/default`: `location /history/`.

**Chart library: uPlot**, not Chart.js. 50 KB for everything used here
(Chart.js is ~200 KB and needs chartjs-plugin-zoom, hammer.js and a date
adapter to match), drawn for exactly this shape of data: several thousand
time-stamped points across stacked panels with a shared, synced cursor and
zoom; drag-zoom and click-to-hide legend are built in; time zones come from
`uPlot.tzDate`, so every axis and readout is America/Tijuana whatever the
viewer's device is set to. Cost: pinch-zoom and grouped bars are not built in;
both are a few dozen lines in `history.js`.

Design choices worth knowing:

- Small multiples, one unit per chart (voltage, current, SOC, power, solar by
  array) rather than InsightLocal's four-axis chart: they share a time axis,
  cursor and zoom, so reading across is the same, and no value is read off the
  wrong axis.
- Colours were checked with a colour-vision validator against the dark panel;
  each chart has at most three line hues, and battery power is a filled area.
- Threshold lines are the *current* settings from `/config`; the page says so.
- Comparison draws the second day dashed on the first day's clock.

## 6. Deploy (not done; needs the owner's OK)

1. KAMRUI: fast-forward `agent` to `dash-graphs`, `git pull --ff-only` in
   `~/mercalde-solar`; `sudo cp agent/solar-history.service /etc/systemd/system/`,
   `sudo systemctl daemon-reload && sudo systemctl enable --now solar-history`.
   Starts one new process. `solar-agent` is **not** restarted; its code on disk
   changes only by the new files.
2. Pi5: copy `pi5/app.py`, `pi5/history_routes.py`, `pi5/static/` to
   `/home/michael/solar_dashboard/` (backup app.py first, per README), then
   `sudo systemctl restart solar-dashboard`. **This restarts the Modbus poll
   and the automatic generator loop** (gunicorn, one worker), clears the
   in-memory event log and the start-origin memory, and for ~10 s the VPS
   fails over to the ESP8266 backup. Same windows as any dashboard deploy:
   no generator running, after sunrise, not 8:30-11:00 pm.
3. VPS: install the new `vps/nginx/default`, `sudo nginx -t && sudo systemctl
   reload nginx`. Without it the section works on the LAN but not on
   mercalde-solar.org.

Rollback: Pi5 `cp` the backup app.py back and restart; the KAMRUI service can
simply be stopped; nginx block removal is inert.

## 7. Usable % (added after the battery work landed)

The battery card, its ring and the HUD show the battery work's usable figure
from `usable.py`, as "26% usable (monitor 81%)". The Pi5 proxies
`/history/api/usable` to the agent's own `GET /battery` on 8090 and takes the
number as given. When the agent does not answer, the page shows the monitor's
SOC and labels it "monitor SOC". The History SOC panel draws usable % from
`/battery?date=&step=1` next to the monitor's SOC, with usable.py's anchors
(full / 52.0 V) as dashed verticals.
