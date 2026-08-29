# Solar Agent

A local-LLM agent that plans generator use for the off-grid system in
Rosarito. It runs on the KAMRUI, reads the Pi5's Flask dashboard, adjusts the
dashboard's generator thresholds through its existing `/config` endpoint, and
talks to the owner over Telegram and Alexa.

**The LLM decides, a deterministic guard vetoes, and the Pi5 executes.** The
agent has exactly one lever: the four generator threshold voltages. It never
starts or stops a generator itself.

## How a tick works

Every 15 minutes, day and night:

1. Python gathers the facts — live status, thresholds, the last 24 hours,
   the load forecast to sunrise, the weather, today's peak voltage.
2. `policy.py` computes every POLICY rule whose condition is arithmetic and
   says, with the numbers, whether it fires.
3. The model sees those facts and that evaluation, may call up to 4 tools, and
   finishes with a one-line recommendation. A rule that fires must be either
   set or overruled in writing; anything else is logged as a policy miss.
4. If it proposes a change, `guard.py` checks it against the hard rules.
   A refusal goes back to the model as the tool result, with the reason.
   Every write that is executed sends the owner a Telegram with the values
   the dashboard read back and the reason given.
5. The plan record is written to the `plans` table. Every line but the
   recommendation is computed in Python, including whether anything was
   applied — the model cannot claim a change it did not make.
6. The intended thresholds are re-sent as a heartbeat, so the Pi5 watchdog
   can tell the agent is alive.

## Layout

| File | What it does |
|---|---|
| `agent.py` | the tick loop, plan record, digests, Telegram inbound, anomalies |
| `policy.py` | the numeric POLICY rules, computed rather than left to the model |
| `guard.py` | the hard rules; every write passes through it |
| `tools.py` | the seven read tools and the single write, as OpenAI schemas |
| `prompts.py` | MISSION / SYSTEM / POLICY, meant to be edited by hand |
| `history.py` | SQLite store, the 60 s sampler, rollups, generator-run derivation |
| `loadmodel.py` | load profile, solar yield, charge rates, projections, learning gate |
| `counters.py` | Gateway energy registers over Modbus 503 |
| `scrape_gateway.py` | InsightLocal history backfill |
| `weather.py` | Open-Meteo forecast and archive |
| `telegram.py` | send and long-poll |
| `ask_server.py` | `POST /ask` and `GET /plan` for Alexa |
| `llm.py`, `llm_probe.py` | llama-server client, and a probe to check it |
| `config.py` | config loading |
| `schneider_modbus.py` | copied from `pi5/` |

Documentation: `docs/gateway_api.md`, `docs/energy_registers.md`,
`docs/alexa.md`.

## Install

On the KAMRUI:

```bash
git clone https://github.com/mmercalde/mercalde-solar.git
cd mercalde-solar && git checkout agent
./agent/install.sh
```

Then fill in `agent/config.json` (gitignored, mode 600):

- `telegram.token` / `telegram.chat_id` — the same bot the dashboard uses;
  both appear in the dashboard's own `config.json` and in `GET /config`
- `gateway.password` — the InsightLocal Admin password, needed only for the
  history backfill

Backfill history, then try a tick that writes nothing:

```bash
agent/venv/bin/python agent/scrape_gateway.py --discover
agent/venv/bin/python agent/scrape_gateway.py --backfill
agent/venv/bin/python agent/agent.py --dry-run
```

Start it:

```bash
sudo systemctl enable --now solar-agent
journalctl -u solar-agent -f
```

## The learning phase

The guard refuses **every** write until two conditions hold (rule 6):

- `hourly` holds the current calendar month from at least one prior year —
  this is what `scrape_gateway.py --backfill` is for
- `samples` covers at least `learning_live_days` (7) consecutive days

The gateway's `years` endpoint lists 2025 and 2026, so a full backfill reaches
far enough back to satisfy the first condition. `--backfill` takes its hourly
energy from the gateway's own per-hour accounting and uses the minute export
for per-hour peak and minimum voltage and for the voltage-to-SOC curve; see
`docs/gateway_api.md`.

### The voltage-to-SOC curve

The 52 V projection needs to know what state of charge the start threshold
corresponds to. Live sampling passes through that voltage rarely, so on live
data alone the plan record says "no observed SOC at 52.0 V yet" for weeks.
The backfill learns it instead, from years of minute-resolution SOC:

```bash
agent/venv/bin/python agent/scrape_gateway.py --soc-only
```

`--backfill` fills the curve too; `--soc-only` refills just the curve without
re-reading energy already stored, which halves the requests to a gateway that
caps concurrent sessions. Check what it learned:

