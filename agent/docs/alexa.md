# Alexa — questions to the agent

Lets the owner put a free-form question to the solar agent by voice, in
English or Spanish.

```
"Alexa, ask solar agent why the Kubota started"
"Alexa, ask solar agent how much solar we made yesterday"
"Alexa, pregúntale a agente solar por qué arrancó el Kubota"
```

The invocation names are **"solar agent"** (English) and **"agente solar"** (Spanish:
es-US and es-MX). The Spanish name was "casa solar" until 2026-11-06, 2:22 pm; it changed
because "Alexa, abre casa solar" didn't open the skill on the Echo. The skill is listed
as "solar system".

Alexa+ is turned off on the Echo, because its hand-off line says "Alexa exit" aloud.

## What happens

1. Alexa sends an `IntentRequest` for one of the agent intents below to
   `https://mercalde-solar.org/alexa`.
2. nginx on the VPS proxies it to `alexa-solar.service`
   (`/var/www/alexa_solar.py`) on 127.0.0.1:5000.
3. The handler reads the `query` slot, puts the intent's question word back in
   front of it, and POSTs
   `{"text": <question>, "lang": "es"|"en", "deliver": "telegram", "wait_s": 5}` to
   `http://192.168.3.152:8090/ask` on the KAMRUI, over WireGuard through the
   Pi5, with a 7 second timeout. Alexa gives up after about 8 s, and Gemma takes
   15 to 30 s.
4. The agent then does one of three things:

| The question | `/ask` returns | Alexa says | Telegram gets |
|---|---|---|---|
| A plan question | 200 at once | One sentence built in Python, e.g. "The pack is projected to reach the 52.0 volt start at 5:31 am; the full plan is on your phone." | `🎙 <question>` and the full plan record |
| Answered within 5 s | 200 | The answer | Nothing |
| Anything slower | 202 | "I'm checking — I'll send the answer to your phone." / "Lo estoy revisando, te envío la respuesta al teléfono." | `🎙 <question>` and the answer, when it's ready |

A late answer goes through the same `answer()` as everything else, so it queues
behind ticks and other questions. If it fails, Telegram gets the fallback below.
If the VPS's 7 s timeout expires, Alexa speaks the fallback and the answer can
still reach Telegram afterwards.

Without `deliver`, `/ask` waits for the answer however long it takes and returns
the full plan record for a plan question. Telegram and the dashboard use `answer()`
as before, but the wider plan wording applies to them too.

Language follows `request.locale` through the existing `is_spanish(data)`
helper — no new language handling was added.

On any failure — the agent down, any other status, a timeout, an empty reply — Alexa
speaks a fixed fallback in the right language rather than an error:

| Language | Fallback |
|---|---|
| English | "The solar agent is not answering right now." |
| Spanish | "El agente solar no responde en este momento." |

Alexa only answers when asked; a late answer goes to Telegram, not to the Echo.

## The agent intents

`AMAZON.SearchQuery` needs at least one carrier word before the slot, and **Alexa
drops the carrier from the slot value**: with `why {query}`, "why did the Kubota
start" arrives as "did the Kubota start". So there is one intent per leading
question word, and `AGENT_PREFIX` in `vps/alexa_solar.py` puts the word back.

The existing intents (battery, status, solar, generator, start/stop/auto/off, events,
auto-gen) keep their exact phrases and should still win their own sentences, such as
"what's the battery level", "how's the battery", "is the generator on" and "que paso".
The VPS checks them first, so a request naming one of them never reaches the agent.
Which intent Alexa chooses for a sentence is decided in the console, and can only be
tested once the intents are built.

Every intent below has a slot named `query` of type `AMAZON.SearchQuery`.

### English (invocation "solar agent")

