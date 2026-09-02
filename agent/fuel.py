#!/usr/bin/env python3
"""What a generator run cost in diesel.

Measurement and reporting. Nothing here decides anything: no threshold moves
because of a gallon, no generator is chosen over another for being cheaper to
run. That would be a different commit and a different argument.

The model is a consumption curve per generator, `[load_fraction, gal_per_hr]`
points from the manifest, interpolated linearly between them and held flat
beyond either end. A run's load fraction is `gross_w / rated_w` and its cost
is `gal_per_hr * hours`.

Two things about that fraction are worth saying plainly, because a number
with a unit on it invites more trust than this one has earned:

`gross_w` is measured at the shunt and at the inverters' load output - what
went into the pack plus what the house took - so it is DC delivered, and the
generator's AC output is higher by whatever the chargers lose, on the order
of 8-10%. The published curves are written in AC terms. Using them against a
DC fraction therefore reads the engine as working less hard than it is, and
under-reports. The bias is one-directional and a tank-fill calibration
removes it: fill, run, fill again, and scale the curve by the pump against
this. Until then every figure here is an estimate that leans low.

And the MEP's half-load point is community-reported rather than published, so
a half-load MEP figure is softer than a full-load one. The Kubota's four
points are all from its operator's manual.
"""

import argparse
import logging
import os
import sys

log = logging.getLogger(__name__)


def gal_per_hour(curve, load_fraction):
    """Gallons an hour at a given fraction of rated output.

    Linear between the points, flat outside them. Flat rather than
    extrapolated at both ends on purpose: below the first point a diesel's
    consumption flattens out into its idle draw rather than falling to zero,
    and above the last it is bounded by the engine, not by the line the last
    two points happened to make. `load_fraction` above 1.0 is allowed and
    simply lands on the flat.

    None when the curve is empty; a one-point curve is that one value
    everywhere, which is the honest reading of a single measurement.
    """
    pts = sorted((float(f), float(g)) for f, g in (curve or []))
    if not pts:
        return None
    if load_fraction is None:
        return None
    x = float(load_fraction)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        x1, y1 = pts[i]
        if x1 >= x:
            x0, y0 = pts[i - 1]
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def model_for(cfg, gen):
    """{rated_w, curve} for one generator, or None if it has no fuel model."""
    m = (cfg.get("fuel") or {}).get(gen)
    if not m or not m.get("rated_w") or not m.get("curve"):
        return None
    return m


def load_fraction(cfg, gen, gross_w):
    """How hard the run worked the engine, as a fraction of its rated output."""
    m = model_for(cfg, gen)
    if not m or gross_w is None:
        return None
    return float(gross_w) / float(m["rated_w"])


def gallons(cfg, gen, gross_w, minutes):
    """Diesel burned by one run, or None if it cannot be computed.

    None rather than zero wherever an input is missing: a run whose gross was
    never measured did burn fuel, and recording nought would make the totals
    quietly wrong instead of visibly incomplete.
    """
    m = model_for(cfg, gen)
    if not m or gross_w is None or minutes is None:
        return None
    rate = gal_per_hour(m["curve"], load_fraction(cfg, gen, gross_w))
    if rate is None:
        return None
    return round(rate * (float(minutes) / 60.0), 3)


# --- whose watts were they -----------------------------------------------------
#
# gross_w is the system's delivery for a minute, credited whole to every
# generator that was running. That is right for learning a charge rate - it is
# what came out of the engines, and the paired figure is kept separate from
# the solo one - but wrong for fuel, where two engines sharing a minute did
# not each produce all of it. Priced on the raw figure, the 2026-08-30 pair
# read the Kubota at 1.40 of rated and the MEP at 1.32, and only the flat top
# of the curve kept the answer from being nonsense.

