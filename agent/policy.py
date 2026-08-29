"""The numeric POLICY rules, evaluated in Python.

The model reads POLICY as prose and decides. That leaves it free to read
"today's peak stayed below 57.0" and answer "no change" without ever doing the
comparison, which is what happened on the first live night: the plan record
projected 52 V at 03:08 with a peak of 55.0, and every tick still said "no
change".

So every rule whose condition is arithmetic is computed here, printed into the
plan record with its numbers, and handed to the model as a finding it must
either act on or overrule in writing. Three rules qualify:

  POLICY 3  raise both stops to 57.0 before a storm or heavy cloud
  POLICY 3  drop both stops to 54.5 when the run lands shortly before a clear
            sunrise (superseded by POLICY 4, whose 57.0 target is the point)
  POLICY 4  solo top-up to 57.0 when today's peak fell short and the pack is
            projected to 52 V before sunrise

Nothing here writes anything. A firing rule is a proposal with a reason;
guard.py still decides whether it may be applied.
"""

import math
import re

import history

# Guard rule 1's separation, applied here so a proposal is never born invalid.
MIN_STOP_MINUS_START = 2.0
# Thresholds are written to one decimal.
EPS = 0.05

# How the model says it has considered a rule and decided against it.
OVERRULE_RE = re.compile(r"overrul\w*\s*:?\s*policy\s*(\d+)", re.IGNORECASE)


def _clock(ts, cfg):
    return history.local(ts, cfg).strftime("%H:%M") if ts else "?"


def _hours(x):
    """One decimal, rounding a decimal half up.

    (57.0 - 54.2) / 1.6 is 1.75 in decimal but 1.7499999... in binary, and
    would print as 1.7. These numbers are checked by hand against the owner's
    own arithmetic, so the decimal answer is the one to show: settle the
    binary noise at six places first, then round the half up.
    """
    return f"{math.floor(round(x, 6) * 10 + 0.5) / 10:.1f}"


def _rule(number, name, fires, detail, proposal=None, **extra):
    r = {"rule": number, "name": name, "fires": bool(fires),
         "detail": detail, "proposal": proposal}
    r.update(extra)
    return r


def _same(a, b):
    return all(abs(a[k] - b[k]) < EPS for k in
               ("mep_start", "mep_stop", "kub_start", "kub_stop"))


def line(r):
    """One rule as a plan-record line."""
    verdict = "FIRES" if r["fires"] else ("held" if r.get("held") else "no")
    return f"POLICY {r['rule']} {r['name']}: {verdict} ({r['detail']})"


def lines(evaluation):
    return [line(r) for r in evaluation]


# --- POLICY 4: solo top-up --------------------------------------------------

def solo_top_up(cfg, f):
    """Peak short of 57.0 and 52 V before sunrise: run one generator to 57.0.

    Every clause is a comparison the model was previously left to make in its
    head, so each one is reported with both numbers whether it passes or not.
    """
    name = "solo top-up"
    peak, v = f.get("peak_today"), f.get("voltage")
    limit = cfg["solo_peak_threshold"]
    proj = f.get("projection") or {}
    sunrise, reached = f.get("sunrise_ts"), proj.get("reached")

    if peak is None or v is None:
        return _rule(4, name, False, "peak voltage or battery voltage unknown")
    if peak >= limit:
        return _rule(4, name, False, f"peak {peak:.1f} ≥ {limit:.1f}")
    parts = [f"peak {peak:.1f} < {limit:.1f}"]

    if not reached:
        return _rule(4, name, False, "; ".join(parts + [
            f"52 V not projected ({proj.get('reason', 'unknown')})"]))
    if not sunrise:
        return _rule(4, name, False, "; ".join(parts + ["next sunrise unknown"]))
    if reached >= sunrise:
        return _rule(4, name, False, "; ".join(parts + [
            f"52 V projected {_clock(reached, cfg)}, not before sunrise "
            f"{_clock(sunrise, cfg)}"]))
    parts.append(f"52 V projected {_clock(reached, cfg)} before sunrise "
                 f"{_clock(sunrise, cfg)}")

    select = cfg["solo_select_voltage"]
    gen = "mep" if v <= select else "kubota"
    label = "MEP" if gen == "mep" else "Kubota"
    parts.append(f"V {v:.1f} {'≤' if v <= select else '>'} {select:.1f} → {label}")

    # POLICY 5: a target is only valid if it is reachable in the run window.
    target = cfg["solo_target"]
    rate = (f.get("charge_rates") or {}).get(gen)
    window = (f.get("run_window_h") or {}).get(gen)
    if not rate or not rate.get("v_per_h"):
        return _rule(4, name, False, "; ".join(parts + [
            f"no observed solo charge rate for {label}, so {target:.1f} cannot "
            f"be shown reachable (POLICY 5)"]))
    needed = (target - v) / rate["v_per_h"]
    if window is not None and needed > window + 1e-9:
        return _rule(4, name, False, "; ".join(parts + [
            f"{target:.1f} needs {_hours(needed)} h at {rate['v_per_h']} V/h but "
            f"the run window is {window:.1f} h (POLICY 5)"]))
    parts.append(f"{target:.1f} reachable in {_hours(needed)} h at "
                 f"{rate['v_per_h']} V/h")

    # The start has to clear the stop by 2.0 V, so it cannot simply be the
    # configured maximum when the target is 57.0.
    start = min(cfg["start_voltage_max"], target - MIN_STOP_MINUS_START)
    if start <= v:
        parts.append(f"the run begins when the pack falls to {start:.1f}")
    other_s, other_p = cfg["default_start"], cfg["default_stop"]
    proposal = ({"mep_start": start, "mep_stop": target,
                 "kub_start": other_s, "kub_stop": other_p} if gen == "mep" else
                {"mep_start": other_s, "mep_stop": other_p,
                 "kub_start": start, "kub_stop": target})
    return _rule(4, name, True, "; ".join(parts), proposal, gen=gen)