| Intent | Sample utterances | VPS prefix |
|---|---|---|
| `AskWhyIntent` | `why {query}`, `how come {query}` | why |
| `AskHowIntent` | `how {query}` | how |
| `AskWhatIntent` | `what {query}` | what |
| `AskWhatIsIntent` | `what's {query}`, `what is {query}` | what is |
| `AskWhenIntent` | `when {query}` | when |
| `AskWhichIntent` | `which {query}` | which |
| `AskWillIntent` | `will {query}` | will |
| `AskIsIntent` | `is {query}` | is |
| `AskAreIntent` | `are {query}` | are |
| `AskDidIntent` | `did {query}` | did |
| `AskDoIntent` | `do {query}` | do |
| `AskDoesIntent` | `does {query}` | does |
| `AskWasIntent` | `was {query}`, `were {query}` | was |
| `AskShouldIntent` | `should {query}` | should |
| `AskAgentIntent` | `ask the agent {query}`, `ask agent {query}`, `question for the agent {query}`, `what does the agent say about {query}`, `agent {query}`, `to tell me {query}`, `tell me {query}`, `about {query}`, `if {query}`, `whether {query}`, `to explain {query}`, `to check {query}` | none |

### Spanish (invocation "agente solar": "pregúntale a agente solar …")

| Intent | Sample utterances | VPS prefix |
|---|---|---|
| `AskPorQueIntent` | `por qué {query}`, `por que {query}` | por qué |
| `AskComoIntent` | `cómo {query}`, `como {query}` | cómo |
| `AskCuantoIntent` | `cuánto {query}`, `cuanto {query}` | cuánto |
| `AskCuantaIntent` | `cuánta {query}`, `cuanta {query}` | cuánta |
| `AskCuantosIntent` | `cuántos {query}`, `cuantos {query}`, `cuántas {query}`, `cuantas {query}` | cuántos |
| `AskQueIntent` | `qué {query}`, `que {query}` | qué |
| `AskCualIntent` | `cuál {query}`, `cual {query}` | cuál |
| `AskCuandoIntent` | `cuándo {query}`, `cuando {query}` | cuándo |
| `AskHayIntent` | `hay {query}` | hay |
| `AskVaIntent` | `va a {query}`, `van a {query}` | va a |
| `AskEstaIntent` | `está {query}`, `esta {query}`, `están {query}` | está |
| `AskAgentIntent` | `pregunta al agente {query}`, `preguntale al agente {query}`, `pregúntale al agente {query}`, `que dice el agente sobre {query}`, `agente {query}`, `si {query}`, `sobre {query}`, `acerca de {query}`, `que me diga {query}`, `que me explique {query}` | none |

`cuántas` goes through the `cuántos` intent, and `were` through `was`, because the
agent reads either fine.

### Status

The VPS side went live on 2026-11-06 at 11:48 am (`d981329`). Only `AskAgentIntent`
with its first five English and five Spanish utterances had been built in the console
at that point. The other intents and utterances are added in the console afterwards,
in both locales, then Build. Unknown `Ask*Intent` names get the existing "not sure" reply.

Left out on purpose in Spanish: `dime {query}` and bare `pregunta {query}`, so they
can't take over existing intents.

## Starting and stopping a generator by voice

`StartGeneratorIntent`, `StopGeneratorIntent` and `SetGeneratorOffIntent` act only
when `intent.confirmationStatus` is `CONFIRMED`:

| Status | What happens |
|---|---|
| `CONFIRMED` | Start: `/setgen?id=…&state=1`. Stop: the graceful `/stopgen?id=…`. Off: `/setgen?id=…&state=0`. |
| `DENIED` | Nothing. "OK, I won't." / "Está bien, no lo haré." |
| `NONE`, or anything else | Nothing. "Please confirm: …" with the session left open. |

**Start, Stop and Off require confirmation in the console, in en-US, es-US and es-MX.**
es-MX was cloned from es-US on 2026-11-06, and its confirmation was verified in the es-MX
simulator. For start and stop (delegationStrategy ALWAYS), Alexa asks "Are you sure you
want to start/stop the {generator}?" / "¿Estás seguro que quieres prender/apagar el
{generator}?" and sends the final status. The handler returns no dialog directive.

`SetGeneratorAutoIntent`, `EnableAutoGenIntent` and `DisableAutoGenIntent` don't check
for confirmation.

### A manual start can stop straight away

A generator started by voice or from the dashboard is stopped by the Pi5's auto loop as
soon as the battery is at or above that generator's stop voltage. **This is intended:**
the owner chose to keep it on 2026-11-06. On that day a voice start of the Kubota at 3:18
pm began stopping 37 s later, at 57.25 V against a 57.0 V stop.

