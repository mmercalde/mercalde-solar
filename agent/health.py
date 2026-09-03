"""How the pack is ageing, from what it has actually done.

Measurement and reporting. Nothing here decides anything: no threshold moves
because of a projected year and no run is scheduled to spare the battery.

Three answers, and each one carries how it was arrived at:

  throughput      what has gone through the pack - amp-hours out, equivalent
                  full cycles, the depth an average day takes it to. Counted,
                  not modelled.
  measured_fade   the pack's own capacity over time, as watt-hours per volt
                  across a fixed mid-band window, one figure per calendar
                  month. This is the only part that could show real fade, and
                  on this history it does not: see `confidence`.
  projection      a parametric NMC model - cycle fade scaled by depth, plus
                  calendar fade scaled by temperature and resting band -
                  with every coefficient named in system.yaml and every
                  assumption returned as a sentence.

Two things this module refuses to do. It does not read the Battery Monitor's
state of charge, which is one shunt integrating against a capacity derived
from the same shunt and cannot check itself. And it does not return a bare
number: every field says what it is in its own name, and the ones that rest
on an assumption say which.

The cells are second-life. Every cycle count is cycles since this pack was
built, and the calendar clock started somewhere nobody here measured.
"""

import collections
import logging
import math
import statistics
import time
from datetime import datetime

import history
import loadmodel
import sun

log = logging.getLogger(__name__)

# The fade series is measured on the day's charge, not the night's discharge.
#
# A night gives up a little of the window at whatever the evening's load
# happens to be, and the pack's voltage under load moves with current as much
# as with charge: the nightly series scattered 68% of its own mean and a line
# through it accounted for 11% of that. A sunny day walks the pack from its
# pre-dawn low all the way to its afternoon peak in one monotone traversal,
# three or four volts of it, driven by something that does not care what the
# house is doing. The same measurement on the same history scatters 35%.
#
# A day has to cross at least this much to count, so that a cloudy day that
# nudged the pack half a volt is not one point of a series.
CHARGE_MIN_SPAN_V = 3.0
# Days with fewer than this in a month leave the month out of the series.
FADE_MIN_DAYS = 3
# Months needed before the measured trend is allowed to stand in for the
# literature calendar coefficient.
FADE_MIN_MONTHS = 6
# And how much of the month-to-month variation the straight line has to
# account for. Below this the slope is describing weather and load, not the
# pack: see measured_fade()'s docstring.
FADE_MIN_R2 = 0.5

BOLTZMANN_EV_PER_K = 8.617333e-5


def _f_to_k(f):
    return (f - 32.0) * 5.0 / 9.0 + 273.15