def solo_share(conn, cfg, now=None):
    """(share per generator, what the split was based on).

    Each engine's share of a shared minute is its own learned solo delivery
    over the pair's. Learned, not rated: what the two actually put out is a
    better guide to how they divided a minute than the numbers on their
    plates, and the Kubota's plate is 7 kW while its runs also drive a Magnum
    the Schneider units cannot see.

    Rated watts stand in until both engines have solo runs to learn from.
    (None, reason) when neither basis is available, which leaves the run
    unattributed rather than split on a guess.
    """
    import loadmodel
    gens = ("mep", "kubota")
    model = loadmodel.LoadModel(conn, cfg)
    learned = {g: (model.charge_rate(g, solo=True, now=now) or {}).get("gross_w")
               for g in gens}
    if all(learned.get(g) for g in gens):
        total = sum(learned[g] for g in gens)
        return {g: learned[g] / total for g in gens}, "learned solo gross"
    rated = {g: (model_for(cfg, g) or {}).get("rated_w") for g in gens}
    if all(rated.get(g) for g in gens):
        total = sum(rated[g] for g in gens)
        return {g: rated[g] / total for g in gens}, "rated watts"
    return None, "no learned solo rate and no rated watts"


def attribute(gross, paired, share):
    """Mean gross credited to one engine, in watts, or None.

    `gross` and `paired` are the run's per-minute system delivery and whether
    the other engine was turning that minute. A minute it ran alone is all
    its own; a shared minute is `share` of it. The mean is over the whole run
    so the result sits on the same axis as gross_w and can be read against
    rated_w the same way.

    None when there is nothing to average, or when a shared minute is present
    and no share was worked out - better an unattributed run, priced on the
    system figure and known to over-read, than a number split on nothing.
    """
    if not gross:
        return None
    flags = list(paired or [])
    if len(flags) != len(gross):
        flags = [False] * len(gross)
    if any(flags) and not share:
        return None
    total = 0.0
    for g, shared in zip(gross, flags):
        total += g * share if shared else g
    return round(total / len(gross), 1)