The check runs in `check_auto_generator()`, so it needs auto control enabled. It only
applies to a generator commanded on: an exercise runs in Mode 2, and the AGS ends it.

### Which generator

Since 2026-11-06, about 1:50 pm, the `generator` slot is required, with an elicitation
prompt, on `StartGeneratorIntent`, `StopGeneratorIntent`, `SetGeneratorAutoIntent` and
`SetGeneratorOffIntent`, in en-US, es-US and es-MX. Before this, if Alexa missed the
name, the confirmation read "prender unknown".

Samples added at the same time:

- Spanish: "que prenda el…", "que arranque el…", "prende el generador", and similar.
- English, with no name: "start the generator", "turn on the generator".

**Any change to the es-US model must also be copied to es-MX (Clone).**

The `GeneratorType` slot type in the console:

| Locale | Canonical values |
|---|---|
| en-US | `kubota`, `mep`, `all` |
| es-US, es-MX | `kubota`, `mep`, `todos` |

Spanish synonyms, es-US and es-MX:

| Canonical | Synonyms |
|---|---|
| `kubota` | el kubota, generador kubota, kubota generador, cobalta, la cobalta, el cobalta, cobalto, el cobalto, generador cobalta, cubota, el cubota, cu bota, kubo |
| `mep` | el militar, mep 803, eme e pe, emep, el eme, eme |
| `todos` | los dos, ambos, todos los generadores, ambos generadores |

**Alexa hears "Kubota" as "cobalta" in Spanish** (seen in the voice history on
2026-11-06), which is why those synonyms are there. They live only in the console:
`get_gen_id` doesn't know "cobalta", so it works because entity resolution returns
`kubota`. A "cobalta" that didn't resolve would get "¿Cuál generador…?" and move nothing.

Spanish samples without the article, for the same reason: "prende/arranca/enciende
generador {generator}" and "apaga/para generador {generator}". Verified in the es-MX
simulator: it resolved to `kubota`, and the confirmation was denied.

The VPS has its own backstop in these four handlers. It takes the name from the slot's
entity resolution when the status code is `ER_SUCCESS_MATCH`, otherwise the raw value.
`get_gen_id` matches whole words, so "small" isn't "all". If neither maps to a generator,
it says "Which generator, the Kubota or the MEP?" /
"¿Cuál generador, el Kubota o el MEP?", keeps the session open and moves nothing.
"unknown" never reaches speech, and a leading "the " or "el " is dropped from the name
before it's spoken ("start the kubota?"). `get_gen_id` still gets the name as it came.
`DENIED` still gets "OK, I won't", since a refusal needs no name. The `CONFIRMED`-only rule is unchanged, and Auto still doesn't check for
confirmation.

`SessionEndedRequest` gets an empty response, and its reason and error go to the log
as `DEBUG SessionEnded`. That is where to look when Alexa reports an invalid response.

## Screens

A "sky clock and energy flow" scene, on devices that report `Alexa.Presentation.APL`. All
of it is built in `vps/alexa_solar.py`. Labels are Spanish when the locale is `es-*`, and
there are no emoji in any document or datasource string: Pi5 events and agent replies can
contain them, so `no_emoji()` strips them first.

| Screen | Shown for | What's on it |
|---|---|---|
| Scene | Launch, help, battery, status, solar, generators | The sky, battery, flows and generators below |
| Events | "What happened" | The last 5 events, newest first, or "No recent events." |
| Agent answer | `AskAgentIntent` and every `Ask*` prefix intent | The question, the answer, then a status strip: battery %, volts, solar W, generator states |
| Scene plus a line | An agent 202 | "Answer sent to your phone" / "Respuesta enviada a tu teléfono" |

Events and answers use the same day and night colors with no scene and no card fills.

### Data fields

From the Pi5's `/data`, the same fields as its dashboard unless noted:

