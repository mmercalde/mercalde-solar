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
import loadmodel
import monthly
import sun as sunmod
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
    # Nothing started on either day, so the right answer is that it did not.
    # The exam of 2026-11-04 failed every such reply for want of a threshold,
    # and the one that invented two starts failed for the same reason.
    if truth["starts_yesterday"] == 0 and truth["starts_today"] == 0:
        if re.search(r"(?:^|[.!?]\s+)(?:the\s+)?kubota\s+started\b", low):
            return False, "says the Kubota started; no start is on record"
        if re.search(r"\b(?:did not|didn'?t|has not|hasn'?t|never|no|not)\b"
                     r".{0,30}\b(?:start|starts|started|run|runs|ran)\b|"
                     r"\b(?:no|zero)\s+generator\s+minutes\b", low):
            return True, "says it did not start, and no start is on record"
        return False, "no start is on record and the answer does not say so"
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
    """Exercise runs count. On 2026-11-05 "neither generator ran" passed, with the
    Kubota 14 minutes into its exercise, because this key left them out too."""
    runs = history.gen_runs(conn, 1, now=now, include_exercise=True)
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
    # "Neither generator ran" names both without using either name, and failed
    # for it on 2026-11-04.
    if len(idle) == len(truth) and re.search(
            r"\bneither\b|\bno generators?\b|\bnone of the generators\b|"
            r"\b(?:the|both) generators?\b[^.]*\b(?:no|not|zero|did ?n[o']t)\b",
            (reply or "").lower()):
        return True, "no generator ran, and the answer says so of both"
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



def truth_last_night_low(conn, cfg, now):
    start, end, _ = sunmod.window_span(cfg, "last_night", now)
    row = conn.execute(
        "SELECT MIN(battery_v) low FROM samples WHERE ts >= ? AND ts <= ?",
        (start, end)).fetchone()
    return {"from": history.stamp(start, cfg), "to": history.stamp(end, cfg),
            "low_v": row["low"]}


def grade_last_night_low(reply, truth):
    """The finished night's low. In the evening, tonight's so far is the trap."""
    volts = [n for n in _numbers(reply) if 40.0 <= n <= 65.0]
    if truth["low_v"] is None:
        return _declined(reply) and not volts, "no samples for that night"
    if any(abs(v - truth["low_v"]) <= 0.05 for v in volts):
        return True, f"{truth['low_v']} V is the low from {truth['from']} to {truth['to']}"
    if volts:
        return False, (f"stated {volts[0]} V; the low from {truth['from']} to "
                       f"{truth['to']} was {truth['low_v']} V")
    return False, "no voltage given"


def truth_shortfall(conn, cfg, now):
    """The same deficit the top-up rule decides on, worked out from the database."""
    until = sunmod.next_sunrise(cfg, now=now)
    d = loadmodel.LoadModel(conn, cfg).overnight_deficit(until, now=now)
    if d.get("deficit_wh") is None:
        return {"shortfall_wh": None, "reason": d.get("reason")}
    truth = {"shortfall_wh": max(0, round(d["needed_wh"] - d["available_wh"])),
             "needed_wh": d["needed_wh"], "available_wh": d["available_wh"]}
    if sunmod.daylight(cfg, now):
        truth["sun_up"] = True
    return truth


NO_SHORTFALL_RE = re.compile(
    r"\bno\s+(?:\w+\s+){0,2}(?:shortfall|deficit)\b|"
    r"\bwithout\s+(?:a\s+|any\s+)?(?:shortfall|deficit)\b|"
    r"\b(?:is not|isn'?t|not)\s+(?:a\s+|any\s+)?(?:shortfall|deficit)\b|"
    r"\b(?:shortfall|deficit)\b[^.]{0,30}\b(?:none|zero|0)\b", re.IGNORECASE)


# The tool's "Not final" sentence, in the words a reply might put it in. A time
# on its own is not enough: "a generator starts at 9 pm" is not that caveat.
NOT_FINAL_RE = re.compile(
    r"\bnot\s+(?:yet\s+)?(?:the\s+)?final\b|\bshrink|\bstill\s+to\s+come\b|"
    r"\bdecid(?:es?|ed)\b[^.]{0,40}\b\d{1,2}(?::\d\d)?\s*[ap]\.?m\b", re.IGNORECASE)


def _grade_shortfall_figure(reply, truth):
    wanted = truth["shortfall_wh"]
    stated = [(wh, unit) for wh, unit in _energies(reply) if unit]
    as_wh = [v * 1000.0 if unit == "kwh" else v for v, unit in stated]
    if wanted == 0:
        if NO_SHORTFALL_RE.search(reply or ""):
            return True, f"no shortfall: {truth['needed_wh']} Wh needed of {truth['available_wh']}"
        if any(abs(v - truth["available_wh"]) <= 0.05 * truth["available_wh"] for v in as_wh):
            return False, "gives the energy available as if it were a shortfall"
        return False, "there is no shortfall and the answer does not say so"
    if any(abs(v - wanted) <= max(500, 0.1 * wanted) for v in as_wh):
        return True, f"matches the {wanted} Wh shortfall"
    return False, f"the shortfall is {wanted} Wh and no stated figure matches it"


def grade_shortfall(reply, truth):
    """Available energy read out as a shortfall is the failure this exists for.

    While the sun is up the figure is not settled, and a reply that gives it as
    if it were has the plan's answer wrong however right the number.
    """
    if truth["shortfall_wh"] is None:
        return _declined(reply), f"cannot be known: {truth.get('reason')}"
    ok, why = _grade_shortfall_figure(reply, truth)
    if ok and truth.get("sun_up") and not NOT_FINAL_RE.search(reply or ""):
        return False, f"{why}, but the sun is up and the reply does not say it is not final"
    return ok, why


