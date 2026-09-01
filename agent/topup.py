"""The per-generator top-up state machine (POLICY 4).

Rule 4 used to re-derive the whole top-up from the pack's voltage on every
tick and remember nothing about what it had already done. On the night of
2026-08-30 that produced three separate top-up decisions between 7:20 and
8:55 pm — Kubota 54.1/56.1, then 54.0/56.0, then 54.6/56.6 — each one a fresh
answer to a question that had already been answered, and the last one written
while the Kubota was already running.

So the decision is a state, one per generator, and it is remembered:

    idle → requested → running → done
                     ↘         ↘ stopped_by_owner
                       failed_to_start

`idle` is the only state rule 4 evaluates in, and a generator leaves it once
per night. Everything else is a hold: the night's top-up has been decided, is
under way, or is over.

  requested         the agent has raised this generator's start
  running           `*Action == 9` has been seen; the start goes back to the
                    owner's baseline at once and nothing more is proposed
  done              the run ended at its stop voltage or on its runtime cap
  stopped_by_owner  the run ended before either, or the AGS mode went Off
  failed_to_start   the start was raised, the pack sat under it, and five
                    minutes later nothing was running

`done`, `stopped_by_owner` and `failed_to_start` all hold until the next
sunset. A night is named by the sunset that opened it, so the state a run
leaves behind at eleven at night is still in force at ten the next morning.
"""

import json
import logging
import os
import time

import config
import history
import sun as sunmod

log = logging.getLogger(__name__)

IDLE = "idle"
REQUESTED = "requested"
RUNNING = "running"
DONE = "done"
STOPPED_BY_OWNER = "stopped_by_owner"
FAILED_TO_START = "failed_to_start"

# States in which the night's top-up decision has already been made. None of
# them returns to idle before the next sunset.
SETTLED = (DONE, STOPPED_BY_OWNER, FAILED_TO_START)
IN_FLIGHT = (REQUESTED, RUNNING)

GENS = ("mep", "kubota")

# How long a raised start is given to produce a running generator. The Pi5
# polls its own thresholds every few seconds, so five minutes is not a race:
# it is long enough that a slow crank or one missed poll is not called a
# failure, and short enough that the night is not spent waiting.
START_TIMEOUT_SECONDS = 300

# A run that ends within this of its stop voltage stopped *on* it.
STOP_V_EPS = 0.05
# A run that ends within this of the runtime cap ended *on* the cap.
CAP_MINUTES_EPS = 1.0

AGS_MODE_OFF = 0

# States the owner is told about, once each. `done` is the plan working and
# `running` is announced by the write that puts the start back, so neither
# needs a message of its own.
NOTIFY = (STOPPED_BY_OWNER, FAILED_TO_START)


def _pretty(gen):
    return "MEP" if gen == "mep" else "Kubota"


