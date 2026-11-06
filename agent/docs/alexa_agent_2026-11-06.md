# Alexa to the agent

2026-11-06. Brief: `claude-code-brief-alexa-agent.md`. How it works and how to run it: `alexa.md`.

The backend is live on both machines: KAMRUI on `88dcb3c`, and the VPS file from `1e2b80d`. **Alexa can't reach it yet, because the skill has no `AskAgentIntent`;** that's the console step below, which is the owner's. Every test here used a fake request body sent to the VPS, not a real Alexa request. All 1,238 tests pass.

## Live file against the repo

**The live file had nothing the repo lacks,** so there was nothing to merge. It was unchanged at the deploy (checksum `31f94a8e…`, the same as the first pull), and the owner saw the diff first. Every difference was an addition in the repo:

| Difference | Part of the agent intent? |
|---|---|
| `AGENT_ASK_URL`, timeouts, fallback and later strings, `ask_agent()` | Yes |
| The `AskAgentIntent` branch | Yes |
| One extra sentence in `AMAZON.HelpIntent`, English and Spanish | **No: the only other change.** Until the console step is done, Help offers something that doesn't work yet. |

Backup: `/var/www/alexa_solar.py.bak-2025-12-15`, same checksum. To roll back, restore it and run `systemctl restart alexa-solar`. Nothing needed rolling back.

## What was built

- **`POST /ask`** takes `"deliver": "telegram"` and `"wait_s"` (limited to 30 s). The answer runs on its own thread through `answer()`, so it queues behind `self.lock`. In time it's a 200. Otherwise it's a 202 at once, and Telegram gets `🎙 <question>` and the answer when ready. A lock decides between the two, so an answer landing on the deadline goes exactly one way. Without those fields the path is as before.
- **Plan phrasing (the owner's decision).** "what is the plan tonight", "what the plan is tonight", "plan tonight", "cuál es el plan de esta noche", "plan para hoy" and similar now count as plan questions, with or without accents. They used to go to the model. This applies to **every** path, so Telegram now returns the record for these phrases too. That's a deliberate exception to the brief's "plain callers unchanged".
- **Spoken plan (the owner's decision).** The record is about 180 words of shorthand. On the deliver path Alexa now says one sentence built in Python, in English or Spanish, and Telegram gets the record. Plain `/ask` still returns the record.

The sentence depends on the projection in the latest plan:

| Projection | English |
|---|---|
| Crosses before the next sunrise | The pack is projected to reach the 52.0 volt start at 5:31 am; the full plan is on your phone. |
| Crosses after it, or not within the horizon | The pack is not projected to reach the 52.0 volt start tonight; … |
| Already there | The pack is already at or below the 52.0 volt start; … |
| Can't say | I can't say when the pack will reach the 52.0 volt start; … |

"Can't say" is separate on purpose: a missing load profile isn't the same as no run tonight.

## Test results

**Existing intents, before and after, English and Spanish.** I compared 16 responses with the live numbers masked:

| Intent | Result |
|---|---|
| `LaunchRequest`, `GetBatteryIntent`, `GetStatusIntent`, `GetSolarIntent`, `GetGeneratorIntent`, `AMAZON.StopIntent` | Same |
| `GetEventsIntent` | Same, and **broken both before and after**; see below |
| `AMAZON.HelpIntent` | Differs only by the added sentence |

There's no separate autogen status intent. That status is the "Auto control enabled" part of `GetStatusIntent`, which matched. I sent no start, stop, set-generator or enable/disable-autogen intent.

**`AskAgentIntent`, from the VPS through the live agent:**

| Locale, question | Time | Alexa would say |
|---|---|---|
| en-US, "what is the plan tonight" | 0.35 s | The pack is projected to reach the 52.0 volt start at 5:31 am; the full plan is on your phone. |
| en-US, "what the plan is tonight" | 0.35 s | Same |
| es-US, "cuál es el plan de esta noche" | 0.35 s | Se proyecta que la batería llegue al arranque de 52.0 voltios a las 5:31 am; el plan completo está en tu teléfono. |
| en-US, no question | 0.00 s | I didn't catch the question. |
| en-US, "how much solar did we make yesterday" | 5.08 s | I'm checking — I'll send the answer to your phone. |

Plain `/ask` with "what is the plan tonight" returned 200 and the full record, 177 words.

**Telegram delivery is not confirmed from here.** Those tests should have sent the owner four messages: three plan records and yesterday's solar. The agent logged no send failure and called `get_history(window="yesterday")` for the late one, but I can't see the phone.

**Not run live:** a late answer in Spanish, a failure in the background, and the VPS's 7 s timeout. The first two are covered by unit tests. The VPS code has no unit tests in this repo.

**Unit tests** in `tests/test_ask_server.py` and `tests/test_qa.py`:

- answered within the wait
- answered late (202, then Telegram)
- Spanish
- a background failure, in and past the wait (Telegram gets the fallback, nothing hangs)
- Telegram itself failing
- two questions queued
- the unchanged default path
- each plan sentence in both languages
- the wider phrasing, including lookalikes that must not match, such as "what the plan is for the generator" and "cuál es el plan para el generador"

## Pre-existing break: `GetEventsIntent`

It answers "Sorry, I couldn't get the events." / "Lo siento, no pude obtener los eventos." It did so on the December file before this deploy too. I left it as is, as you asked, and didn't look into why.

## Limits to know about

- **Slow questions start with silence:** Alexa is silent for 5 s before it says "I'm checking".
- **The phone sentence is said before Telegram confirms.** If Telegram is down, Alexa has still said the plan is on your phone; the failure only reaches the agent's log.
- **If the VPS times out at 7 s,** Alexa speaks the fallback, and a late answer can still arrive on Telegram.
- **"No plan has been recorded yet"** is English only, in either language. It can only appear before the first tick, and it isn't new.

## Console steps, for the owner

For **both** en-US and es-US, in the skill listed as "solar system":

1. Build → Interaction Model → Intents → **+ Add Intent** → custom intent named exactly `AskAgentIntent`.
2. Add a slot named exactly `query`, type **`AMAZON.SearchQuery`**.
3. Add the sample utterances.

   en-US:
   ```
   ask the agent {query}
   ask agent {query}
   question for the agent {query}
   tell me {query}
   what does the agent say about {query}
   agent {query}
   ```
   es-US:
   ```
   pregunta al agente {query}
   preguntale al agente {query}
   pregunta {query}
   dime {query}
   que dice el agente sobre {query}
   agente {query}
   ```
4. Save Model → Build Model.
5. Test: "Alexa, ask my solar to ask the agent what the plan is tonight" and "Alexa, pregúntale a casa solar al agente cuál es el plan de esta noche".

## Docs

`alexa.md` and `SPEC.md` now give the invocation names as "my solar" and "casa solar", with the skill listed as "solar system", and note that "open my solar" collides with Alexa's built-in "solar". The names themselves weren't changed.
