# Solar Agent

A local-LLM agent that plans generator use for the off-grid system in
Rosarito. It runs on the KAMRUI, reads the Pi5's Flask dashboard, adjusts the
dashboard's generator thresholds through its existing `/config` endpoint, and
talks to the owner over Telegram and Alexa.

**The LLM decides, a deterministic guard vetoes, and the Pi5 executes.** The
agent has exactly one lever: the four generator threshold voltages. It never
starts or stops a generator itself.

Two of those four numbers have absolute limits: a start is never written
below 52.0 V and a stop never above 57.0 V. They live in `guard.py` as
constants, are checked before every other rule on every write, and are not
reachable from `config.json`, a POLICY rule or the owner's own baseline.
`config.py` clamps its configured bounds to them at load, and
`pi5/agent_watchdog.sh` carries its own copy so a reset cannot land outside
them either.

## How a tick works

Every 15 minutes, day and night:

1. Python gathers the facts — live status, thresholds, the last 24 hours,
   the load forecast to sunrise, the weather, today's peak voltage. On the
   first gather of a run the live thresholds are settled against what this
   agent last wrote: the same values are its own and the stored baseline
   stands; different ones were moved while it was away and are the owner's.
   Stored intent is never re-asserted over the dashboard either way.
2. `policy.py` computes every POLICY rule whose condition is arithmetic and
   says, with the numbers, whether it fires. Whether a target is reachable is
   `loadmodel.py`'s answer, from each generator's learned gross delivery less
   the load the run window expects — never in volts per hour or in the shunt
   alone, both of which measure the generator minus the house. The curve is the charge-side one, learned from
   the minute samples inside real runs, because the Pi5 stops on the terminal
   voltage during a charge and not on the settled resting voltage.
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
6. Nothing is re-sent. Every write the agent makes goes through the guard and
   the audit log; `apply_thresholds` refuses to send anything the guard has
   not approved. The Pi5 watchdog reads liveness from `GET /plan`, so there is
   nothing for a heartbeat to prove.

## Layout

| File | What it does |
|---|---|
| `agent.py` | the tick loop, plan record, digests, Telegram inbound, anomalies |
| `policy.py` | the numeric POLICY rules, computed rather than left to the model |
| `topup.py` | the per-generator top-up state machine POLICY 4 runs on |
| `guard.py` | the hard rules; every write passes through it |
| `tools.py` | the eight read tools and the single write, as OpenAI schemas |
| `eval_cases.py` | Q&A cases and their graders, for `model_eval.py --exam` |
| `system.yaml` | the system manifest: hardware, network, policy constants |
| `system.py` | reads the manifest; generates SYSTEM; checks the guard's limits |
| `prompts.py` | MISSION and POLICY by hand; SYSTEM generated from the manifest |
| `history.py` | SQLite store, the 60 s sampler, rollups, generator-run derivation |
| `loadmodel.py` | load profile, solar yield, charge rates, projections, learning gate |
| `counters.py` | Gateway energy registers over Modbus 503 |
| `scrape_gateway.py` | InsightLocal history backfill |
| `sun.py` | sunrise and sunset, computed from lat/lon; no network |
| `weather.py` | Open-Meteo cloud and irradiance, forecast and archive |
| `telegram.py` | send and long-poll |
| `ask_server.py` | `POST /ask` and `GET /plan` for Alexa |
| `llm.py`, `llm_probe.py` | llama-server client, and a probe to check it |
| `model_eval.py` | replay recorded ticks against a candidate model and score it |
| `config.py` | config loading |
| `schneider_modbus.py` | copied from `pi5/` |

Documentation: `docs/gateway_api.md`, `docs/energy_registers.md`,
`docs/alexa.md`, `docs/kamrui_stability.md`.

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

## The manifest

`agent/system.yaml` is the one description of the site: inverters, generators,
battery, arrays, network, and the policy constants. Three things read it.

The SYSTEM section of the model's prompt is generated from it, so the prompt
cannot drift from the hardware. `config.json` keeps only what is secret or
per-install — the Telegram token, the gateway password, the model endpoint —
and every key the manifest also names is taken from the manifest, with a
warning if `config.json` still sets it. And `guard.py` keeps its hard floor
and ceiling as code constants, because a number that can be edited in a data
file is not a hard limit, but checks them against `battery.floor_v` and
`battery.ceiling_v` at import: a manifest that disagreed would be describing a
system that does not exist.

Where the repository disagreed with itself the manifest records the
disagreement rather than resolving it by guesswork. The array table in the
top-level README gives slave IDs that contradict the mapping in
`docs/energy_registers.md`, which was established by reading each slave with
both MPPT register tables and seeing which one gave a possible answer; the
manifest carries the verified mapping and marks the physical array behind each
controller as unverified.

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

## The top-up state machine

POLICY 4 used to be re-derived from the pack's voltage on every tick, with no
memory of what it had already done. On the night of 2026-08-30 that produced
three separate Kubota top-ups between 7:20 and 8:55 pm — 54.1/56.1, then
54.0/56.0, then 54.6/56.6 — the last of them written while the Kubota was
already running.

So the decision is a state now, one per generator, in `topup.py` and
persisted to `data/topup_state.json`:

```
idle → requested → running → done
                 ↘         ↘ stopped_by_owner
                   failed_to_start
```

`idle` is the only state rule 4 evaluates in, and a generator leaves it once
per night. A night is named by the sunset that opened it, so what a run
settles at eleven at night still holds at ten the next morning.

