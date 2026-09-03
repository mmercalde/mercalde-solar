"""Every calendar month of the record, and the months that stand out.

Measurement and reporting. Nothing here decides anything.

The point of it is the superlatives. Asked which month was the worst for
solar, a model handed a series of seventeen months will rank them itself, and
ranking seventeen rows is exactly the sort of arithmetic it does confidently
and wrongly. So the ranking is done here, in Python, and comes back as named
fields the answer can be read straight out of.

Generator activity comes from two sources that tile rather than overlap, and
saying which is which is most of the work:

  `gen_runs` is derived from the live `*Action` samples, so it starts when
  the agent did - 2026-08-28 - and is precise: real start and stop times, and
  a modelled gallon figure per run. `daily.mep_minutes` is computed from the
  same table by rollup_daily and inherits exactly the same gap, which is why
  it is not the complete history it looks like.

  The scraped `gen` energy counter - /SYS/GEN/ENERGY_HOUR, in `hourly` -
  runs from 2025-05-02 to 2026-08-27 and is the only record of what the
  generators did before the agent existed. It is metered energy rather than
  run times, it does not say which engine produced it, and its hours are
  whole-hour buckets, so a twenty-minute run reads as an hour.

Reported without a source, December 2025 was 554 kWh in deficit with zero
generator hours, which cannot happen. It was 673 kWh of generator energy over
141 hours, and the balance below agrees.

Two filters, both because a partial period is not a bad one:

  A day is only eligible to be the best or worst solar day if it has close to
  a full day of hourly rows behind it. Without that the worst solar day on
  this record is 2026-08-28 at 0.0 kWh - the afternoon live sampling started,
  six hours of it - rather than 2025-11-15 at 2.7, which was actually a dark
  November day.

  A month is only eligible for the superlatives if it has most of its days.
  September 2026 is two days old and would otherwise be the worst month for
  solar in the record by a factor of eight.

Both exclusions are counted and returned rather than done quietly.
"""

import collections
import logging
import time

import fuel
import history

log = logging.getLogger(__name__)

# Hourly rows a day needs before it can be the best or the worst.
MIN_HOURS_FOR_DAY_RANKING = 20
# Days a month needs before it can be a superlative.
MIN_DAYS_FOR_MONTH_RANKING = 20


def _hours_per_day(conn, cfg):
    """{day: hourly rows behind it}, to tell a dark day from a short one."""
    per_day = collections.Counter()
    for r in conn.execute("SELECT hour_ts FROM hourly WHERE device='solar'"):
        per_day[history.local_day(r["hour_ts"], cfg)] += 1
    return per_day


def kwh_per_gallon(cfg):
    """{gen: kWh delivered per gallon at full load}, and the mean of them.

    From each generator's own curve: rated kilowatts over gallons an hour at
    full load. The two land within 8% of each other - the Kubota at 10.9, the
    MEP at 10.1 - which is what makes it possible to price a month of metered
    generator energy without knowing which engine produced it.
    """
    out = {}
    for gen in history.GENS:
        model = fuel.model_for(cfg, gen)
        if not model:
            continue
        gph = fuel.gal_per_hour(model["curve"], 1.0)
        if gph:
            out[gen] = model["rated_w"] / 1000.0 / gph
    mean = (sum(out.values()) / len(out)) if out else None
    return out, mean


def _metered_by_month(conn, cfg):
    """{month: {kwh, hours}} of generator energy, from the scraped counter.

    The complete history, and the only one there is before the agent. Hours
    are whole-hour buckets in which the generator produced anything at all,
    so they are an upper bound on running time rather than a measurement of
    it.
    """
    out = collections.defaultdict(lambda: {"kwh": 0.0, "hours": 0})
    for r in conn.execute("SELECT hour_ts, wh_in FROM hourly "
                          "WHERE device='gen' AND wh_in > 0"):
        cell = out[history.local_day(r["hour_ts"], cfg)[:7]]
        cell["kwh"] += r["wh_in"] / 1000.0
        cell["hours"] += 1
    return out


def _balance_by_month(conn, cfg):
    """{month: net battery kWh}, for the energy-balance check."""
    out = collections.defaultdict(float)
    for r in conn.execute("SELECT hour_ts, wh_in, wh_out FROM hourly "
                          "WHERE device='battery'"):
        out[history.local_day(r["hour_ts"], cfg)[:7]] += (
            (r["wh_in"] or 0) - (r["wh_out"] or 0)) / 1000.0
    return out