| Item | Fields |
|---|---|
| Battery | `batterySOC`, `batteryVoltage` |
| Solar | `mppt80PVPower`, `southArrayPVPower`, `westArrayPVPower` |
| House load | `acPower1` + `acPower2` |
| Generator state | `mepState`, `kubotaState`, `mepFault`, `kubotaFault`, `mep803aAction`, `kubotaAction`, `mep803aMode`, `kubotaMode` |
| Charge power while running | MEP: `chargePower1` + `chargePower2` (the XW Pros, IDs 10 and 12). Kubota: `chargePower3` (the XW+, ID 11) |
| Auto control | `autoGenEnabled` |
| Thresholds | `startVoltage` and `stopVoltage` from a `GET /config` with no parameters |

**Charge power comes from the charger code only.** The history database doesn't store these
fields, so the mapping hasn't been checked against a real run.

**House load hides during a run.** While any generator counts as running, the
battery-to-house flow and its label are hidden, because the inverters' AC output is then the
generator's. That's the same rule as the agent's `load_w`. A Kubota-only run might read
correctly, since the XW+ doesn't invert to the house, but the screen takes the safe side.

The state word follows the same order as the dashboard's `chipFor()`:

| State | When | Dot and word |
|---|---|---|
| Fault | `ags_fault`, or the fault flag | `#F87171` "Fault" / "Falla" |
| Running | Action 9, starting or stopping, or Mode On with nothing turning yet | `#34D399` "Running" / "Prendido" |
| Auto | Mode 2 | `#FBBF24` "Auto" / "Automático" |
| Off | Mode 0, or anything else | `#5B6575` "Off" / "Apagado" |

The green flow appears only at Action 9, not for the rest of "Running".

`GET /config` writes nothing. Its response also holds the Telegram token, so the VPS takes
only the two voltages and never logs it. If it can't be read, the line is left off.

### The sky

Sunrise and sunset are computed on the VPS with NOAA's general formula for lat 32.36, lon
-117.06, timezone America/Tijuana, with no outside call. It agreed with Open-Meteo to within
a minute on 2026-11-06 (6:09 am, 4:54 pm against 4:53 pm). The VPS runs Python 3.8, so the
timezone comes from `pytz` there and `zoneinfo` elsewhere. Times are 12-hour: "3:41 pm",
"3:41 p. m."

Day runs from the exact sunrise to the exact sunset, both included. The sun's position is
the fraction of daylight elapsed, mapped onto the semicircle: left horizon, top, right
horizon. Python computes x and y per layout and passes them in the datasource. At night
there's no sun or arc, only the moon and 7 stars at fixed places.

### Colors

| Item | Day | Night |
|---|---|---|
| Sky | `#13306B` | `#070B1A` |
| Ground | `#081734` | `#04060D` |
| Horizon | `#2B4A8A` | `#1C2440` |
| Secondary text | `#AFC3E8` | `#8B95A5` |

Stars `#CBD5E1`, moon `#E5E7EB` with a sky-colored disc cut out. Sun `#FBBF24`, with a glow
at 25% opacity and a dashed path at 50%. Battery by SOC: 70 and up `#34D399`, 40 to 69
`#FBBF24`, under 40 `#F87171`. Big numbers are weight 300.

### Flows

AVG paths with round caps and moving dashes (`strokeDashOffset` bound to `elapsedTime`,
faster with more power). A device that doesn't animate shows static dashes.

| Flow | Color | Label | Shown when |
|---|---|---|---|
| Sun to battery | Amber `#FBBF24` | Total solar kW | Daytime and solar over 50 W |
| Battery to house | Blue `#60A5FA` | House kW | No generator running |
| Generator to battery | Green `#34D399` | That generator's charge kW | That generator at Action 9 |

The sun's flow bends around the big numbers, entering the battery on the sun's side.

### Spot and Show

APL `when` on `viewport.shape == 'round'` picks the layout. One scene graphic per layout
stretches over the screen, and text sits on top in `vw` and `vh`, so sizes scale with it.

- **Echo Spot (round):** horizon at 58%. Time at the top, then the sun or moon on the sky,
  and the battery icon just above the horizon. Below it are the big % and volts, generator
  dots with names on the left, and the house with its kW on the right. "Sunset 4:54 pm" /
  "Atardecer 4:54 p. m." at night becomes "Sunrise…" / "Amanecer…". No thresholds or arrays.