def _linear_fit(xs, ys):
    """(slope, intercept, r_squared) or None when there is nothing to fit."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = (1 - ss_res / ss_tot) if ss_tot else 0.0
    return slope, intercept, r2


# --- what has gone through it ---------------------------------------------------

def throughput(conn, cfg, now=None):
    """Amp-hours out, cycles, and the depth an average day reaches.

    Counted from the hourly battery discharge counter over its own voltage,
    which is amp-hours out of the pack rather than watt-hours at a socket.
    Nothing here is modelled and nothing here is a state of charge.

    `mean_resting_v` is over the clean overnight discharge hours - no
    generator, no solar, voltage falling - which is the nearest thing this
    system records to a pack at rest, and is what the calendar model reads.
    """
    now = int(now or time.time())
    b = cfg.get("battery") or {}
    nominal_ah = b.get("nominal_ah")
    rows = conn.execute(
        "SELECT hour_ts, wh_out, mean_v FROM hourly WHERE device='battery' "
        "AND wh_out IS NOT NULL AND mean_v > 0 ORDER BY hour_ts").fetchall()
    if not rows:
        return {"total_ah_out": None,
                "basis": "no hourly battery discharge rows yet"}

    by_day = collections.defaultdict(float)
    total_ah = total_wh = 0.0
    for r in rows:
        ah = r["wh_out"] / r["mean_v"]
        total_ah += ah
        total_wh += r["wh_out"]
        by_day[history.local_day(r["hour_ts"], cfg)] += ah

    span_days = (rows[-1]["hour_ts"] - rows[0]["hour_ts"]) / 86400.0
    span_years = span_days / 365.25
    # The first and last day are partial by construction, so they would drag
    # a mean daily figure down for a reason that has nothing to do with use.
    whole = sorted(by_day)[1:-1]
    mean_daily_ah = (sum(by_day[d] for d in whole) / len(whole)) if whole else None

    efc = (total_ah / nominal_ah) if nominal_ah else None
    out = {
        "first_day": sorted(by_day)[0], "last_day": sorted(by_day)[-1],
        "span_days": round(span_days, 1),
        "total_ah_out": round(total_ah),
        "total_kwh_out": round(total_wh / 1000.0, 1),
        "nominal_ah": nominal_ah,
        "equivalent_full_cycles": round(efc, 1) if efc is not None else None,
        "cycles_per_year": (round(efc / span_years, 1)
                            if efc is not None and span_years > 0 else None),
        "mean_daily_ah_out": round(mean_daily_ah) if mean_daily_ah else None,
        "mean_daily_dod_pct": (round(mean_daily_ah / nominal_ah * 100, 1)
                               if mean_daily_ah and nominal_ah else None),
        "days_counted": len(whole),
        "mean_resting_v": _mean_resting_v(conn, cfg, now),
        "basis": ("amp-hours are the hourly battery discharge counter over "
                  "that hour's mean voltage; cycles are those amp-hours over "
                  "nominal_ah from system.yaml. The cells are second-life, so "
                  "this counts cycles since the pack was built and not the "
                  "cycles the cells have had."),
    }
    return out


def _mean_resting_v(conn, cfg, now):
    """Mean pack voltage over the clean overnight discharge hours."""
    model = loadmodel.LoadModel(conn, cfg)
    nights = model._discharge_nights(now)
    volts = weight = 0.0
    for rec in nights.values():
        for b, (_wh, v) in rec["bins"].items():
            volts += (b * loadmodel.ENERGY_BIN_V + loadmodel.ENERGY_BIN_V / 2) * v
            weight += v
    return round(volts / weight, 2) if weight else None


# --- has it actually faded ------------------------------------------------------

def charge_segments(conn, cfg, now=None):
    """One (span_v, net_wh, net_ah) per day the sun walked the pack up.

    From the pre-solar minimum voltage to the day's peak, inside the sunrise
    to sunset window, over hours no generator was producing in - so the
    energy is the sun's and the traversal is one monotone climb rather than a
    day's worth of comings and goings. Net of what the house drew at the same
    time, because that came out of the pack while the panels were putting it
    in.
    """
    now = int(now or time.time())
    rows = conn.execute(
        "SELECT b.hour_ts, b.min_v, b.max_v, b.mean_v, b.wh_in, b.wh_out, "
        "       COALESCE(g.wh_in, 0) AS gen_wh "
        "FROM hourly b "
        "LEFT JOIN hourly g ON g.hour_ts = b.hour_ts AND g.device = 'gen' "
        "WHERE b.device = 'battery' AND b.min_v IS NOT NULL "
        "AND b.max_v IS NOT NULL AND b.hour_ts <= ? "
        "ORDER BY b.hour_ts", (now,)).fetchall()
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[history.local_day(r["hour_ts"], cfg)].append(r)

    out = {}
    for day, hours in by_day.items():
        times = sun.times(cfg, day)
        if not times:
            continue
        sunrise, sunset = times
        lit = [r for r in hours if sunrise <= r["hour_ts"] <= sunset]
        if not lit:
            continue
        peak = max(lit, key=lambda r: r["max_v"])
        before = [r for r in hours if r["hour_ts"] <= peak["hour_ts"]]
        low = min(before, key=lambda r: r["min_v"])
        span = peak["max_v"] - low["min_v"]
        if span < CHARGE_MIN_SPAN_V:
            continue
        window = [r for r in hours
                  if low["hour_ts"] <= r["hour_ts"] <= peak["hour_ts"]]
        if any(r["gen_wh"] > 0 for r in window):
            continue
        net_wh = sum((r["wh_in"] or 0) - (r["wh_out"] or 0) for r in window)
        net_ah = sum(((r["wh_in"] or 0) - (r["wh_out"] or 0)) / r["mean_v"]
                     for r in window if r["mean_v"])
        if net_wh <= 0:
            continue
        out[day] = {"span_v": round(span, 2), "net_wh": round(net_wh),
                    "net_ah": round(net_ah, 1),
                    "from_v": low["min_v"], "to_v": peak["max_v"],
                    "wh_per_v": net_wh / span, "ah_per_v": net_ah / span}
    return out


def measured_fade(conn, cfg, now=None):
    """The pack's own capacity over time, from the day's charge.

    If the pack holds less than it did, then walking it up the same stretch
    of its curve takes fewer watt-hours. Each clear day gives one such walk -
    pre-dawn low to afternoon peak, three volts or more of it - and the
    month's figure is the median of its days. It owes nothing to a state of
    charge.

    What comes back is honest about what it can and cannot see. On this
    history the months scatter 35% of their own mean, a straight line through
    them accounts for 13%, and the fitted slope is *positive*, which a pack
    cannot do. Look at the months themselves and the reason is plain: they
    are seasonal, low in early summer and high in autumn, and 2026 sits on
    top of 2025 rather than below it. `year_over_year` compares the same
    month a year apart, which is the only way to read a series with a season
    in it, and it does not show fade either. Any real fade is smaller than
    this method can resolve, and `usable_for_projection` keeps the slope away
    from the model until that changes.
    """
    now = int(now or time.time())
    segments = charge_segments(conn, cfg, now=now)
    per_month = collections.defaultdict(list)
    for day, seg in segments.items():
        per_month[day[:7]].append(seg)

    series = []
    for month in sorted(per_month):
        segs = per_month[month]
        if len(segs) < FADE_MIN_DAYS:
            continue
        series.append({
            "month": month, "days": len(segs),
            "wh_per_v": round(statistics.median(s["wh_per_v"] for s in segs)),
            "ah_per_v": round(statistics.median(s["ah_per_v"] for s in segs), 1),
            "median_span_v": round(statistics.median(s["span_v"] for s in segs), 2),
        })

    out = {"metric": "net watt-hours and amp-hours per volt of a day's solar "
                     "charge, pre-solar minimum to daily peak",
           "min_span_v": CHARGE_MIN_SPAN_V,
           "min_days_per_month": FADE_MIN_DAYS,
           "days_measured": len(segments),
           "months": len(series), "monthly": series,
           "trend_pct_per_year": None, "r_squared": None,
           "scatter_pct_of_mean": None,
           "year_over_year": _year_over_year(series),
           "usable_for_projection": False}
    if len(series) < 2:
        out["confidence"] = (f"{len(series)} month(s) of data; a trend needs "
                             f"at least two and is only trusted from "
                             f"{FADE_MIN_MONTHS}")
        return out

    ys = [s["wh_per_v"] for s in series]
    xs = list(range(len(ys)))
    fit = _linear_fit(xs, ys)
    mean_y = sum(ys) / len(ys)
    out["scatter_pct_of_mean"] = round((max(ys) - min(ys)) / mean_y * 100, 1)
    if fit and mean_y:
        slope, _intercept, r2 = fit
        out["trend_pct_per_year"] = round(slope * 12 / mean_y * 100, 1)
        out["r_squared"] = round(r2, 2)

    months, r2 = out["months"], out["r_squared"] or 0.0
    trend = out["trend_pct_per_year"]
    if months < FADE_MIN_MONTHS:
        out["confidence"] = (f"{months} months, fewer than the "
                             f"{FADE_MIN_MONTHS} this is trusted from")
    elif r2 < FADE_MIN_R2:
        yoy = out["year_over_year"]
        extra = ""
        if yoy and yoy.get("mean_pct") is not None:
            extra = (f" The same months a year apart differ by "
                     f"{yoy['mean_pct']:+.0f}% on average across "
                     f"{yoy['pairs']} pair(s), which does not show fade "
                     f"either.")
        out["confidence"] = (
            f"{months} months, but the straight line accounts for only "
            f"{r2 * 100:.0f}% of the variation and the months are spread "
            f"{out['scatter_pct_of_mean']:.0f}% of the mean. The shape of it "
            f"is seasonal, not a decline.{extra} No fade can be measured "
            f"from this yet; any that is there is smaller than the method "
            f"can resolve.")
    elif trend is not None and trend > 0:
        out["confidence"] = (
            f"{months} months and the line fits, but the slope is positive: "
            f"a pack gaining capacity is not a thing, so something else is "
            f"moving the series and it is not used.")
    else:
        out["usable_for_projection"] = True
        out["confidence"] = (f"{months} months, the line accounts for "
                             f"{r2 * 100:.0f}% of the variation")
    return out


# --- and how long has it got -----------------------------------------------------

def _year_over_year(series):
    """The same month a year apart, which is how a seasonal series is read.

    A straight line through twelve months of a signal that rises and falls
    with the season measures the season. Comparing August with August does
    not.
    """
    by_month = {s["month"]: s["wh_per_v"] for s in series}
    pairs = []
    for month, value in sorted(by_month.items()):
        year, mm = month.split("-")
        earlier = by_month.get(f"{int(year) - 1}-{mm}")
        if earlier:
            pairs.append({"month": mm, "from": earlier, "to": value,
                          "change_pct": round((value - earlier) / earlier * 100, 1)})
    if not pairs:
        return {"pairs": 0, "mean_pct": None,
                "note": "no month has a counterpart a year earlier yet"}
    return {"pairs": len(pairs), "comparisons": pairs,
            "mean_pct": round(sum(p["change_pct"] for p in pairs) / len(pairs), 1)}


def _resting_band_pct(conn, cfg, resting_v, now):
    """Where the pack rests, as a percentage of its measured 52-59.5 V span.

    Not a state of charge. This pack has no state-of-charge scale worth
    using, so the calendar model is expressed against the one span that was
    actually measured: the 40.9 kWh the pack gave up between 59.5 and 52.0 V.
    The learned Wh-vs-V curve says how much of that sits below the resting
    voltage, and that fraction is what stands in for "how full it sits".
    """
    b = cfg.get("battery") or {}
    span_kwh = b.get("measured_kwh_59_5_to_52_0")
    if not span_kwh or resting_v is None:
        return None
    model = loadmodel.LoadModel(conn, cfg)
    below = model.energy_above(b.get("floor_v", 52.0), resting_v, now=now)
    if below.get("wh") is None:
        return None
    return round(below["wh"] / 1000.0 / span_kwh * 100, 1)


def projection(conn, cfg, tp=None, fade=None, now=None):
    """Years to 80% capacity, by cycling and by calendar, and which bites.

    A parametric model, not a measurement. Cycle fade is charged per
    equivalent full cycle and scaled by the depth those cycles actually
    reach; calendar fade starts from a reference rate and is moved by
    temperature, on Arrhenius, and by how full the pack sits.

    Every coefficient is in system.yaml under battery.aging with the
    reasoning beside it, and every choice this function makes comes back in
    `assumptions` as a sentence, because a number of years is exactly the
    kind of output that gets quoted without its basis.
    """
    now = int(now or time.time())
    b = cfg.get("battery") or {}
    a = b.get("aging") or {}
    tp = tp if tp is not None else throughput(conn, cfg, now=now)
    fade = fade if fade is not None else measured_fade(conn, cfg, now=now)
    eol = a.get("end_of_life_capacity_pct", 80)
    to_lose_pct = 100.0 - eol
    notes = []

    # --- cycling ---
    cycles_yr = tp.get("cycles_per_year")
    dod = tp.get("mean_daily_dod_pct")
    cycle_pct_yr = None
    if cycles_yr and dod and a.get("cycle_fade_pct_per_efc_at_full_dod"):
        # Fade per cycle goes as DoD^n, and an equivalent full cycle taken in
        # shallow bites contains 1/DoD of them, so fade per EFC goes as
        # DoD^(n-1). With n above 1 that is below 1 for shallow cycling: the
        # gentler-per-unit-of-throughput result the cycle-life literature
        # reports. The factor is named in the assumptions because getting it
        # upside down is easy and invisible in the output.
        n = a.get("cycle_dod_exponent", 1.0)
        depth = dod / 100.0
        depth_factor = depth ** (n - 1.0)
        per_efc = a["cycle_fade_pct_per_efc_at_full_dod"] * depth_factor
        cycle_pct_yr = cycles_yr * per_efc
        direction = ("less" if depth_factor < 1 else
                     "more" if depth_factor > 1 else "the same")
        notes.append(
            f"cycle fade: {cycles_yr:.0f} equivalent full cycles a year at a "
            f"mean depth of {dod:.0f}%, charged at "
            f"{a['cycle_fade_pct_per_efc_at_full_dod']}%/EFC at full depth. "
            f"Shallow cycling costs {direction} per equivalent full cycle: "
            f"depth^(n-1) with n={n} gives a factor of {depth_factor:.2f}, so "
            f"each EFC costs {per_efc:.4f}% rather than "
            f"{a['cycle_fade_pct_per_efc_at_full_dod']}%")

    # --- calendar ---
    resting_v = tp.get("mean_resting_v")
    band = _resting_band_pct(conn, cfg, resting_v, now)
    cal_pct_yr = a.get("calendar_fade_pct_per_year_ref")
    if cal_pct_yr is not None:
        ref_f = a.get("calendar_ref_temp_f", 77)
        amb_f = b.get("ambient_f")
        if amb_f is not None and a.get("calendar_activation_ev"):
            t_ref, t_amb = _f_to_k(ref_f), _f_to_k(amb_f)
            arr = math.exp(a["calendar_activation_ev"] / BOLTZMANN_EV_PER_K
                           * (1.0 / t_ref - 1.0 / t_amb))
            cal_pct_yr *= arr
            notes.append(
                f"calendar fade: {a['calendar_fade_pct_per_year_ref']}%/yr at "
                f"{ref_f} F, multiplied by {arr:.2f} for {amb_f} F on "
                f"Arrhenius at {a['calendar_activation_ev']} eV"
                + (" (the room is kept, so this is a year-round figure and "
                   "not a summer one)" if b.get("climate_controlled") else ""))
        if band is not None and a.get("calendar_band_sensitivity_per_pct"):
            ref_band = a.get("calendar_band_reference_pct", 50)
            factor = max(0.2, 1 + a["calendar_band_sensitivity_per_pct"]
                         * (band - ref_band))
            cal_pct_yr *= factor
            notes.append(
                f"the pack rests at {resting_v} V, {band:.0f}% of the way up "
                f"the measured 52.0-59.5 V span, and sitting {ref_band - band:.0f} "
                f"points below the {ref_band}% reference multiplies calendar "
                f"fade by {factor:.2f}")
        else:
            notes.append("resting band unknown, so calendar fade carries no "
                         "state-of-charge adjustment")

    # The measured series stands in for the literature coefficient only when
    # it has the months AND a line worth believing. It does not, yet.
    if fade.get("usable_for_projection") and fade.get("trend_pct_per_year"):
        cal_pct_yr = abs(fade["trend_pct_per_year"])
        notes.append(f"calendar coefficient replaced by the measured trend, "
                     f"{fade['trend_pct_per_year']}%/yr over "
                     f"{fade['months']} months")
    else:
        notes.append("the measured fade series is not used: "
                     + str(fade.get("confidence", "not computed")))

    years_cycle = (to_lose_pct / cycle_pct_yr) if cycle_pct_yr else None
    years_cal = (to_lose_pct / cal_pct_yr) if cal_pct_yr else None
    dominant = None
    if years_cycle is not None and years_cal is not None:
        dominant = "cycling" if years_cycle < years_cal else "calendar"
    elif years_cycle is not None:
        dominant = "cycling"
    elif years_cal is not None:
        dominant = "calendar"

    combined_pct_yr = None
    if cycle_pct_yr is not None or cal_pct_yr is not None:
        combined_pct_yr = (cycle_pct_yr or 0.0) + (cal_pct_yr or 0.0)
    years_combined = ((to_lose_pct / combined_pct_yr)
                      if combined_pct_yr else None)

    notes.append(f"end of life is taken as {eol}% of present capacity, and "
                 f"'present' is today's pack, not a new one: these cells are "
                 f"second-life and had a history before this pack existed")
    notes.append("the combined figure adds the two mechanisms, which is the "
                 "pessimistic reading: they overlap - both consume the same "
                 "lithium inventory - so adding them overstates the rate and "
                 "the combined years are a floor rather than a best guess. "
                 "The two components are given separately for that reason")

    return {
        "combined_fade_pct_per_year": (round(combined_pct_yr, 2)
                                       if combined_pct_yr else None),
        "years_to_80pct_combined": (round(years_combined, 1)
                                    if years_combined else None),
        "cycle_fade_pct_per_year": (round(cycle_pct_yr, 2)
                                    if cycle_pct_yr else None),
        "calendar_fade_pct_per_year": (round(cal_pct_yr, 2)
                                       if cal_pct_yr else None),
        "years_to_80pct_cycle": (round(years_cycle, 1)
                                 if years_cycle else None),
        "years_to_80pct_calendar": (round(years_cal, 1) if years_cal else None),
        "dominant_mechanism": dominant,
        "resting_band_pct_of_measured_span": band,
        "end_of_life_capacity_pct": eol,
        "assumptions": notes,
    }


# --- the whole answer --------------------------------------------------------------

def battery_health(conn, cfg, now=None):
    """Everything the tool returns, each part carrying its own basis."""
    now = int(now or time.time())
    b = cfg.get("battery") or {}
    tp = throughput(conn, cfg, now=now)
    fade = measured_fade(conn, cfg, now=now)
    return {
        "as_of": history.stamp(now, cfg),
        "pack": {
            "chemistry": b.get("chemistry_short") or b.get("chemistry"),
            "configuration": b.get("configuration"),
            "second_life": b.get("second_life"),
            "nominal_ah": b.get("nominal_ah"),
            "measured_kwh_59_5_to_52_0": b.get("measured_kwh_59_5_to_52_0"),
            "est_total_kwh": b.get("est_total_kwh"),
            "est_total_kwh_note": "extrapolated from the measured span, and "
                                  "much the softer of the two figures",
            "ambient_f": b.get("ambient_f"),
            "climate_controlled": b.get("climate_controlled"),
            "held_between_v": [b.get("floor_v"), b.get("ceiling_v")],
        },
        "throughput": tp,
        "measured_fade": fade,
        "projection": projection(conn, cfg, tp=tp, fade=fade, now=now),
        "state_of_charge_note": (
            "No state of charge is reported anywhere in this tool: the "
            "Battery Monitor's scale is unreliable. Its figure is one shunt "
            "integrating against a "
            "capacity derived from the same shunt, so it cannot check "
            "itself, and it has read 100% at 56.2 V and 98% at 55.6 V. What "
            "the pack holds is answered in watt-hours between two voltages."),
        "note": ("Every figure here is either counted or modelled and says "
                 "which. Nothing in it moves a threshold or chooses a "
                 "generator."),
    }