def truth_worst_month_this_year(conn, cfg, now):
    """This year's complete months, from the daily rollup and not from monthly."""
    here = history.local(now, cfg)
    this_month = here.strftime("%Y-%m")
    rows = conn.execute(
        "SELECT substr(day, 1, 7) month, SUM(solar_wh) wh, COUNT(*) days FROM daily "
        "WHERE day >= ? AND substr(day, 1, 7) < ? GROUP BY month",
        (f"{here.year}-01-01", this_month)).fetchall()
    full = [r for r in rows if r["days"] >= monthly.MIN_DAYS_FOR_MONTH_RANKING]
    worst = min(full, key=lambda r: r["wh"]) if full else None
    return {"month": worst["month"] if worst else None,
            "kwh": round(worst["wh"] / 1000.0, 1) if worst else None}


MONTH_NAMES = {
    1: ("January", "enero"), 2: ("February", "febrero"), 3: ("March", "marzo"),
    4: ("April", "abril"), 5: ("May", "mayo"), 6: ("June", "junio"),
    7: ("July", "julio"), 8: ("August", "agosto"), 9: ("September", "septiembre"),
    10: ("October", "octubre"), 11: ("November", "noviembre"),
    12: ("December", "diciembre")}


def grade_worst_month_this_year(reply, truth):
    if truth["month"] is None:
        return _declined(reply), "this year has no complete month yet"
    text = reply or ""
    year, month = truth["month"].split("-")
    names = MONTH_NAMES[int(month)] + (truth["month"],)
    if not any(re.search(rf"\b{n}\b", text, re.IGNORECASE) for n in names):
        return False, f"the worst complete month this year is {truth['month']}"
    other_years = [y for y in re.findall(r"\b20\d{2}\b", text) if y != year]
    if other_years:
        return False, f"names {other_years[0]}; the answer is in {year}"
    return True, f"{truth['month']}, {truth['kwh']} kWh"


def _energies(text):
    """(value, unit) for each figure, with unit 'kwh', 'wh' or None.

    Reads 33,377 and 33.377 Wh as thousands, and 33,4 as a Spanish decimal.
    """
    cleaned = DATE_RE.sub(" ", TIME_RE.sub(" ", text or ""))
    out = []
    for raw, unit in re.findall(r"(-?\d[\d.,]*)\s*(kwh|wh)?\b", cleaned, re.IGNORECASE):
        unit = unit.lower() or None
        raw = raw.rstrip(".,")
        if re.fullmatch(r"-?\d+,\d{1,2}", raw):
            raw = raw.replace(",", ".")
        elif unit == "wh" and re.fullmatch(r"-?\d{1,3}\.\d{3}", raw):
            raw = raw.replace(".", "")
        try:
            out.append((float(raw.replace(",", "")), unit))
        except ValueError:
            pass
    return out


def truth_solar_yesterday(conn, cfg, now):
    start, end, _ = sunmod.window_span(cfg, "yesterday", now)
    wh = conn.execute(
        "SELECT SUM(wh_in) wh FROM hourly WHERE device='solar' "
        "AND hour_ts >= ? AND hour_ts < ?", (start, end)).fetchone()["wh"]
    return {"wh": round(wh) if wh is not None else None,
            "kwh": round(wh / 1000.0, 1) if wh is not None else None}


def grade_solar_yesterday(reply, truth):
    """33.4 kWh or 33,377 Wh. 33,377 kWh is a thousand times the array's output."""
    if truth["wh"] is None:
        return _declined(reply), "no solar recorded for yesterday"
    for value, unit in _energies(reply):
        if unit in ("kwh", None) and abs(value - truth["kwh"]) <= 0.5:
            return True, f"{truth['kwh']} kWh"
        if unit == "wh" and abs(value - truth["wh"]) <= 500:
            return True, f"{truth['wh']} Wh"
    if any(unit == "kwh" and abs(value - truth["wh"]) <= 500
           for value, unit in _energies(reply)):
        return False, f"calls {truth['wh']} Wh kilowatt-hours"
    return False, f"yesterday's solar was {truth['kwh']} kWh and no figure matches"


SPANISH_RE = re.compile(r"\b(ayer|produj\w*|generó|generaron|fueron|fue|de solar)\b",
                        re.IGNORECASE)


def grade_solar_yesterday_es(reply, truth):
    passed, why = grade_solar_yesterday(reply, truth)
    if passed and not SPANISH_RE.search(reply or ""):
        return False, "right figure, but not answered in Spanish"
    return passed, why


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
    {"id": "last_night_low",
     "question": "What was the lowest battery voltage last night?",
     "truth": truth_last_night_low, "grade": grade_last_night_low,
     "about": "the night that finished, even when asked in the evening"},
    {"id": "tonight_shortfall",
     "question": "Will a generator run tonight, and what's the shortfall?",
     "truth": truth_shortfall, "grade": grade_shortfall,
     "about": "the shortfall itself, not the energy available"},
    {"id": "worst_month_this_year",
     "question": "Which month this year had the worst solar?",
     "truth": truth_worst_month_this_year, "grade": grade_worst_month_this_year,
     "about": "this year's complete months, not the whole record"},
    {"id": "solar_yesterday_kwh",
     "question": "How many kWh of solar did we make yesterday?",
     "truth": truth_solar_yesterday, "grade": grade_solar_yesterday,
     "about": "Wh and kWh kept apart"},
    {"id": "solar_yesterday_kwh_es",
     "question": "¿Cuántos kWh de solar produjimos ayer?", "lang": "es",
     "truth": truth_solar_yesterday, "grade": grade_solar_yesterday_es,
     "about": "the same in Spanish, where it went wrong"},
]