def _runs_by_month(conn, cfg):
    """{month: {gen: {hours, gal, runs, unpriced}}} from gen_runs.

    Hours and gallons come from the same rows so the two agree with each
    other, and a run is counted in the month it began. Exercise runs are left
    out, as they are everywhere else: thirty minutes at nine in the morning
    is not the agent's and is not a signal.
    """
    out = collections.defaultdict(
        lambda: {g: {"hours": 0.0, "gal": 0.0, "runs": 0, "unpriced": 0}
                 for g in history.GENS})
    for r in conn.execute(
            "SELECT gen, start_ts, duration_min, fuel_gal FROM gen_runs "
            "WHERE kind != 'exercise'"):
        month = history.local_day(r["start_ts"], cfg)[:7]
        cell = out[month][r["gen"]]
        cell["runs"] += 1
        cell["hours"] += (r["duration_min"] or 0) / 60.0
        if r["fuel_gal"] is None:
            cell["unpriced"] += 1
        else:
            cell["gal"] += r["fuel_gal"]
    return out


# What a tool result may cost. get_monthly_summary's full form ran to 17,630
# characters over seventeen months - about 4,400 tokens - and on the KAMRUI's
# integrated GPU an answer carrying it went past the 180 s model timeout and
# the owner got "the agent is not answering". The compact form is the default
# now, and the fix is the payload rather than the timeout: a question about
# which month was worst needs one field, not seventeen rows of prose.
COMPACT_MONTHS = 12


