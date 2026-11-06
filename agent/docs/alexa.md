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

Spanish synonyms: los dos, ambos, todos los generadores, ambos generadores, el militar,
mep 803, el kubota, generador kubota.

The VPS has its own backstop in these four handlers. It takes the name from the slot's
entity resolution when the status code is `ER_SUCCESS_MATCH`, otherwise the raw value.
`get_gen_id` matches whole words, so "small" isn't "all". If neither maps to a generator,
it says "Which generator, the Kubota or the MEP?" /
"¿Cuál generador, el Kubota o el MEP?", keeps the session open and moves nothing.
"unknown" never reaches speech, and a leading "the " or "el " is dropped from the name
before it's spoken ("start the kubota?"). `get_gen_id` still gets the name as it came. `DENIED` still gets "OK, I won't", since a refusal needs
no name. The `CONFIRMED`-only rule is unchanged, and Auto still doesn't check for
confirmation.

`SessionEndedRequest` gets an empty response, and its reason and error go to the log
as `DEBUG SessionEnded`. That is where to look when Alexa reports an invalid response.

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
