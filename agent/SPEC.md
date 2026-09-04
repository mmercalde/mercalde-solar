# Solar Agent — Build Specification (final)

Build a local-LLM agent that plans generator use for an off-grid solar system from load history and weather forecast. It runs on the KAMRUI mini PC, reads the existing Flask dashboard on the Pi5, adjusts the dashboard's generator thresholds through its existing `/config` endpoint, and talks to the owner over Telegram. The LLM decides; a deterministic guard layer vetoes; the Pi5 executes.

Repo: `https://github.com/mmercalde/mercalde-solar.git`, branch `agent` off `main` (tag `pre-agent` marks the starting point). All new code goes in a new top-level directory `agent/`. Under `pi5/` the only permitted addition is `pi5/agent_watchdog.sh`; `pi5/app.py` must not be modified. `vps/alexa_solar.py` gets one new intent branch (§10). `docs/NETWORK.md` gets one correction (§12).

Do not invent endpoints, register addresses, or data keys. Everything the agent touches is listed here; if something needed is missing, stop and ask.

---

## 1. Hosts and services

| Host | Address | Role |
|---|---|---|
| KAMRUI | 192.168.3.152 (LAN); reachable from VPS via WireGuard through Pi5 | Runs the agent and the LLM. Ryzen 7 5825U, 22 GB RAM, Ubuntu with desktop. |
| Pi5 | 192.168.3.10:8080 on the Schneider segment | Flask dashboard (`pi5/app.py`, gunicorn). Owns all generator start/stop logic. |
| VPS | 45.32.131.224 | nginx, WireGuard hub (10.8.0.1), Alexa skill backend `alexa-solar.service` running `/var/www/alexa_solar.py` on 127.0.0.1:5000 |
| Gateway | 192.168.3.131 | Schneider Conext Gateway; Modbus TCP port 503; InsightLocal web UI (also proxied at `https://mercalde-solar.org/#/…`) |

LLM on KAMRUI (already running, systemd):
- `llama-server.service`: stock **Qwen3-8B-Q4_K_M**, `http://127.0.0.1:8080`, 32k context, `--jinja` enabled — **use this one**
- `llama-server-abliterated.service`: port 8081 — leave alone, it serves Open WebUI
- API: OpenAI-compatible `POST /v1/chat/completions` with standard `tools` array. Tool calling verified working. No Ollama.

Memory note: two 8B models resident is ~20 GB of 22. If the agent's tests show swap pressure, report it; the fix is stopping the abliterated service, not shrinking the model.

## 2. Environment

- Python 3.11+, venv in `agent/venv`
- Dependencies: `requests`, `apscheduler`, `pytest`. stdlib `sqlite3`. Keep it minimal.
- Config: `agent/config.json` (gitignored), ship `agent/config.example.json`:

```json
{
  "dashboard_url": "http://192.168.3.10:8080",
  "llm_url": "http://127.0.0.1:8080/v1/chat/completions",
  "llm_model": "qwen3",
  "telegram": {"token": "", "chat_id": ""},
  "gateway": {"url": "https://192.168.3.131", "user": "Admin", "password": ""},
  "lat": 32.36, "lon": -117.06, "tz": "America/Tijuana",
  "tick_minutes": 15,
  "digest_hours": [7, 19],
  "start_voltage_min": 52.0, "start_voltage_max": 56.0,
  "stop_voltage_min": 54.5,  "stop_voltage_max": 57.0,
  "default_start": 52.0, "default_stop": 54.5,
  "solo_peak_threshold": 57.0,
  "solo_select_voltage": 55.0,
  "solo_target": 57.0,
  "ags_max_run_hours": {"mep": 3, "kubota": 3},
  "exercise": {"mep_days": 5, "kubota_days": 3, "start": "09:00", "minutes": 30},
  "learning_live_days": 7,
  "ask_port": 8090
}
```

- Telegram: same bot token and chat ID the dashboard uses. The dashboard only sends; the agent is the sole consumer of `getUpdates`.
- Weather: Open-Meteo, no key. Hourly cloud cover, `shortwave_radiation`, temperature, sunrise/sunset.