- **Echo Show (every landscape Hub size):** a full-width panorama with the ground on the
  bottom 38% and a sun arc across 60% of the width. The big %, volts and battery icon stand
  slightly left of center on the horizon. Ground left has each generator's dot, name,
  right-aligned status and thresholds; ground right has "Solar by array". Top-left is auto
  control, top-right the time, bottom center the sunset or sunrise.

### Session and limits

- **Session:** read answers leave `shouldEndSession` unset, which keeps the session open
  without the mic, and the documents set `idleTimeout` to 60000 (60 s). Launch and help
  keep the mic open, as they invite a reply. A read that has nothing to show ends the
  session as before.
- **No screen, and confirmations:** devices without a screen behave as they did, with no
  extra calls to the Pi5. Confirmation dialogs get no screen and keep the mic open.
- **Time limit:** Alexa allows about 8 s and the agent can use 5 to 7. The figures after an
  agent answer use a short timeout and are skipped after 6.5 s, so an answer can appear
  without its strip. If the Pi5 is out on a 202, the answer screen carries the line instead.
- **Size:** Alexa's limit is 24 KB per response. Agent text is clipped to 2,000 characters
  on screen, and Alexa still speaks all of it.

**The datasource key is `solar`, never `payload`.** APL reserves `payload` as a
`mainTemplate` parameter: it binds the whole datasources object, so `${payload.batterySOC}`
looks for `datasources.batterySOC` and every value renders blank, with no error. An AVG
graphic can't read the datasource either, so everything it needs is passed as a parameter on
its `VectorGraphic`. `agent/tests/test_vps_screens.py` checks all of this, but no test
renders APL, so a layout change needs the simulator.

## Deploying the backend

```bash
scp vps/alexa_solar.py root@45.32.131.224:/var/www/alexa_solar.py
ssh root@45.32.131.224 'systemctl restart alexa-solar && systemctl status alexa-solar --no-pager'
```

Check it took:

```bash
ssh root@45.32.131.224 'journalctl -u alexa-solar -n 30 --no-pager'
```

## Checking the path without Alexa

The agent's endpoint, from the KAMRUI:

```bash
curl -s -X POST http://192.168.3.152:8090/ask \
     -H 'Content-Type: application/json' \
     -d '{"text": "what is the battery voltage", "lang": "en"}'
```

Add `"deliver": "telegram", "wait_s": 5` to test the way Alexa calls it. This
sends a real Telegram message whenever the answer is late or it's a plan question.

The VPS handler, without going through Amazon:

```bash
ssh root@45.32.131.224 "curl -s -X POST http://127.0.0.1:5000/alexa \
  -H 'Content-Type: application/json' \
  -d '{\"request\":{\"type\":\"IntentRequest\",\"locale\":\"en-US\",
       \"intent\":{\"name\":\"AskAgentIntent\",
       \"slots\":{\"query\":{\"name\":\"query\",\"value\":\"what is the plan\"}}}}}'"
```

## Verified

`alexa_agent_2026-11-06.md` has the first deploy. For the question-word intents, on
2026-11-06 at 11:48 am, using fake requests on the VPS:

- **The 16 read-only responses** (launch, battery, status, solar, generator, events,
  help, stop, in both languages) matched the ones from before the deploy, with live
  numbers masked.
- **All 26 intents in both languages, 52 requests,** through the real handler with
  the call to the agent stubbed, so no model time or Telegram messages: each sent the
  rebuilt question, the right language, `deliver=telegram`, `wait_s=5` and a 7 s
  timeout. An empty slot gets "I didn't catch the question", and an unknown
  `AskFooIntent` the "not sure" reply.
- **Three through the real agent:** `AskWhatIsIntent` with "the plan tonight" spoke
  the plan sentence in 0.36 s. `AskWhyIntent` with "did the kubota start this morning"
  and `AskPorQueIntent` with "arrancó el kubota esta mañana" said the phone would get
  it, at about 5.1 s. The VPS log showed the full questions, such as "why did the
  kubota start this morning".

No start, stop, set-generator or enable/disable-autogen intent was sent. No real Alexa
request has reached the new intents, because they aren't in the console yet.