class TopUp:
    """The state, persisted, plus the transitions that move it.

    Nothing here writes to the dashboard or sends a message. `advance()`
    returns what changed and why; the agent decides what to do about it, so
    the machine can be replayed against a night that has already happened
    without touching anything.
    """

    def __init__(self, cfg, path=None):
        self.cfg = cfg
        self.path = path or os.path.join(config.DATA_DIR, "topup_state.json")
        self.state = self._load()
        # Every move this object has made, in order. advance() returns the
        # ones it caused itself, but the guard moves the machine too - a
        # raised start, an owner's edit - and the replay wants all of them in
        # one place. Nothing depends on it; it is a record, not state.
        self.moves = []

    # --- persistence --------------------------------------------------------

    def _load(self):
        try:
            with open(self.path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        state.setdefault("night", None)
        gens = state.setdefault("gens", {})
        for gen in GENS:
            gens.setdefault(gen, {"state": IDLE, "since": 0})
        return state

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.path)

    # --- reading it ---------------------------------------------------------

    def entry(self, gen):
        return self.state["gens"][gen]

    def status(self, gen):
        return self.entry(gen)["state"]

    def idle_gens(self):
        return [g for g in GENS if self.status(g) == IDLE]

    def in_flight(self):
        return [g for g in GENS if self.status(g) in IN_FLIGHT]

    def settled(self):
        return [g for g in GENS if self.status(g) in SETTLED]

    def pending_notices(self):
        """Generators whose state the owner has not been told about yet."""
        return [g for g in GENS if self.status(g) in NOTIFY
                and not self.entry(g).get("notified")]

    def mark_notified(self, gen):
        self.state["gens"][gen]["notified"] = True
        self.save()

    def snapshot(self):
        """What policy.py is handed. A copy: the rules read, they do not move it."""
        return {"night": self.state.get("night"),
                "gens": {g: dict(self.entry(g)) for g in GENS}}

    # --- the night ----------------------------------------------------------

    def night_key(self, now):
        """The local date of the sunset that opened the night `now` falls in.

        Before today's sunset the night in force is still the one that opened
        yesterday evening, so a run that finished at eleven last night keeps
        holding all through this morning.
        """
        today = history.local_day(now, self.cfg)
        times = sunmod.times(self.cfg, today)
        if times and now >= times[1]:
            return today
        return history.local_day(now - 86400, self.cfg)

    def roll(self, now):
        """Start a fresh night if the sunset has moved on. Returns transitions."""
        night = self.night_key(now)
        if self.state.get("night") == night:
            return []
        was = {g: self.status(g) for g in GENS}
        self.state["night"] = night
        moved = []
        for gen in GENS:
            if was[gen] != IDLE:
                moved.append(self._set(gen, IDLE, now,
                                       f"a new night opened ({night})"))
            else:
                self.state["gens"][gen] = {"state": IDLE, "since": now}
        self.save()
        return moved

    # --- moving it ----------------------------------------------------------

    def _set(self, gen, state, now, why, **extra):
        was = self.status(gen)
        entry = {"state": state, "since": int(now), "why": why}
        if state in NOTIFY:
            entry["notified"] = False
        entry.update(extra)
        # What was asked for survives the move, so `done` still knows the
        # numbers the night was decided on and the no-creep rule still has
        # them to measure a later proposal against.
        for key in ("start", "stop", "requested_at", "detail"):
            if key in self.entry(gen) and key not in entry:
                entry[key] = self.entry(gen)[key]
        self.state["gens"][gen] = entry
        log.info("top-up %s: %s → %s (%s)", gen, was, state, why)
        moved = {"gen": gen, "from": was, "to": state, "ts": int(now),
                 "why": why, "entry": dict(entry)}
        self.moves.append(moved)
        return moved

    def request(self, gen, start, stop, now, detail=""):
        """The agent has raised this generator's start. Once per night."""
        if self.status(gen) != IDLE:
            return None
        moved = self._set(gen, REQUESTED, now,
                          f"the agent raised {_pretty(gen)}'s start to "
                          f"{start:.1f} with a stop of {stop:.1f}",
                          start=float(start), stop=float(stop),
                          requested_at=int(now), detail=detail)
        self.save()
        return moved

    def owner_took_it(self, gen, now, why):
        """The owner moved this generator's thresholds. Their night now."""
        if self.status(gen) == STOPPED_BY_OWNER:
            return None
        moved = self._set(gen, STOPPED_BY_OWNER, now, why)
        self.save()
        return moved

    def advance(self, observed, now):
        """Every transition the tick's observations imply, in order.

        `observed` is one dict per generator:
          action        `*Action`, 9 running / 10 stopped
          mode          `*Mode`, 0 off / 1 on / 2 auto
          voltage       the pack, now
          stop_v        the stop threshold in force for this generator
          run           the generator's newest run row, or None
          cap_minutes   min(Pi5 maxRuntime, the AGS limit)
        """
        now = int(now)
        moved = list(self.roll(now))
        for gen in GENS:
            o = observed.get(gen) or {}
            moved += self._advance_one(gen, o, now)
        if moved:
            self.save()
        return moved

    def _advance_one(self, gen, o, now):
        state = self.status(gen)
        if state not in IN_FLIGHT:
            return []
        action, mode = o.get("action"), o.get("mode")
        running = action == history.GEN_RUNNING

        if state == REQUESTED:
            if running:
                return [self._set(gen, RUNNING, now,
                                  f"{_pretty(gen)} is running")]
            if mode == AGS_MODE_OFF:
                return [self._set(gen, STOPPED_BY_OWNER, now,
                                  f"{_pretty(gen)}'s AGS mode went Off before "
                                  f"it started")]
            return self._timed_out(gen, o, now)

        # RUNNING.
        if mode == AGS_MODE_OFF:
            return [self._set(gen, STOPPED_BY_OWNER, now,
                              f"{_pretty(gen)}'s AGS mode went Off mid-run")]
        if running:
            return []
        return [self._run_ended(gen, o, now)]

    def _timed_out(self, gen, o, now):
        """Nothing running, five minutes after a start the pack sits under."""
        entry = self.entry(gen)
        start = entry.get("start")
        voltage, since = o.get("voltage"), entry.get("requested_at") or entry["since"]
        if start is None or voltage is None:
            return []
        # The Pi5 starts a generator when the pack is *below* the start
        # threshold. While the pack is above it there is nothing to wait for
        # and nothing has failed.
        if voltage > start + STOP_V_EPS:
            return []
        if now - since < START_TIMEOUT_SECONDS:
            return []
        return [self._set(gen, FAILED_TO_START, now,
                          f"{_pretty(gen)}'s start was raised to {start:.1f} "
                          f"with the pack at {voltage:.2f} V and nothing was "
                          f"running {int((now - since) / 60)} minutes later",
                          ags_mode=o.get("mode"), ags_action=o.get("action"),
                          ags_online=o.get("ags_online"),
                          auto_gen_enabled=o.get("auto_gen_enabled"),
                          voltage=voltage)]

    def _run_ended(self, gen, o, now):
        """`done` if the run reached its stop or its cap, else the owner's."""
        run, stop_v = o.get("run"), o.get("stop_v")
        cap = o.get("cap_minutes")
        reached = (run or {}).get("stop_v")
        minutes = (run or {}).get("duration_min")
        if reached is None:
            reached = o.get("voltage")
        if minutes is None:
            minutes = (now - self.entry(gen)["since"]) / 60.0

        at_stop = (stop_v is not None and reached is not None
                   and reached >= stop_v - STOP_V_EPS)
        at_cap = (cap is not None and minutes is not None
                  and minutes >= cap - CAP_MINUTES_EPS)
        shown = f"{reached:.2f} V" if reached is not None else "an unknown voltage"
        span = f"{minutes:.0f} min" if minutes is not None else "an unknown time"
        if at_stop:
            return self._set(gen, DONE, now,
                             f"{_pretty(gen)} ran {span} and stopped at {shown}, "
                             f"its stop threshold", stop_v=stop_v,
                             ran_minutes=minutes, ended_v=reached)
        if at_cap:
            return self._set(gen, DONE, now,
                             f"{_pretty(gen)} ran {span} and stopped at {shown} "
                             f"on its {cap:.0f} min runtime cap",
                             stop_v=stop_v, ran_minutes=minutes, ended_v=reached)
        target = f"its {stop_v:.1f} stop" if stop_v is not None else "its stop"
        cap_note = f" and its {cap:.0f} min cap" if cap is not None else ""
        return self._set(gen, STOPPED_BY_OWNER, now,
                         f"{_pretty(gen)} ran {span} and stopped at {shown}, "
                         f"short of {target}{cap_note}",
                         stop_v=stop_v, ran_minutes=minutes, ended_v=reached)


