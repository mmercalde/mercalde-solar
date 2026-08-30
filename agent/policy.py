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


def window_opens(cfg, f):
    """When today's top-up window opens, from `topup_earliest`.

    "sunset", "sunset-30" for half an hour before it, or a clock time like
    "20:00". The point of the window is that the day's solar goes into the
    pack first: a top-up is decided once production is finished and the
    shortfall is known, not at half past nine in the morning with eight hours
    of sun still to come.
    """
    spec = str(cfg.get("topup_earliest") or "sunset").strip().lower()
    if ":" in spec:
        try:
            hour, minute = (int(x) for x in spec.split(":", 1))
        except ValueError:
            return f.get("sunset_ts")
        now = f.get("now")
        if not now:
            return None
        return int(history.local(now, cfg).replace(
            hour=hour, minute=minute, second=0, microsecond=0).timestamp())
    base, _, offset = spec.partition("-")
    if base.strip() != "sunset":
        return None
    ts = f.get("sunset_ts")
    if not ts:
        return None
    try:
        return ts - int(offset) * 60 if offset else ts
    except ValueError:
        return ts


def _held_for_daylight(cfg, f):
    """The reason a top-up is held, or None if the window is open.

    The window runs from sunset to midnight. Before it the sun is still
    filling the pack; after midnight the evening's decision has been made.
    """
    now, opens = f.get("now"), window_opens(cfg, f)
    if not now or not opens:
        return None
    if now >= opens:
        return None
    left = f.get("remaining_solar_wh")
    solar = (f"{left / 1000.0:.1f} kWh" if left is not None
             else "not learned yet")
    return (f"held until {_clock(opens, cfg)}; remaining solar today {solar}")


def _clock(ts, cfg):
    return history.clock(ts, cfg) if ts else "?"


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


def call_to_action(rules):
    """What a firing rule asks for, in the values it wants written."""
    out = []
    for r in rules:
        p = r["proposal"]
        out.append(f"POLICY {r['rule']} {r['name']} FIRES" + (
            f" → set MEP {p['mep_start']:.1f}/{p['mep_stop']:.1f}, "
            f"Kubota {p['kub_start']:.1f}/{p['kub_stop']:.1f}" if p else ""))
    return out


# --- POLICY 4: solo top-up --------------------------------------------------

# How far above the pack's voltage a start is set so the Pi5 acts on it at
# once. Thresholds are written to one decimal, and the reading wanders a
# little, so a tenth would be a coin toss.
START_ABOVE_PACK_V = 0.2


def _proposal(cfg, gens, target, start, baseline):
    """The chosen generators raised, the rest left at the owner's baseline."""
    values = dict(baseline)
    for gen in gens:
        skey, pkey = ("mep_start", "mep_stop") if gen == "mep" else \
                     ("kub_start", "kub_stop")
        values[skey], values[pkey] = start, target
    return values


def _bands(cfg):
    """[(name, generators, ceiling Wh)] smallest first, ending open-ended."""
    conf = cfg.get("topup_bands") or {}
    kub, mep = conf.get("kubota"), conf.get("mep")
    return [("Kubota", ("kubota",), kub),
            ("MEP", ("mep",), mep),
            ("both", ("mep", "kubota"), None)]


def _band_for(cfg, deficit_wh):
    """The band the deficit lands in, and the ones above it to step through."""
    bands = _bands(cfg)
    for i, (name, gens, ceiling) in enumerate(bands):
        if ceiling is None or deficit_wh <= ceiling:
            return i, bands
    return len(bands) - 1, bands


def _window_for(f, gens):
    windows = f.get("run_window_h") or {}
    chosen = [windows[g] for g in gens if g in windows]
    return min(chosen) if chosen else None