def monthly_summary(conn, cfg, months=COMPACT_MONTHS, detail=False, now=None):
    """The months that stand out, and a table of the recent ones.

    `detail` returns everything about every month - the day names, the
    per-generator dicts, the per-month provenance sentences - and is for a
    question about one particular month. The default is a compact table of
    the last `months` and the superlatives, which is what a question about
    which month was best or worst actually needs.

    The superlatives always rank over the whole record however short the
    table is. Ranking only what was printed would make the answer depend on
    how much of it was asked for.
    """
    now = int(now or time.time())
    days = conn.execute(
        "SELECT day, solar_wh, load_wh, peak_v, min_v FROM daily "
        "ORDER BY day").fetchall()
    if not days:
        return {"months": [], "superlatives": {},
                "note": "no daily rollup yet; nothing to summarise"}

    months_wanted = max(1, int(months or COMPACT_MONTHS))
    hours = _hours_per_day(conn, cfg)
    runs = _runs_by_month(conn, cfg)
    metered = _metered_by_month(conn, cfg)
    battery = _balance_by_month(conn, cfg)
    _per_gal, mean_gal = kwh_per_gallon(cfg)
    full_load_gph = [fuel.gal_per_hour(fuel.model_for(cfg, g)["curve"], 1.0)
                     for g in history.GENS if fuel.model_for(cfg, g)]
    gph_mean = sum(full_load_gph) / len(full_load_gph) if full_load_gph else None
    by_month = collections.defaultdict(list)
    for d in days:
        by_month[d["day"][:7]].append(d)

    partial_days = []
    months = []
    for month in sorted(by_month):
        rows = by_month[month]
        solar = sum(r["solar_wh"] or 0 for r in rows)
        load = sum(r["load_wh"] or 0 for r in rows)
        volts = [r["min_v"] for r in rows if r["min_v"] is not None]
        peaks = [r["peak_v"] for r in rows if r["peak_v"] is not None]

        full = [r for r in rows
                if hours.get(r["day"], 0) >= MIN_HOURS_FOR_DAY_RANKING
                and r["solar_wh"] is not None]
        partial_days += [r["day"] for r in rows if r not in full]
        best = max(full, key=lambda r: r["solar_wh"]) if full else None
        worst = min(full, key=lambda r: r["solar_wh"]) if full else None

        gen = runs.get(month) or {g: {"hours": 0.0, "gal": 0.0, "runs": 0,
                                      "unpriced": 0} for g in history.GENS}
        met = metered.get(month) or {"kwh": 0.0, "hours": 0}
        run_hours = sum(gen[g]["hours"] for g in history.GENS)
        run_gal = sum(gen[g]["gal"] for g in history.GENS)
        has_runs = any(gen[g]["runs"] for g in history.GENS)

        # Gallons, two ways, over two eras that tile rather than overlap.
        # Modelled is precise and starts when the agent did; estimated is
        # metered generator energy over kilowatt-hours per gallon at full
        # load and covers everything the scrape reaches. They are added, not
        # chosen between: August 2026 is scraped to the 27th and has recorded
        # runs from the 28th.
        est_gal = (met["kwh"] / mean_gal) if (mean_gal and met["kwh"]) else 0.0
        # The same energy priced off the hour buckets instead, so the gap is
        # visible. It runs high - a twenty-minute run fills an hour - and is
        # reported rather than used.
        est_from_hours = (met["hours"] * gph_mean) if gph_mean else 0.0

        # What the month must have got from somewhere other than the sun and
        # the pack. A month deep in deficit beside no generator hours is then
        # visibly inconsistent rather than quietly wrong.
        batt_net = battery.get(month, 0.0)
        implied = max(0.0, (load - solar) / 1000.0 - batt_net)

        months.append({
            "month": month,
            "days_with_data": len(rows),
            "days_ranked": len(full),
            "solar_kwh": round(solar / 1000.0, 1),
            "load_kwh": round(load / 1000.0, 1),
            "net_kwh": round((solar - load) / 1000.0, 1),
            # What the sun did not cover. Deliberately simpler than
            # gen_kwh_implied below, which also nets off what the pack gave
            # up over the month: this is the plain question "how short was
            # the month", and gen_kwh is the answer to what covered it.
            "shortfall_kwh": round(max(0.0, (load - solar) / 1000.0), 1),
            "battery_net_kwh": round(batt_net, 1),
            "gen_kwh_implied": round(implied, 1),
            "gen_kwh_metered": round(met["kwh"], 1),
            "gen_hours_metered": met["hours"],
            "gen_hours_recorded": {g: round(gen[g]["hours"], 2)
                                   for g in history.GENS},
            "gen_hours": round(run_hours + met["hours"], 2),
            "gen_hours_basis": _hours_basis(run_hours, met["hours"]),
            "gen_runs": {g: gen[g]["runs"] for g in history.GENS},
            "fuel_gal_modelled": {g: (round(gen[g]["gal"], 2) if gen[g]["runs"]
                                      else 0.0) for g in history.GENS},
            "fuel_gal_estimated": round(est_gal, 2),
            "fuel_gal_estimated_from_hours": round(est_from_hours, 2),
            "fuel_gal_total": round(run_gal + est_gal, 2),
            # Generator energy on one axis for the whole record: metered
            # where the scrape reaches, and backed out of the modelled
            # gallons where only the runs do. It is fuel_gal_total times
            # kilowatt-hours per gallon by construction, so the two cannot
            # disagree.
            "gen_kwh_total": round((run_gal + est_gal) * mean_gal, 1)
                             if mean_gal else None,
            "fuel_basis": _fuel_basis(has_runs, met["kwh"]),
            "fuel_unpriced_runs": sum(gen[g]["unpriced"] for g in history.GENS),
            "min_v": round(min(volts), 2) if volts else None,
            "max_v": round(max(peaks), 2) if peaks else None,
            "best_solar_day": ({"date": best["day"],
                                "kwh": round(best["solar_wh"] / 1000.0, 1)}
                               if best else None),
            "worst_solar_day": ({"date": worst["day"],
                                 "kwh": round(worst["solar_wh"] / 1000.0, 1)}
                                if worst else None),
        })

    superlatives = _superlatives(months)
    window = months if months_wanted >= len(months) else months[-months_wanted:]

    if not detail:
        return {
            "superlatives": superlatives,
            "months_shown": len(window), "months_on_record": len(months),
            "first_month": months[0]["month"], "last_month": months[-1]["month"],
            "columns": COMPACT_COLUMNS,
            "months": [_compact(m) for m in window],
            "basis": (
                f"Superlatives rank the whole record, not the table; a month "
                f"needs {MIN_DAYS_FOR_MONTH_RANKING} days to rank. Rows "
                f"follow `columns`. shortfall_kwh is load_kwh less solar_kwh "
                f"floored at zero, how short the month was; gen_kwh is what "
                f"covered it, modelled from recorded runs since 2026-08-28 "
                f"and estimated from the metered counter before that. "
                f"Gallons come from published curves, never metered. "
                f"detail=true for one month's days."),
        }

    return {
        "months": window,
        "months_shown": len(window), "months_on_record": len(months),
        "first_month": months[0]["month"], "last_month": months[-1]["month"],
        "superlatives": superlatives,
        "partial_days_excluded_from_day_ranking": sorted(partial_days),
        "day_ranking_needs_hours": MIN_HOURS_FOR_DAY_RANKING,
        "month_ranking_needs_days": MIN_DAYS_FOR_MONTH_RANKING,
        "note": ("Every figure is summed in Python. The superlatives are the "
                 "answer to which month was best or worst - read them, do not "
                 "rank the series. Solar and load are the hourly counters "
                 "rolled up per day. Generator activity comes from two "
                 "sources that tile: recorded runs from 2026-08-28, precise "
                 "and per generator and excluding the 09:00 exercises, and "
                 "before that the scraped energy counter, which is metered "
                 "but does not say which engine ran and counts whole hours. "
                 "fuel_basis says which a month rests on. Gallons are "
                 "modelled from published consumption curves, never metered. "
                 "gen_kwh_implied is what the month must have got from "
                 "somewhere other than the sun and the pack: a large deficit "
                 "beside no generator hours means the record is incomplete, "
                 "not that the house ran on nothing."),
    }