## 3. Dashboard interface (read)

`GET /data` returns JSON. Keys used:

| Key | Meaning |
|---|---|
| `batteryVoltage` | Pack volts |
| `battSocBM` | SOC % from Battery Monitor shunt (authoritative; ignore `batterySOC`) |
| `battPower`, `battCurrent` | True battery W / A from shunt, signed (negative = discharging) |
| `battAhRemaining`, `battMinToDischarge` | Battery Monitor projections |
| `battMonitorOnline` | If false, treat SOC/power as unknown |
| `acPower1`, `acPower2` | AC output W per XW Pro; house load = sum (when no generator is running) |
| `mppt80PVPower`, `southArrayPVPower`, `westArrayPVPower` | Solar W per controller; total = sum |
| `mep803aAction`, `kubotaAction` | 9 = running, 10 = stopped |
| `mep803aMode`, `kubotaMode` | 0 off, 1 on, 2 auto |
| `mepOnReason`, `kubotaOnReason` | AGS register 0x0044 as text: `not_on`, `dc_voltage_low`, `battery_soc_low`, `ac_current_high`, `contact_closed`, `manual_on`, `exercise`, `non_quiet_time`. The only source for why a generator started. `null` if the read failed |
| `mepOffReason`, `kubotaOffReason` | AGS register 0x0045 as text; `manual_off`, `exercise_done`, `quiet_time` are named, anything else comes through as `code_N` |
| `mepExercise`, `kubotaExercise` | `{every_days, minutes, start, start_raw}` from AGS registers 0x006F–0x0071, re-read hourly. The exercise schedule, which the agent used to carry as a hardcoded 09:00 and get wrong |
| `mepAgsOnline`, `kubotaAgsOnline` | AGS reachable |
| `pollErrors` | Cumulative Modbus errors |
| `autoGenEnabled` | Pi5 auto-gen logic active |
| `lastUpdate` | Timestamp of the snapshot |

`GET /config` returns the live generator thresholds: for each of `mep` and `kub`: `startVoltage`, `stopVoltage`, `chargeRate`, `maxRuntime` (minutes), `cooldown`. Read every tick.

`GET /acdiag` for per-inverter AC voltage/frequency/power/current when investigating an AC anomaly.

Gateway Modbus (port 503, via `pi5/schneider_modbus.py`, copy it into `agent/`): energy counters Today / This Month / This Year / Lifetime kWh per MPPT and per XW, Battery Monitor in/out. Read once an hour into the `counters` table, off the tick and spaced: on the tick these reads collided with the Pi5's own 5-second poll of the same gateway. Register addresses are in the spec PDFs in the repo (`Conext_*_Modbus_503_spec_*.pdf` — these are ZIP archives of numbered `.txt` pages; `unzip` then `grep`). Use the addresses from those documents, not from memory.

## 4. Dashboard interface (write) — the only lever

`GET /config?mep.startVoltage={a}&mep.stopVoltage={b}&kub.startVoltage={c}&kub.stopVoltage={d}`

That is the agent's sole write. It never calls `/setgen`, `/stopgen`, `/writereg`, `/setmpptmode`, `/autogen`, or any other endpoint, and never sends `chargeRate`, `maxRuntime`, or `cooldown` parameters.

How generator behaviour is produced from this one lever:

- **Normal**: both generators share the same start and stop. Pi5 starts *both* when voltage falls below start and stops each at stop.
- **Pre-charge before bad weather**: raise both starts (and set stop to 56–57) so the run happens now instead of at 3 a.m.
- **Solo top-up**: raise *one* generator's start above the current voltage so only it fires; leave the other at `default_start` as a backstop.
- **Return to default** once the reason has passed.

## 5. Data stores

`agent/data/history.sqlite`:

| Table | Source | Rows |
|---|---|---|
| `samples` | `/data` every 60 s | one per minute; rolled up into `hourly` and deleted after 90 days |
| `hourly` | rollup of `samples` + scraped InsightLocal history | per hour per device: mean V, mean A, Wh in, Wh out, min/max V, source tag |
| `daily` | rollup of `hourly` | per day: solar Wh, load Wh, gen minutes per gen, peak V, min V |
| `counters` | Gateway Modbus energy registers, hourly at :37 | cross-check for `daily` |
| `gen_runs` | derived from `*Action` transitions | start, stop, gen, duration, start V, stop V, observed charge rate (V/h and A), `kind` = `auto` / `agent` / `exercise` / `manual` |
| `plans` | every tick (§8) | the plan record |
| `actions` | every guard decision | mirrors `audit.log` |

`agent/scrape_gateway.py`: logs into InsightLocal and replays the chart CSV export (Dashboard → Battery Summary → Battery 1 → date → CSV; the file has columns `Date, Volts(V), Current(A), Temperature(°C), State Of Charge(%)` at one-minute resolution, one day per request). Discover the underlying HTTP request by fetching the page and reading its JavaScript, or by using a headless browser; document what you find in `agent/docs/gateway_api.md`. Walk backwards one day at a time until the export is empty, downsample each day to `hourly` immediately, never store minute rows. Check the Selection dropdown for other devices (XW units, MPPTs); if per-device charts exist, scrape those too with the same pattern. Then run nightly for yesterday.

Exercise runs are tagged from the AGS's own `mepOnReason` / `kubotaOnReason`, which is authoritative: reason `exercise` is an exercise whatever the clock says. `kind` comes from that reason for every run — `manual_on` → `manual`, `dc_voltage_low` still splits into `agent` or `auto` by who last moved the threshold, and any other code stands as itself. The start-time heuristic (within 5 min of the scheduled time, ≤ duration + 5 min) is the fallback for runs whose reason never reached the sample, and it logs when it is used. The schedule it measures against comes from `mepExercise` / `kubotaExercise`, per generator; `system.yaml` holds only a fallback. Exercise runs are excluded from the load model, charge-rate estimates, fuel figures, and anomaly triggers.

The heuristic alone was wrong: it had both generators at 09:00, and on 2026-09-03 the Kubota exercised at 6:49 PM and was filed as an ordinary auto-start.

Load model (`agent/loadmodel.py`, pure Python, no LLM):
- Hourly load profile by hour-of-day and weekday/weekend, seasonal (by month) from `hourly`
- Overnight drawdown Wh (sunset → sunrise) by month
- Solar yield vs forecast cloud cover, learned per month
- Charge rate per generator (solo and paired) from `gen_runs`
- Excludes samples where either generator is running and all `exercise` runs

## 6. Tools exposed to the model

Standard OpenAI tool schemas in `agent/tools.py`; each maps to a Python function.