def solo_top_up(cfg, f, model):
    """POLICY 4, the top-up. What the night is short of, and what will cover it.

    Not "did today's peak reach 57" but "how many watt-hours is the pack short
    of sunrise", which is the question the run is actually for. The shortfall
    sets the stop voltage and picks the generators; 57.0 is now the ceiling of
    that calculation rather than its purpose.
    """
    name = "top-up"
    held = _held_for_daylight(cfg, f)
    if held:
        return _rule(4, name, False, held, held=True)

    v, soc = f.get("voltage"), f.get("soc")
    baseline = f.get("baseline") or {}
    d = f.get("deficit") or {}
    if v is None or not baseline:
        return _rule(4, name, False, "battery voltage or baseline unknown")
    if d.get("deficit_wh") is None:
        return _rule(4, name, False,
                     f"the deficit is not known ({d.get('reason', 'unknown')})")

    deficit = d["deficit_wh"]
    floor_v = d.get("floor_v", 52.0)
    if deficit <= 0:
        return _rule(4, name, False,
                     f"the pack holds {abs(deficit):,} Wh more than the night "
                     f"needs above {floor_v:.1f} V, so nothing is short")
    minimum = cfg["min_topup_wh"]
    if deficit < minimum:
        return _rule(4, name, False,
                     f"deficit {deficit:,} Wh is under the {minimum:,} Wh a run "
                     f"is worth; POLICY 3's pre-dawn stop covers a night this "
                     f"close")

    margin = cfg["topup_margin_pct"]
    parts = [f"deficit {deficit:,} Wh to sunrise above {floor_v:.1f} V "
             f"(needs {d.get('needed_wh', 0):,}, holds {d.get('available_wh', 0):,})"]

    # The run has to begin now, so the start goes above the pack - and the
    # stop has to clear that start by the separation the guard requires,
    # whatever the deficit alone would have asked for.
    start = round(min(cfg["start_voltage_max"],
                      max(cfg["start_voltage_min"],
                          round(v + START_ABOVE_PACK_V, 1))), 1)
    ceiling = cfg["solo_target"]
    if start <= v + EPS:
        return _rule(4, name, False, "; ".join(parts + [
            f"but a start above {v:.1f} V would be over the "
            f"{cfg['start_voltage_max']:.1f} V limit, so no run can be started "
            f"now"]))
    least_stop = round(start + MIN_STOP_MINUS_START, 1)
    if least_stop > ceiling + EPS:
        return _rule(4, name, False, "; ".join(parts + [
            f"but a start above {v:.1f} V would need a stop of "
            f"{least_stop:.1f}, over the {ceiling:.1f} ceiling, so no run can "
            f"be started now"]))

    index, bands = _band_for(cfg, deficit)
    tried = []
    while index < len(bands):
        label, gens, band_max = bands[index]
        gen = None if len(gens) > 1 else gens[0]
        solo = len(gens) == 1
        want = model.topup_target(deficit, margin, soc, d.get("capacity_wh"),
                                  low=cfg["solo_target_floor"], high=ceiling,
                                  gen=gen, solo=solo)
        if want is None:
            return _rule(4, name, False, "; ".join(parts + [
                "no curve reaches the state of charge the deficit asks for"]))
        target = max(want["volts"], least_stop)
        note = [f"+{margin}% is {want['padded_wh']:,} Wh → stop "
                f"{want['volts']:.1f} ({want['basis']})"]
        if target > want["volts"] + EPS:
            note.append(f"raised to {target:.1f} to clear a start above "
                        f"{v:.1f} V by {MIN_STOP_MINUS_START:.1f} V")
        reach = model.reach(gen, v, target, _window_for(f, gens), solo=solo,
                            soc_now=soc)
        band = (f"{label} band (deficit ≤ {band_max:,} Wh)" if band_max
                else f"{label} band (above every other)")
        if reach["ok"]:
            parts += note
            if tried:
                parts.append("stepped up past " + "; ".join(tried))
            parts.append(f"{band}: {reach['why']}")

            parts.append(f"start {start:.1f} is above the pack's {v:.1f} V, so "
                         f"the run begins now")
            return _rule(4, name, True, "; ".join(parts),
                         _proposal(cfg, gens, target, start, baseline),
                         gen="+".join(gens), target=target, mode=label.lower(),
                         deficit_wh=deficit, start=start)
        tried.append(f"{band} {reach['why']}")
        index += 1

    # Every band falls short of the target. Running both for as long as they
    # are allowed still beats letting the pack fall through the floor, so the
    # top band takes the most it can reach.
    label, gens, _ = bands[-1]
    parts.append("no band reaches it: " + "; ".join(tried))
    lower = model.best_reachable_target(None, v, _window_for(f, gens),
                                        ceiling=ceiling,
                                        floor=max(cfg["solo_target_floor"],
                                                  least_stop),
                                        soc_now=soc, solo=False)
    if lower is None:
        return _rule(4, name, False, "; ".join(parts + [
            f"and both together cannot reach {least_stop:.1f}"]))
    parts.append(f"both together to {lower:.1f}, the most they can reach")
    parts.append(f"start {start:.1f} is above the pack's {v:.1f} V, so the run "
                 f"begins now")
    return _rule(4, name, True, "; ".join(parts),
                 _proposal(cfg, gens, lower, start, baseline),
                 gen="+".join(gens), target=lower, mode="both",
                 deficit_wh=deficit, start=start)


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
    proposal = {"mep_start": cfg["default_start"], "mep_stop": target,
                "kub_start": cfg["default_start"], "kub_stop": target}
    # Raising a stop does not start anything, so it is never held. A
    # pre-charge that raises a start would run a generator, and that waits
    # for the same window POLICY 4 waits for.
    baseline = f.get("baseline") or {}
    raises_a_start = any(
        proposal[k] > (baseline.get(k, cfg["default_start"])) + EPS
        for k in ("mep_start", "kub_start"))
    if raises_a_start:
        held = _held_for_daylight(cfg, f)
        if held:
            return _rule(3, name, False, f"{detail}, but the pre-charge start "
                         f"raise is {held}", held=True)
    return _rule(3, name, True,
                 f"{detail} → stop {target:.1f} (live stops MEP "
                 f"{stops[0]} / Kubota {stops[1]})", proposal)


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

def evaluate(cfg, facts, model):
    """Every numeric rule, in rule order, against one tick's facts.

    `model` is the LoadModel: the rules decide, it does the physics, and the
    guard asks it the same questions so the two cannot disagree.

    POLICY 4 is computed first because it settles the pre-dawn clause of
    POLICY 3: both want the stop moved, and a top-up to 57.0 is not served by
    stopping at 54.5.
    """
    four = solo_top_up(cfg, facts, model)
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