def attributed_gross(conn, cfg, gen, start_ts, stop_ts, share=None):
    """`attribute` for a run already in the table, from its minute samples.

    The same arithmetic as at close, over the samples the run spanned. Where
    those have been purged - they are kept 90 days - there is nothing to
    split and this returns None.
    """
    import history
    if share is None:
        share, _ = solo_share(conn, cfg, now=start_ts)
    other_col = "kub_action" if gen == "mep" else "mep_action"
    rows = conn.execute(
        f"SELECT batt_power, ac_power1, ac_power2, {other_col} AS other "
        "FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (start_ts, stop_ts)).fetchall()
    gross, flags = [], []
    for r in rows:
        if r["ac_power1"] is None and r["ac_power2"] is None:
            continue
        if r["batt_power"] is None:
            continue
        gross.append(r["batt_power"] + (r["ac_power1"] or 0) + (r["ac_power2"] or 0))
        flags.append(r["other"] == history.GEN_RUNNING)
    return attribute(gross, flags, (share or {}).get(gen))


def gross_for_pricing(row):
    """The watts a run is priced on: its own share, or the system figure.

    A solo run's two figures are the same number. A paired run falls back to
    the system figure only when the split could not be computed at all, and
    that reads the engine high.
    """
    attr = row["gross_attr_w"] if "gross_attr_w" in row.keys() else None
    return attr if attr is not None else row["gross_w"]


# --- what it costs, when the owner has said -----------------------------------

def price(cfg):
    """Dollars a gallon, or None. Unset is normal and never an error."""
    p = cfg.get("diesel_price_per_gal")
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def cost(cfg, gal):
    if gal is None:
        return None
    p = price(cfg)
    return round(gal * p, 2) if p is not None else None


def phrase(cfg, gal, approx="~", noun="gal"):
    """"~1.2 gal", or "~1.2 gal ≈ $4.80" once a price is set. None if nothing.

    The cost goes last so the sentence still reads if the noun is longer than
    a unit - "~1.4 gal of diesel ≈ $5.94".
    """
    if gal is None:
        return None
    out = f"{approx}{gal:.1f} {noun}"
    c = cost(cfg, gal)
    if c is not None:
        out += f" ≈ ${c:,.2f}"
    return out


# --- planning ------------------------------------------------------------------

def estimate(cfg, model, gens, hours, solo=None, now=None):
    """Diesel a planned run of `hours` would burn, per generator and in total.

    Each generator is priced on its own gross delivery - learned where there
    are runs to learn from, the configured assumption before that - because
    two engines sharing a night are not one engine working twice as hard.

    Returns {"gal": total or None, "hours": hours, "per_gen": {gen: {...}}}.
    """
    out = {"gal": None, "hours": hours, "per_gen": {}}
    if hours is None or hours <= 0:
        return out
    total = 0.0
    seen = False
    for gen in gens or ():
        gross = model.gross_for(gen, solo=solo, now=now)
        gal = gallons(cfg, gen, gross, hours * 60.0)
        out["per_gen"][gen] = {"gross_w": gross, "gal": gal,
                               "load_fraction": load_fraction(cfg, gen, gross)}
        if gal is not None:
            total += gal
            seen = True
    if seen:
        out["gal"] = round(total, 2)
    return out


# --- filling in what happened before this existed ------------------------------

def backfill_attribution(conn, cfg):
    """Split the gross of every past run that has not been split yet.

    Idempotent: only rows where gross_attr_w IS NULL. A run whose samples
    have been purged cannot be split and stays NULL, so it is looked at again
    next time and still writes nothing.

    Returns (attributed, unattributable).
    """
    share, _basis = solo_share(conn, cfg)
    rows = conn.execute(
        "SELECT id, gen, start_ts, stop_ts FROM gen_runs "
        "WHERE gross_attr_w IS NULL AND stop_ts IS NOT NULL").fetchall()
    done, missing = 0, 0
    for r in rows:
        attr = attributed_gross(conn, cfg, r["gen"], r["start_ts"], r["stop_ts"],
                                share=share)
        if attr is None:
            missing += 1
            continue
        conn.execute("UPDATE gen_runs SET gross_attr_w=? WHERE id=?",
                     (attr, r["id"]))
        done += 1
    conn.commit()
    return done, missing


def backfill(conn, cfg):
    """Split what has not been split, then price what has not been priced.

    Idempotent in both halves: gross_attr_w is only written where it is NULL
    and fuel_gal only where it is NULL, so a second run changes nothing. Runs
    without a gross are left alone rather than guessed at - see gallons().

    Returns (filled, skipped) where skipped are rows it could not price.
    """
    backfill_attribution(conn, cfg)
    # Either figure will do to price on. Some early runs predate gross_w
    # entirely - it arrived in a later migration - and the attribution reads
    # their minutes fresh, so it can price a run the close never could.
    rows = conn.execute(
        "SELECT id, gen, gross_w, gross_attr_w, duration_min FROM gen_runs "
        "WHERE fuel_gal IS NULL AND duration_min IS NOT NULL "
        "AND (gross_w IS NOT NULL OR gross_attr_w IS NOT NULL)").fetchall()
    filled, skipped = 0, 0
    for r in rows:
        gal = gallons(cfg, r["gen"], gross_for_pricing(r), r["duration_min"])
        if gal is None:
            skipped += 1
            continue
        conn.execute("UPDATE gen_runs SET fuel_gal=? WHERE id=?", (gal, r["id"]))
        filled += 1
    conn.commit()
    return filled, skipped


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
    import history

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backfill", action="store_true",
                    help="fill fuel_gal for past runs that have gross_w")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load()
    conn = history.connect(args.db)

    if args.backfill:
        share, basis = solo_share(conn, cfg)
        if share:
            print("shared minutes split by " + basis + ": "
                  + ", ".join(f"{g} {share[g] * 100:.0f}%" for g in sorted(share)))
        split, unsplit = backfill_attribution(conn, cfg)
        filled, skipped = backfill(conn, cfg)
        print(f"gross attributed for {split} run(s); {unsplit} had no samples left")
        print(f"fuel_gal filled for {filled} run(s); "
              f"{skipped} could not be computed")

    rows = conn.execute(
        "SELECT gen, COUNT(*) n, SUM(duration_min)/60.0 h, SUM(fuel_gal) gal, "
        "SUM(fuel_gal IS NULL) unpriced FROM gen_runs GROUP BY gen").fetchall()
    for r in rows:
        gal = f"{r['gal']:.2f} gal" if r["gal"] is not None else "no fuel figure"
        c = cost(cfg, r["gal"])
        print(f"{r['gen']:8s} {r['n']:4d} runs  {r['h'] or 0:7.1f} h  {gal:>16s}"
              + (f"  ${c:,.2f}" if c is not None else "")
              + (f"  ({r['unpriced']} unpriced)" if r["unpriced"] else ""))


if __name__ == "__main__":
    main()