def failed_to_start_message(gen, entry, baseline_start, other=None):
    """What the owner is told when a raised start produced nothing.

    The AGS state is the first thing worth knowing: the controller answers
    the Pi5, and a mode of Off or an unreachable AGS explains the silence
    without anyone walking out to the generator.
    """
    mode = entry.get("ags_mode")
    names = {0: "Off", 1: "On", 2: "Auto"}
    lines = [f"⚠️ {_pretty(gen)} didn't start — AGS state "
             f"{mode if mode is not None else '?'}"
             + (f" ({names[mode]})" if mode in names else "")
             + ", controller may need a reset"]
    detail = [f"start was {entry.get('start'):.1f} with the pack at "
              f"{entry.get('voltage'):.2f} V"
              if entry.get("start") is not None and entry.get("voltage") is not None
              else None,
              f"action {entry.get('ags_action')}",
              "AGS offline" if entry.get("ags_online") is False else None,
              "auto-gen is disabled on the dashboard"
              if entry.get("auto_gen_enabled") is False else None]
    lines.append("; ".join(d for d in detail if d) + ".")
    lines.append(f"Its start goes back to {baseline_start:.1f}"
                 + (f" and the top-up is re-evaluated with the {_pretty(other)}."
                    if other else " and it is held until the next sunset."))
    return "\n".join(lines)


def stopped_by_owner_message(gen, entry):
    return (f"ℹ️ {_pretty(gen)}'s top-up ended as the owner's, not the agent's — "
            f"{entry.get('why', 'the run did not end on its stop or its cap')}. "
            f"No further top-up for it until the next sunset.")