# --- POLICY 3: the two stop-voltage cases -----------------------------------

def storm_stop(cfg, f):
    """Heavy cloud tomorrow: carry more charge into it."""
    name = f"storm stop {cfg['stop_voltage_max']:.1f}"
    cloud = f.get("tomorrow_cloud")
    limit = cfg["storm_cloud_pct"]
    target = cfg["stop_voltage_max"]
    th = f.get("thresholds") or {}

    if cloud is None:
        return _rule(3, name, False, "tomorrow's cloud cover unknown")
    if cloud < limit:
        return _rule(3, name, False,
                     f"tomorrow {cloud}% daylight cloud < {limit}%")
    detail = f"tomorrow {cloud}% daylight cloud ≥ {limit}%"
    stops = (th.get("mep_stop"), th.get("kub_stop"))
    if all(s is not None and abs(s - target) < EPS for s in stops):
        return _rule(3, name, False,
                     f"{detail}, but both stops are already {target:.1f}")
    return _rule(3, name, True,
                 f"{detail} → stop {target:.1f} (live stops MEP "
                 f"{stops[0]} / Kubota {stops[1]})",
                 {"mep_start": cfg["default_start"], "mep_stop": target,
                  "kub_start": cfg["default_start"], "kub_stop": target})


def predawn_stop(cfg, f, superseded=False):
    """A run landing shortly before a clear sunrise: let solar finish it."""
    name = f"pre-dawn stop {cfg['stop_voltage_min']:.1f}"
    limit = cfg["clear_cloud_pct"]
    window = cfg["predawn_hours"]
    target = cfg["stop_voltage_min"]
    cloud = f.get("tomorrow_cloud")
    proj = f.get("projection") or {}
    sunrise, reached = f.get("sunrise_ts"), proj.get("reached")
    th = f.get("thresholds") or {}

    if not reached:
        return _rule(3, name, False,
                     f"52 V not projected ({proj.get('reason', 'unknown')})")
    if not sunrise:
        return _rule(3, name, False, "next sunrise unknown")
    lead = (sunrise - reached) / 3600.0
    if lead <= 0:
        return _rule(3, name, False,
                     f"52 V projected {_clock(reached, cfg)}, after sunrise "
                     f"{_clock(sunrise, cfg)}")
    if lead > window:
        return _rule(3, name, False,
                     f"52 V projected {_clock(reached, cfg)}, {_hours(lead)} h "
                     f"before sunrise {_clock(sunrise, cfg)} "
                     f"(window {window:.1f} h)")
    detail = (f"52 V projected {_clock(reached, cfg)}, {_hours(lead)} h before "
              f"sunrise {_clock(sunrise, cfg)} ≤ {window:.1f} h")
    if cloud is None:
        return _rule(3, name, False,
                     f"{detail}, but tomorrow's cloud cover is unknown")
    if cloud > limit:
        return _rule(3, name, False,
                     f"{detail}, but tomorrow {cloud}% daylight cloud > {limit}% "
                     f"is not a clear sunrise")
    detail += f"; tomorrow {cloud}% daylight cloud ≤ {limit}%"
    if superseded:
        return _rule(3, name, False, f"{detail}, but POLICY 4 fires and its "
                     f"{cfg['solo_target']:.1f} target supersedes", held=True)
    stops = (th.get("mep_stop"), th.get("kub_stop"))
    if all(s is not None and abs(s - target) < EPS for s in stops):
        return _rule(3, name, False,
                     f"{detail}, but both stops are already {target:.1f}")
    return _rule(3, name, True, f"{detail} → stop {target:.1f}",
                 {"mep_start": cfg["default_start"], "mep_stop": target,
                  "kub_start": cfg["default_start"], "kub_stop": target})


# --- the whole evaluation ---------------------------------------------------

def evaluate(cfg, facts):
    """Every numeric rule, in rule order, against one tick's facts.

    POLICY 4 is computed first because it settles the pre-dawn clause of
    POLICY 3: both want the stop moved, and a top-up to 57.0 is not served by
    stopping at 54.5.
    """
    four = solo_top_up(cfg, facts)
    return [storm_stop(cfg, facts),
            predawn_stop(cfg, facts, superseded=four["fires"]),
            four]


def firing(evaluation):
    return [r for r in evaluation if r["fires"]]


def overruled(text):
    """Rule numbers the model said, in writing, that it is overruling."""
    return {int(n) for n in OVERRULE_RE.findall(text or "")}


def misses(evaluation, text, write_result):
    """Firing rules the model neither proposed nor overruled.

    A guard refusal is not a miss: the model did its part by proposing the
    values. What counts is whether the rule was addressed at all.
    """
    wr = write_result or {}
    proposed = wr.get("now") or wr.get("would_set")
    seen = overruled(text)
    out = []
    for r in firing(evaluation):
        if r["rule"] in seen:
            continue
        if proposed and r["proposal"] and _same(proposed, r["proposal"]):
            continue
        out.append(r)
    return out