Read (always allowed):
- `get_status()` — condensed `/data` + `/config`, plus per generator `run_reason` (the AGS's, never inferred) and `started_at` / `running_minutes` while it runs
- `get_history(hours | window)` — min/max/avg V, solar Wh, load Wh, battery Wh out, gen minutes
- `get_load_forecast(hours)` — expected Wh from the load model for the next N hours, and projected time to reach 52.0 V
- `get_gen_runtime(days)` — per-gen totals and run list with charge rates
- `get_weather()` — next 48 h summary
- `get_ac_diag()`
- `send_telegram(text)`

Write (through guard):
- `set_gen_thresholds(mep_start, mep_stop, kub_start, kub_stop, reason)`

A field that is not a measurement carries a note saying what it is instead. `load_w` is null while a generator is feeding the inverters, and reads zero for a few seconds during an AC transfer; both were once reported as "no load is being drawn". Where a tool cannot answer, it says so in the field rather than going quiet, because quiet is what the model fills in from the voltage.

Every time-word the owner uses — "overnight", "this month", "December" — is a
literal argument value the tool takes, never something the model has to
translate into a number. A model asked for "overnight" with only `hours=` will
pass 24 and report a day as a night; asked for December with only `months=` it
will pass 1 and report September. Both happened. The word is the argument.

## 7. Guard — hard rules, not model-editable

`agent/guard.py`. Every write passes `guard.check(...)`; refusals return a reason string as the tool result.

1. Bounds: each start in `[start_voltage_min, start_voltage_max]`, each stop in `[stop_voltage_min, stop_voltage_max]`, stop − start ≥ 2.0 for each gen.
2. No-op: refuse if all four values equal the current `/config` values.
3. Running gen: if a gen's `*Action == 9`, refuse lowering its stop. Raising is allowed. Its start is irrelevant mid-run.
4. Reachability: for any gen whose start is above current voltage (i.e. it will fire now), `(stop − current_V)` must be achievable at that gen's observed charge rate within `min(Pi5 maxRuntime, ags_max_run_hours)`. If no observed rate exists yet, refuse and tell the model to use the default thresholds.
5. Rate: at most one write per 60 minutes, excluding the heartbeat (§9).
6. Learning gate: refuse until `hourly` contains the current calendar month from at least one prior year AND `samples` covers ≥ `learning_live_days` consecutive days.
7. Stale data: refuse if `/data` `lastUpdate` is older than 5 min or `battMonitorOnline` is false.
8. Owner override: if `/config` differs from what the agent last wrote (owner changed it in the dashboard UI), adopt the owner's values and make no writes for 6 hours; log it.
9. Every check, pass or refuse, is written to `agent/data/audit.log` (timestamp, args, reason string, V, SOC, result) and to the `actions` table.

## 8. Agent loop and plan record

`agent/agent.py`:

- **Tick every 15 min, day and night.** Build the prompt: condensed status, `/config`, last 24 h summary, load forecast to sunrise (or next 12 h), weather 24 h, today's peak voltage, time. The model may call up to 4 tools then must end with either "no change" or one `set_gen_thresholds` plus a `send_telegram` explaining it.
- **Plan record**, written to `plans` every tick and shown on request, computed by Python except the last two lines which come from the model:

```
2026-08-28 16:00  V 55.8  SOC 84%  load 1.1 kW
peak today: 55.8 V  (threshold 57.0 → solar shortfall)
overnight Wh (profile, Aug weekday): 10,800
projected 52.0 V at: 04:10   sunrise 06:31
next daylight (Fri Aug 29): 20% cloud, est. solar 61 kWh (Aug clear-day 68)
recommend: Kubota solo, start 56.0 / stop 57.0; MEP 52.0 / 54.5   — peak <57, >55 so Kubota
applied: no (learning phase)
```

- **Digests** at 07:00 and 19:00 local. 19:00 includes tonight's plan. 07:00 reports last night's projection vs what happened (predicted time to 52 V vs actual, gen runs).
- **Telegram inbound**: long-poll, accept only the configured chat ID, pass text to the model with the same tools. "plan" returns the latest plan record verbatim. Language follows the message.
- **`POST /ask`** on `ask_port`, bound to 192.168.3.152 only: `{"text": "...", "lang": "en"|"es"}` → same handler as Telegram inbound → plain-text reply ≤ 60 words (for speech). `GET /plan` returns the latest plan record as JSON.
- **Anomaly triggers** (deterministic, wake the model immediately, 30-min cooldown each): `pollErrors` +10 in 5 min; either `*AgsOnline` goes false; one array < 30% of the others' average for 30 min in daylight; V < 52.5 with no gen running and `autoGenEnabled` false; V < 51.0 regardless.
- **Learning phase**: everything above runs, but the guard refuses writes and the plan record says so. Telegram actions are replaced by "would have set …".

## 9. Heartbeat and Pi5 watchdog

- Agent: every tick, re-send its currently intended thresholds via `/config` even when unchanged. This is the heartbeat and is exempt from guard rule 5 and the no-op rule; it is only sent when the guard's learning gate is open.
- `pi5/agent_watchdog.sh` + crontab entry every 30 min on the Pi5: find the last `/config` write from the agent (identify by a `X-Agent: solar-agent` request header logged by nginx/gunicorn access log, or by an `&src=agent` query parameter that `app.py` already ignores — pick whichever works without modifying `app.py` and document it). If none in 6 h and thresholds differ from `default_start`/`default_stop`, call `/config` to reset both gens to defaults and send a Telegram: "Agent silent 6 h — thresholds reset to 52.0 / 54.5". Install instructions in README; the owner installs it by hand.

## 10. Alexa

- `vps/alexa_solar.py`: add `AskAgentIntent` with slot `query` (`AMAZON.SearchQuery`). Handler POSTs `{"text": query, "lang": "es" if is_spanish(data) else "en"}` to `http://192.168.3.152:{ask_port}/ask`, 8 s timeout, speaks the reply; on failure speaks a fixed fallback in the right language. All existing intents untouched.
- Document the console changes in `agent/docs/alexa.md`: add the intent with sample utterances in English and Spanish, invocation name is "solar system" (verify in console). Deploy: copy to `/var/www/alexa_solar.py` on the VPS, `systemctl restart alexa-solar`.
- No Alexa push notifications in v1.

## 11. System prompt (`agent/prompts.py`)

Keep three clearly separated, easy-to-edit sections.

**MISSION**
> I manage the generators for an off-grid home in Rosarito so the battery bank stays in the middle of its charge curve, generator fuel is spent when it does the most good, and the owner is never surprised. I read, I forecast, I set thresholds, and I explain every move in one line. When unsure, I tell the owner instead of acting.

**SYSTEM** (plain-language description)
Three Schneider XW inverters (master, slave, XW+), ~100 kWh NMC bank held between roughly 52 and 57 V on purpose (longevity), ~13 kW PV in three groups, two generators: MEP-803A (10+ kW, 100% charge rate) and Kubota (7 kW, capped at 70%). Each generator run is limited to 120 min by the Pi5 and 3 h by the AGS. Both generators exercise 30 min at 09:00 (Kubota every 3 days, MEP every 5) — those runs are not the agent's and not a signal. The Pi5 starts both generators at the start threshold and stops each at its stop threshold; the agent only sets those thresholds.

**POLICY** (owner's rules; owner will add more)
1. Never recommend charging to full. Mid-curve is the goal.
2. Default: both gens 52.0 start / 54.5 stop.
3. Clear forecast for tomorrow: stop 54.5. Cloudy forecast: stop 56–57. Pre-charge before a bad day by raising start so the run lands in daylight.
4. Solo top-up: if today's peak voltage stayed below 57.0 and the overnight projection reaches 52 V before sunrise, run one generator now to 57.0. Choose by current post-solar voltage: ≤ 55.0 → MEP; > 55.0 → Kubota. Raise only that gen's start; leave the other at default.
5. A target is only valid if reachable within the run window at that generator's observed charge rate.
6. Return thresholds to default once the reason has passed.
7. Restate numbers only from tool results. Never compute Wh, hours, or rates yourself. When uncertain, send a Telegram instead of acting. Every action carries a one-line reason.

## 12. Deliverables

- `agent/` — `agent.py`, `tools.py`, `guard.py`, `history.py`, `loadmodel.py`, `scrape_gateway.py`, `counters.py`, `prompts.py`, `telegram.py`, `ask_server.py`, `schneider_modbus.py` (copied), `config.example.json`, `requirements.txt`, `install.sh`, `solar-agent.service` (`User=michael`, `WorkingDirectory=/home/michael/mercalde-solar/agent`, `Restart=always`, `After=network-online.target llama-server.service`), `README.md`, `docs/gateway_api.md`, `docs/alexa.md`
- `pi5/agent_watchdog.sh` and its crontab line (documented, not installed)
- `vps/alexa_solar.py` with `AskAgentIntent`
- `docs/NETWORK.md`: correct the `/alexa` entry — backend runs on the VPS at 127.0.0.1:5000 under `alexa-solar.service`; remove the KAMRUI:5001 reference
- `--dry-run` flag on `agent.py`: one tick against the live dashboard, prints the plan record and what the model would do, never writes
- Tests in `agent/tests/`: `test_guard.py` (every rule, pass and fail), `test_tools.py` (write tool hits exactly the `/config` URL with the four params and nothing else), `test_loadmodel.py`, `test_gen_runs.py` (exercise tagging)

Commit order on branch `agent`: LLM probe script → history + samples → scraper → counters → load model → tools → guard + tests → agent loop + plan record → heartbeat/watchdog → Alexa intent → deploy files + docs. Small commits, each with tests passing. Final message: exact commands to install on KAMRUI and run a dry-run tick.