```bash
agent/venv/bin/python -c "import sys; sys.path.insert(0,'agent'); \
  import config, history, loadmodel, json; c = config.load(); \
  print(json.dumps(loadmodel.LoadModel(history.connect(), c).soc_curve_status(), indent=1))"
```

Everything else runs meanwhile: the agent samples, forecasts, plans, and says
what it would have done. The plan record ends `applied: no (learning phase)`.

Check where it stands:

```bash
agent/venv/bin/python -c "import sys; sys.path.insert(0,'agent'); \
  import config, history, loadmodel, json; c = config.load(); \
  print(json.dumps(loadmodel.LoadModel(history.connect(), c).learning_status(), indent=1))"
```

## The guard

Nine rules, none of them model-editable. Every decision, pass or refuse, is
written to `data/audit.log` and the `actions` table.

1. **Bounds** — each start 52.0–56.0, each stop 54.5–57.0, stop at least
   2.0 V above start.
2. **No-op** — refuse if all four values already match `/config`.
3. **Running generator** — its stop may be raised, never lowered. Its start
   is irrelevant mid-run.
4. **Reachability** — a generator that will fire now must be able to reach
   its stop at its own observed charge rate, inside
   `min(Pi5 maxRuntime, ags_max_run_hours)`. No observed rate means refuse.
5. **Rate** — at most one write per hour. The heartbeat is exempt.
6. **Learning gate** — as above.
7. **Stale data** — refuse if the dashboard poll is over 5 minutes old or the
   Battery Monitor is offline.
8. **Owner override** — if `/config` no longer matches what the agent last
   wrote, adopt the owner's values and stand down for 6 hours.
9. **Audit** — every check is recorded.

### One deliberate deviation

SPEC section 7 rule 7 keys staleness off `/data`'s `lastUpdate`. That key is
**uptime**, not a timestamp — `pi5/app.py:868` says so outright:

> `lastUpdate is really uptime; kept as-is for the ESP32 display and Alexa webhook. clockTime is the actual wall clock of this poll.`

Keying staleness off uptime would never fire, so the rule uses `clockTime`,
which carries the meaning the rule intends. `lastUpdate` is still recorded in
`samples`.

## Talking to it

**Telegram** — message the bot from the configured chat; anything else is
ignored. Send `plan` to get the latest plan record verbatim. Otherwise the
question goes to the model with the same tools, and the reply follows the
language you asked in.

**Alexa** — "Alexa, ask solar system what the plan is tonight". See
`docs/alexa.md`.

**HTTP**, from the KAMRUI only:

```bash
curl -s -X POST http://192.168.3.152:8090/ask \
     -H 'Content-Type: application/json' \
     -d '{"text": "what is the battery voltage", "lang": "en"}'

curl -s http://192.168.3.152:8090/plan
```

### Answers are grounded

Qwen3-8B will sometimes answer a question about live state without calling a
tool, and then invent the number — asked in Spanish during testing it replied
"55.2 V" while the pack sat at 54.09 V. POLICY 7 forbids exactly that, so an
ungrounded reply is retried once, and if the model still will not look,
Python answers from `/data` instead. A made-up voltage never reaches the
owner or Alexa.

## Running it by hand

```bash
agent/venv/bin/python agent/agent.py --dry-run          # one tick, writes nothing
agent/venv/bin/python agent/agent.py --once             # one real tick
agent/venv/bin/python agent/agent.py --ask "..."        # one question
agent/venv/bin/python agent/agent.py --digest evening   # send a digest now
agent/venv/bin/python agent/llm_probe.py                # check llama-server
agent/venv/bin/python agent/counters.py                 # dump energy counters
```

## Tests

```bash
cd agent && venv/bin/python -E -m pytest -q
```

`-E` makes the interpreter ignore `PYTHONPATH`. It matters on a machine with
ROS installed, whose pytest plugins are otherwise autoloaded into the venv
and fail on a missing `yaml`.

## The Pi5 watchdog

`pi5/agent_watchdog.sh` resets the thresholds to `default_start` /
`default_stop` and sends one Telegram if the agent goes silent for 6 hours.
It takes those values from the agent itself, over `/plan`, so they stay in
step with `config.json`. It measures silence from the last time the agent
actually answered, and never resets while the learning gate is closed —
during the learning phase the guard permits no writes, so thresholds off the
defaults are the owner's, not the agent's. The owner installs it by hand;
the crontab line is in the script header. It detects liveness through the
agent's own `GET /plan`, because gunicorn on the Pi5 runs without
`--access-logfile` and so writes no HTTP access log to search.

## Memory on the KAMRUI

Two 8B models resident is about 20 GB of 22. `llm_probe.py` reports swap use
on the host it runs on. If the agent's tests show swap pressure, the fix is
stopping `llama-server-abliterated.service`, not shrinking the model.
