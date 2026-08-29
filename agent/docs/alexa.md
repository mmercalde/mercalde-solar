# Alexa — AskAgentIntent

Lets the owner put a free-form question to the solar agent by voice, in
English or Spanish. Every existing intent is untouched.

```
"Alexa, ask solar system what the plan is tonight"
"Alexa, pregunta a solar system cual es el plan de esta noche"
```

## What happens

1. Alexa sends an `IntentRequest` for `AskAgentIntent` to
   `https://mercalde-solar.org/alexa`.
2. nginx on the VPS proxies it to `alexa-solar.service`
   (`/var/www/alexa_solar.py`) on 127.0.0.1:5000.
3. The handler reads the `query` slot and POSTs
   `{"text": <query>, "lang": "es"|"en"}` to `http://192.168.3.152:8090/ask`
   on the KAMRUI, over WireGuard through the Pi5, with an 8 second timeout.
4. The agent answers in at most 60 words of plain speech, in the language it
   was asked in, and Alexa speaks it.

Language follows `request.locale` through the existing `is_spanish(data)`
helper — no new language handling was added.

On any failure — the agent down, a non-200, a timeout, an empty reply — Alexa
speaks a fixed fallback in the right language rather than an error:

| Language | Fallback |
|---|---|
| English | "The solar agent is not answering right now." |
| Spanish | "El agente solar no responde en este momento." |

No push notifications in v1: Alexa only answers when asked.

## Developer console changes

Open the skill at <https://developer.amazon.com/alexa/console/ask>.

### 1. Confirm the invocation name

Build → Invocation. The SPEC records it as **solar system**. Verify it
matches before testing; it could not be checked from the repository, and the
sample utterances below assume it.

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

English:

```
ask the agent {query}
ask agent {query}
question for the agent {query}
tell me {query}
what does the agent say about {query}
agent {query}
```

Spanish (add these under the `es-MX` / `es-US` locale):

```
pregunta al agente {query}
preguntale al agente {query}
pregunta {query}
dime {query}
que dice el agente sobre {query}
agente {query}
```

### 5. Build and test

Save Model → Build Model. Then in the Test tab:

```
ask solar system to ask the agent what the plan is tonight
```

Switch the test locale to Spanish and try:

```
pregunta a solar system que dice el agente sobre la bateria
```

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

The VPS handler, without going through Amazon:

```bash
ssh root@45.32.131.224 "curl -s -X POST http://127.0.0.1:5000/alexa \
  -H 'Content-Type: application/json' \
  -d '{\"request\":{\"type\":\"IntentRequest\",\"locale\":\"en-US\",
       \"intent\":{\"name\":\"AskAgentIntent\",
       \"slots\":{\"query\":{\"name\":\"query\",\"value\":\"what is the plan\"}}}}}'"
```

## Verified before deployment

Tested against a live `/ask` server with real Alexa request bodies:

| Case | Result |
|---|---|
| English question | agent's answer spoken |
| Spanish question (`es-MX` locale) | agent's answer spoken in Spanish |
| Empty `query` slot | "I didn't catch the question." |
| Slot missing entirely | "No entendi la pregunta." |
| Agent unreachable | English fallback |
| Agent unreachable, Spanish locale | Spanish fallback |
| `GetBatteryIntent`, `AMAZON.HelpIntent`, `AMAZON.StopIntent`, unknown intent | unchanged |
