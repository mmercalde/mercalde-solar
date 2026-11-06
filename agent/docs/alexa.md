# Alexa — AskAgentIntent

Lets the owner put a free-form question to the solar agent by voice, in
English or Spanish. Every existing intent is untouched.

```
"Alexa, ask my solar to ask the agent what the plan is tonight"
"Alexa, pregúntale a casa solar al agente cuál es el plan de esta noche"
```

The invocation names are **"my solar"** (en-US) and **"casa solar"** (es-US). The
skill is only *listed* as "solar system". "Alexa, open my solar" collides with
Alexa's built-in "solar" and gets "not supported on this device"; "Alexa, ask my
solar for the battery status" works, so start with "ask".

## What happens

1. Alexa sends an `IntentRequest` for `AskAgentIntent` to
   `https://mercalde-solar.org/alexa`.
2. nginx on the VPS proxies it to `alexa-solar.service`
   (`/var/www/alexa_solar.py`) on 127.0.0.1:5000.
3. The handler reads the `query` slot and POSTs
   `{"text": <query>, "lang": "es"|"en", "deliver": "telegram", "wait_s": 5}` to
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

## Developer console changes

Done by the owner on 2026-11-06: added in en-US and es-US and built at 9:36 am,
then tested in the simulator. These are the steps, should the intent need redoing.
The interaction model is per locale, so do them for **both**.

Open the skill, listed as "solar system", at
<https://developer.amazon.com/alexa/console/ask>.

### 1. Don't change the invocation names

Build → Invocation should show "my solar" (en-US) and "casa solar" (es-US). The
utterances and test phrases below assume them.

### 2. Add the intent

Build → Interaction Model → Intents → **+ Add Intent** → *Create custom
intent* → name it exactly `AskAgentIntent`.

### 3. Add the slot

In that intent, add a slot named exactly `query` with slot type
**`AMAZON.SearchQuery`**.

`AMAZON.SearchQuery` may not be the only slot in an utterance and every
utterance using it needs at least one carrier word before it — that is why
each sample below starts with a verb phrase.

### 4. Sample utterances

These are the ones the skill actually has. "tell me {query}", "dime {query}" and a
bare "pregunta {query}" are left out on purpose, so they can't steal utterances from
the existing intents. Don't add them back.

English:

```
ask the agent {query}
ask agent {query}
question for the agent {query}
what does the agent say about {query}
agent {query}
```

Spanish (add these under the `es-US` locale):

```
pregunta al agente {query}
preguntale al agente {query}
pregúntale al agente {query}
que dice el agente sobre {query}
agente {query}
```

### 5. Build and test

Save Model → Build Model, in each locale. Then in the Test tab, en-US:

```
ask my solar to ask the agent what the plan is tonight
ask my solar to ask the agent how much solar did we make yesterday
```

The first should speak one sentence and send the plan to Telegram. The second
should say it's checking, then send the answer to Telegram. In es-US:

```
pregúntale a casa solar al agente cuál es el plan de esta noche
```

On a device: "Alexa, ask my solar to ask the agent what the plan is tonight" and
"Alexa, pregúntale a casa solar al agente cuál es el plan de esta noche".

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

See `alexa_agent_2026-11-06.md`. The cases there used fake request bodies sent to
the VPS. The owner then tested the built intent in the Alexa simulator.