# The compact table's fields, named once at the top of it rather than on
# every row. Twelve rows of {"month": ..., "solar_kwh": ...} spend 780
# characters repeating seven key names, which is a third of the whole budget.
COMPACT_COLUMNS = ["month", "solar_kwh", "load_kwh", "gen_kwh",
                   "shortfall_kwh", "fuel_gal", "min_v", "max_v"]


def _compact(m):
    """One month as a row under COMPACT_COLUMNS.

    Whole numbers where a decimal buys nothing: a kilowatt-hour either way in
    a month of a thousand is below the noise in the counters. The voltages
    keep one decimal, because the pack lives its whole life between 51 and 60
    and rounding them to whole numbers would throw the field away to save two
    characters.
    """
    return [
        m["month"],
        round(m["solar_kwh"]),
        round(m["load_kwh"]),
        round(m["gen_kwh_total"] or 0),
        round(m["shortfall_kwh"]),
        round(m["fuel_gal_total"]),
        round(m["min_v"], 1) if m["min_v"] is not None else None,
        round(m["max_v"], 1) if m["max_v"] is not None else None,
    ]


def _hours_basis(run_hours, metered_hours):
    parts = []
    if run_hours:
        parts.append(f"{run_hours:.1f} h from recorded runs")
    if metered_hours:
        parts.append(f"{metered_hours} whole-hour buckets the scraped counter "
                     f"showed the generator producing in, an upper bound on "
                     f"running time rather than a measurement of it")
    return " plus ".join(parts) if parts else "no generator activity recorded"


def _fuel_basis(has_runs, metered_kwh):
    parts = []
    if has_runs:
        parts.append("modelled from the runs themselves")
    if metered_kwh:
        parts.append("estimated from metered generator energy at full-load "
                     "gallons per kilowatt-hour, which cannot say which "
                     "engine produced it")
    return " and ".join(parts) if parts else "no generator activity"


def _superlatives(months):
    """The months that stand out, and why the short ones cannot.

    Each is {month, value} plus the basis, because a bare month name invites
    the question this answers.
    """
    ranked = [m for m in months
              if m["days_with_data"] >= MIN_DAYS_FOR_MONTH_RANKING]
    excluded = [f"{m['month']} ({m['days_with_data']} days)"
                for m in months if m not in ranked]
    out = {"months_ranked": len(ranked), "months_excluded": excluded}
    if not ranked:
        out["note"] = (f"no month yet has the {MIN_DAYS_FOR_MONTH_RANKING} "
                       f"days a ranking needs")
        return out

    ranked_note = (f"{len(ranked)} of {len(months)} months, "
                   f">={MIN_DAYS_FOR_MONTH_RANKING} days each")

    def pick(key, chooser, field, basis=ranked_note):
        m = chooser(ranked, key=lambda r: r[key])
        return {"month": m["month"], "value": m[key], "units": field,
                "basis": basis}

    out.update({
        "worst_solar_month": pick("solar_kwh", min, "kWh"),
        "best_solar_month": pick("solar_kwh", max, "kWh"),
        "highest_load_month": pick("load_kwh", max, "kWh"),
        "lowest_load_month": pick("load_kwh", min, "kWh"),
        "most_fuel_month": pick("fuel_gal_total", max, "US gallons",
                                basis=f"{ranked_note}; complete fuel series, "
                                      f"recorded runs plus metered energy, "
                                      f"not the runs alone"),
    })
    if out["most_fuel_month"]["value"] == 0:
        out["most_fuel_month"] = {
            "month": None, "value": 0.0, "units": "US gallons",
            "basis": "no generator activity in any month long enough to rank"}
    return out
