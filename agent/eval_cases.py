"""Q&A cases with their ground truth, for `model_eval.py --exam`.

model_eval.py replays tick prompts. This is the other half: questions put to
the running agent over POST /ask, marked against what the database actually
says. Every case here is a question the agent has already got wrong once, so
the file doubles as the regression record for the ask path.

A ground truth is computed from the database at run time, never written down
here. A case whose expected answer is a constant stops being a test of the
agent the first time the pack moves.
"""

import re

import history
import tools as toolsmod

DEFAULT_START = 52.0            # only used to say what a raised start is not


def _minutes(rows):
    return round(sum(r["duration_min"] or 0 for r in rows), 1)


# A clock time and a date are not figures the model is asserting. "2:47 am"
# must not put 47 into the numbers it stated: grading the live 8B, this
# reported the model as having said "47.0 V" when it had said 52.84, and a
# time landing within the tolerance of the true voltage would have passed an
# answer that was wrong.
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?\s?m\.?)?",
                     re.IGNORECASE)
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _numbers(text):
    """Every figure the answer states, with times and dates taken out first."""
    cleaned = DATE_RE.sub(" ", TIME_RE.sub(" ", text or ""))
    out = []
    for raw in re.findall(r"-?\d[\d,]*(?:\.\d+)?", cleaned):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return out


def _declined(reply):
    """Does the answer admit it cannot say, rather than guessing?"""
    return bool(re.search(
        r"\b(no sample|not recorded|cannot|can't|could not|couldn't|unable|"
        r"don't have|do not have|no data|not available|no reading)\b",
        reply or "", re.IGNORECASE))


# --- the cases ---------------------------------------------------------------

def truth_gen_starts(conn, cfg, now):
    """Kubota starts by day, and whether a raised start was in force."""
    today = history.local_day(now, cfg)
    yesterday = history.local_day(now - 86400, cfg)
    by_day = {}
    for r in history.gen_runs(conn, 3, now=now):
        if r["gen"] != "kubota":
            continue
        day = history.local_day(r["start_ts"], cfg)
        if history.local(r["start_ts"], cfg).hour < 12:
            by_day.setdefault(day, []).append(r)
    raised = [r for rows in by_day.values() for r in rows
              if r["start_v"] is not None and r["start_v"] > DEFAULT_START]
    return {"today": today, "yesterday": yesterday,
            "starts_today": len(by_day.get(today, [])),
            "starts_yesterday": len(by_day.get(yesterday, [])),
            "started_above_default": [r["start_v"] for r in raised]}


def grade_gen_starts(reply, truth):
    """The question says yesterday; the two starts were this morning.

    A pass either names the raised start threshold that let a generator run
    at a voltage the default would not have started it at, or corrects the
    day. Blaming a low battery is the answer that was wrong before: the pack
    was above the default start both times.
    """
    low = (reply or "").lower()
    names_threshold = bool(re.search(r"5[23]\.\d", reply or "")) and \
        re.search(r"\b(start|threshold)\b", low) is not None
    corrects_day = (truth["starts_yesterday"] != 2
                    and re.search(r"\b(today|this morning|not yesterday|only one|once)\b",
                                  low) is not None)
    blames_low_battery = re.search(
        r"\b(low|dropped|fell|below)\b.{0,40}\b(battery|voltage|charge)\b|"
        r"\bbattery\b.{0,30}\b(low|dropped|fell)\b", low) is not None
    if names_threshold or corrects_day:
        return True, ("names the raised start threshold" if names_threshold
                      else "corrects the day the starts were on")
    if blames_low_battery:
        return False, ("blames a low battery; the pack was above the "
                       f"{DEFAULT_START} default at both starts")
    return False, "neither cites the raised start threshold nor checks the day"


def truth_runtime(conn, cfg, now):
    runs = history.gen_runs(conn, 1, now=now)
    return {gen: {"runs": len([r for r in runs if r["gen"] == gen]),
                  "minutes": _minutes([r for r in runs if r["gen"] == gen])}
            for gen in history.GENS}