| state | how it is entered | what follows |
|---|---|---|
| `requested` | the agent raised that generator's start | the 2 V spread is fixed here and never re-applied |
| `running` | `*Action == 9` seen | its start goes straight back to the owner's baseline, and nothing more is proposed for it |
| `done` | the run ended on its stop voltage or its runtime cap | held until the next sunset |
| `stopped_by_owner` | the run ended short of both, or the AGS mode went Off, or the owner edited `/config` | held until the next sunset, one Telegram |
| `failed_to_start` | asked, pack under the start, nothing running five minutes later | start returned, one Telegram naming the AGS state, and the top-up is re-evaluated with the other generator |

The rule also holds while `autoGenEnabled` is false. A start threshold written
into a disabled auto-gen starts nothing, and five minutes later the machine
would report a generator that "didn't start" and a controller that may need a
reset — neither of which would be true. The owner turned auto-gen off at
7:26 pm on 2026-08-30 and the agent went on setting start thresholds for the
next hour and a half.

## The guard

Nine rules, none of them model-editable. Every decision, pass or refuse, is
written to `data/audit.log` and the `actions` table.

1. **Bounds** — each start 52.0–56.0, each stop 54.5–57.0, stop at least
   2.0 V above start.
2. **No-op** — refuse if all four values already match `/config`.
3. **Running generator** — its stop may be raised, never lowered. Its start
   is irrelevant mid-run. And while a generator's top-up is in flight —
   `requested` or `running` — neither its start nor its stop may be raised
   past what that top-up asked for. A top-up is decided once, and the lift
   that clears the start by the 2.0 V separation is computed with it. Once
   the run is over the ceiling lifts and POLICY 3 can raise a stop before a
   storm like any other night.
4. **Reachability** — a generator that will fire now must be able to reach
   its stop at its own observed charge rate, inside
   `min(Pi5 maxRuntime, ags_max_run_hours)`. No observed rate means refuse.
5. **Rate** — at most one write per hour, unless it only moves values back
   toward the owner's baseline.
6. **Learning gate** — as above.
7. **Stale data** — refuse if the dashboard poll is over 5 minutes old or the
   Battery Monitor is offline.
8. **Owner override** — if `/config` no longer matches what the agent last
   wrote, adopt the owner's values, stand down for 6 hours, and end that
   generator's top-up night. The comparison is against the agent's own last
   write and never against the baseline: an owner who puts a raised start
   back to 52.0 has restored the baseline and overruled the agent at the same
   time, and measured against the baseline that reads as nothing having
   happened. The same test settles what is in force at startup — thresholds
   that match the agent's last write are the agent's own and the stored
   baseline stands; thresholds that differ were moved while the agent was
   away, and are the owner's.
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
agent/venv/bin/python agent/model_eval.py -n 20         # score the live model
```

## Comparing models

`model_eval.py` replays recorded ticks against a model and marks it on the
four things that have actually gone wrong in production:

| check | what it catches |
|---|---|
| tool calls | a call naming no tool, or arguments that will not bind |
| numbers | a figure in the answer that was in neither the prompt nor any tool result — POLICY 8, and the failure that put an invented voltage in front of Alexa |
| rules | a POLICY rule that fired and was answered with "no change" instead of being set or overruled |
| narration | telling the owner a write happened. Only the write path may say that; at 12:17 am a model said it after the guard had refused |

The prompt and the answer are stored with every plan, so a candidate is asked
exactly what the live model was asked, on nights that have already happened.
Ticks whose prompt has been pruned are skipped rather than reconstructed — a
replay of a rebuilt prompt scores a model on a question nobody put to it.
`eval_retention_days` (14 by default) decides how long the prompts are kept;
the plan records themselves are never pruned.

Nothing is written and nothing is sent. The tools are read-only and pinned to
the tick being replayed: `get_status` comes from the sample recorded that
minute, the history and forecast tools compute from the database as of then,
and `set_gen_thresholds` and `send_telegram` are captured for scoring. The
one exception is `get_weather`, which has no stored history and answers for
now rather than for then, which is why replays are worth keeping recent.

### A second llama-server for A/B

Run the candidate on another port. It does not touch the agent, which keeps
talking to whatever `llm_url` in `config.json` points at:

```bash
# on the KAMRUI, alongside the live server on 8080
llama-server -m ~/models/qwen3-14b-q4_k_m.gguf \
  --host 127.0.0.1 --port 8082 --jinja -c 8192 -ngl 99 &

agent/venv/bin/python agent/model_eval.py -n 20 \
  --candidate http://127.0.0.1:8082/v1/chat/completions=qwen3-14b
```

The configured endpoint is scored as the incumbent alongside it; `--candidate`
is repeatable, and `--only-candidates` leaves the live model out. Give a model
name after `=` when the second server serves a different one.

```
model                        ticks  clean  calls  invalid  unsourced  fired  missed  narrated
---------------------------------------------------------------------------------------------
qwen3 (live)                    20    75%     47        0          3     11       2         0
qwen3-14b @ 127.0.0.1:8082      20    95%     51        1          0     11       0         0
```

`clean` is the share of ticks with no fault of any kind. Every fault is
printed underneath the table with its timestamp, so a number in it can be
chased back to the tick that produced it. `--json` emits the rows instead.

### The exam

`--exam` is the other half: it puts the questions in `eval_cases.py` to the
running agent over `POST /ask` and marks each answer against the database.

```bash
agent/venv/bin/python agent/model_eval.py --exam
agent/venv/bin/python agent/model_eval.py --exam --case voltage_at_247
```

Every case is a question the agent has answered wrongly at least once, so the
file is also the regression record for the ask path: a raised start threshold
mistaken for a flat battery, a window minimum offered as the reading at a
named minute, the plan record paraphrased instead of quoted. The ground truth
is computed from the database each run rather than written down, so a case
does not quietly stop testing anything when the pack moves.

Both servers want their own weights in RAM. The KAMRUI has enough for an 8B
and a 14B at Q4 at the same time, but not much else; stop the candidate when
the comparison is done.

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