def grade_runtime(reply, truth):
    """Every figure quoted must be one of the real ones, and a generator that
    did not run must be reported as not having run."""
    said = _numbers(reply)
    allowed = set()
    for v in truth.values():
        allowed |= {float(v["runs"]), round(float(v["minutes"]), 1)}
        allowed.add(round(v["minutes"] / 60.0, 1))
    wrong = [n for n in said if n not in allowed and n != 24]
    if wrong:
        return False, f"quoted figures not in gen_runs: {wrong}"
    idle = [g for g, v in truth.items() if v["runs"] == 0]
    for gen in idle:
        if gen not in (reply or "").lower():
            return False, f"{gen} ran not at all and is not mentioned"
        if not re.search(rf"{gen}[^.]*\b(no|not|zero|0|did ?n[o']t)\b",
                         (reply or "").lower()):
            return False, f"does not say plainly that the {gen} did not run"
    return True, "figures match gen_runs and idle generators are named"


def truth_plan(conn, cfg, now):
    plan = history.latest_plan(conn)
    return {"text": plan["text"] if plan else None}


def grade_plan(reply, truth):
    """The record itself, not a paraphrase of it."""
    if truth["text"] is None:
        return "no plan" in (reply or "").lower(), "no plan has been recorded"
    if (reply or "").strip() == truth["text"].strip():
        return True, "the stored plan record, verbatim"
    missing = [line.split(":")[0] for line in truth["text"].splitlines()
               if line.split(":")[0] and line.split(":")[0] not in (reply or "")]
    return False, f"not the record; missing {missing[:3]}"


def truth_voltage_at(conn, cfg, now, hhmm="02:47"):
    when, _ = toolsmod.parse_when(hhmm, cfg, now)
    row = conn.execute(
        "SELECT ts, battery_v FROM samples WHERE ts BETWEEN ? AND ? "
        "ORDER BY ABS(ts - ?) LIMIT 1",
        (when - toolsmod.SAMPLE_WINDOW_SECONDS,
         when + toolsmod.SAMPLE_WINDOW_SECONDS, when)).fetchone()
    return {"asked_for": history.stamp(when, cfg),
            "voltage": row["battery_v"] if row else None,
            "sample_at": history.stamp(row["ts"], cfg) if row else None}


def grade_voltage_at(reply, truth):
    """The reading at that minute, or an honest refusal. Nothing else.

    The failure this exists for: the 24-hour minimum offered as the reading
    at 2:47 am, 1.4 V out and six hours adrift.
    """
    said = _numbers(reply)
    volts = [n for n in said if 40.0 <= n <= 65.0]
    if truth["voltage"] is None:
        return (_declined(reply) and not volts,
                "no sample exists, so the only right answer is to say so")
    near = [v for v in volts if abs(v - truth["voltage"]) <= 0.05]
    if near:
        return True, f"{near[0]} matches the sample at {truth['sample_at']}"
    if volts:
        return False, (f"stated {volts[0]} V; the sample at "
                       f"{truth['sample_at']} reads {truth['voltage']} V")
    if _declined(reply):
        return False, (f"declined, but the sample at {truth['sample_at']} "
                       f"reads {truth['voltage']} V")
    return False, "no voltage given and no refusal either"


CASES = [
    {"id": "kubota_twice",
     "question": "Why did the Kubota start twice yesterday morning?",
     "truth": truth_gen_starts, "grade": grade_gen_starts,
     "about": "a raised start threshold, and a false premise about the day"},
    {"id": "runtime_24h",
     "question": "How long did each generator run in the last 24 hours?",
     "truth": truth_runtime, "grade": grade_runtime,
     "about": "figures that match gen_runs, and naming a generator that idled"},
    {"id": "tonight_plan",
     "question": "What is tonight's plan?",
     "truth": truth_plan, "grade": grade_plan,
     "about": "the stored plan record rather than a paraphrase"},
    {"id": "voltage_at_247",
     "question": "What was the battery voltage at exactly 2:47 am last night?",
     "truth": truth_voltage_at, "grade": grade_voltage_at,
     "about": "a point reading, or a refusal - never a window aggregate"},
]
