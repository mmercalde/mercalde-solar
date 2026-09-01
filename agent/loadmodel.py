"""Statistical model of the house, learned from history. Pure Python, no LLM.

SPEC section 5:
  - hourly load profile by hour-of-day and weekday/weekend, seasonal by month
  - overnight drawdown Wh (sunset -> sunrise) by month
  - solar yield vs cloud cover, learned per month
  - charge rate per generator, solo and paired
  - watt-hours between two pack voltages, from overnight discharges
  - excludes every hour a generator was running, exercise runs included

What the pack holds is the Wh-vs-V curve and never the Battery Monitor's
state of charge. That figure is one shunt's percentage multiplied by a
capacity derived from the same shunt, so it cannot check itself; it is shown
to the owner and used in no decision, and `soc_disagreement` watches it
against the curve so a drifted shunt is still noticed.

The model never guesses past the evidence: where there is not enough history
it returns None and says how much it has, so the guard and the plan record can
say "not yet learned" instead of inventing a number.
"""

import calendar
import logging
import math
import statistics
import time
from datetime import datetime, timedelta

import history
import sun
import weather

log = logging.getLogger(__name__)

# A profile cell needs this many observations before it is worth quoting.
MIN_SAMPLES_PER_HOUR = 3
# How the house's own history is preferred, newest first. The first tier with
# enough evidence in it wins outright, so this year's pattern takes over from
# last year's the moment there is a fortnight of it - a household changes, and
# an August from three years ago should not still be arguing with this week.
RECENT_TIERS = ((14, "last 14 nights"), (60, "last 60 days"))
MIN_NIGHTS_PER_TIER = 3
# The cleaned hourly rows are built once and reused until the table changes,
# rather than once per hour of every forecast walk.
MIN_DAYS_FOR_SOLAR_FIT = 8
# A run shorter than this cannot say anything about a rate.
MIN_RUN_MINUTES = 10
# Runs needed before the charge-side curve is trusted over the resting one.
MIN_CHARGE_CURVE_RUNS = 3

# A projection this close is reported as a window, not a clock time: the pack
# is already at the target and the minute is noise.
PROJECTION_SOON_SECONDS = 900

# A voltage bin of the learned curve needs this many observations before it
# is treated as a point. The backfill supplies thousands per bin; live
# sampling alone can take weeks to reach it at the start threshold.
MIN_SOC_OBSERVATIONS = 10

# --- the energy-vs-voltage curve ---------------------------------------------
#
# How many watt-hours sit between two pack voltages, learned from what the
# house actually took out between them on nights the pack was doing nothing
# else. It replaces state of charge times capacity, which asked the Battery
# Monitor's shunt for a number no one had checked against the meter.
#
# A quarter of a volt: an ordinary discharge hour moves the pack across one
# or two of these, so a night contributes a rate to each bin it crosses
# rather than one lump to whichever bin it happened to sit in.
ENERGY_BIN_V = 0.25
# A night that clipped the corner of a bin has a Wh-per-volt that is mostly
# rounding. It has to cross this much of the bin to speak for it.
MIN_BIN_FRACTION = 0.4
# Nights behind a bin before it is quoted.
MIN_NIGHTS_PER_BIN = 3
# The window the curve is built over. Wider than any plausible pack voltage,
# so the query never has to guess at the range it will find data in.
CURVE_V_LOW, CURVE_V_HIGH = 40.0, 70.0


def _median(values):
    return statistics.median(values) if values else None


def _isotonic(points):
    """Force a (volts, soc, weight) curve to be non-decreasing in voltage.

    State of charge cannot fall as voltage rises, but the raw observations say
    otherwise between about 54 and 56 V: during and just after a generator run
    the terminal voltage is elevated while the shunt's SOC still lags, so those
    minutes land at a high voltage with a mid-range SOC. Pool-adjacent-
    violators merges exactly those runs, weighted by how many observations
    each bin carries, and leaves an already-monotonic stretch untouched.
    """
    blocks = []                       # [sum(soc*w), sum(w), [indices]]
    for _, soc, w in points:
        blocks.append([soc * w, w, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            a = blocks.pop()
            b = blocks.pop()
            blocks.append([a[0] + b[0], a[1] + b[1], a[2] + b[2]])
    out, i = [], 0
    for total, weight, count in blocks:
        value = total / weight
        for _ in range(count):
            volts, _soc, w = points[i]
            out.append((volts, value, w))
            i += 1
    return out


def _kw(watts):
    return f"{watts / 1000.0:.1f} kW"


def rate_phrase(rate, expected_load_w=None, net_w=None, soc_per_h=None):
    """How a charge rate is written wherever the owner or the model sees one.

    Both halves, always: what the generator delivers and what the house is
    expected to take out of it, because the difference is the whole reason a
    run that looked slow once was slow.
    """
    if not rate or rate.get("gross_w") is None:
        return "no observed rate"
    assumed = ", assumed" if rate.get("assumed") else ""
    if rate.get("assumed_net"):
        head = f"{_kw(rate['gross_w'])} into pack{assumed}"
    elif expected_load_w is None or net_w is None:
        return f"gross {_kw(rate['gross_w'])}{assumed}"
    else:
        head = (f"gross {_kw(rate['gross_w'])} − expected load "
                f"{_kw(expected_load_w)} = {_kw(net_w)} into pack{assumed}")
    if soc_per_h is not None:
        head += f" ({soc_per_h:.1f}% SOC/h)"
    return head


def _weighted_median(pairs):
    """Median of [(value, weight)] without expanding the weights."""
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    half = total / 2.0
    seen = 0
    for value, weight in pairs:
        seen += weight
        if seen >= half:
            return value
    return pairs[-1][0]


class ChargeCurve:
    """How the pack behaves while a generator is charging it.

    The resting curve says what state of charge a pack holds at a voltage once
    it has settled. The Pi5 does not stop on that: it stops on the terminal
    voltage during the charge, which internal resistance and surface charge
    lift well above the resting value. Estimating a run against the resting
    curve therefore asks the generator to deliver charge the run never needed
    - the morning both generators went 52 to 56 in 70 minutes, the resting
    estimate wanted 2.8 hours for 57.

    So this is learned separately, from the minute samples inside real runs,
    and holds two relations:

      soc_points  terminal volts against state of charge
      traces      per run, the first minute the pack read each voltage

    The traces answer "how long did this actually take" directly, which is the
    better answer when the voltages either side have been seen. soc_points
    carry the rest.
    """

    def __init__(self, gen, solo, traces, soc_points):
        self.gen, self.solo = gen, solo
        self.traces = traces
        self.soc_points = soc_points

    @property
    def runs(self):
        return len(self.traces)

    @property
    def learned(self):
        return self.runs >= MIN_CHARGE_CURVE_RUNS

    @property
    def label(self):
        who = "both generators" if self.gen is None else self.gen
        if self.solo is not None:
            who += " solo" if self.solo else " paired"
        return f"{who}, {self.runs} run{'' if self.runs == 1 else 's'}"

    @staticmethod
    def _arrival(trace, v):
        """Minutes into the run when the pack first read at least `v`."""
        want = history.soc_bin(v)
        seen = [m for b, m in trace.items() if b >= want]
        return min(seen) if seen else None

    @staticmethod
    def _voltage_by(trace, minutes):
        """The highest voltage the pack had reached by `minutes` into the run."""
        seen = [b for b, m in trace.items() if m <= minutes]
        return history.soc_bin_volts(max(seen)) if seen else None

    def _usable(self, from_v):
        """Runs that actually passed through from_v on their way up.

        A run that began above from_v never charged from there, and one that
        began well below it reached from_v with surface charge already built,
        so it would understate a fresh run. Only runs that started at or below
        from_v are counted, and the estimate from mid-run is optimistic by
        whatever charge had already gone in.
        """
        return [t for t in self.traces
                if t["start_v"] is not None
                and t["start_v"] <= from_v + history.SOC_BIN_V]

    def fell_short(self, from_v, to_v, window_h):
        """Runs that passed from_v, had the whole window, and never reached to_v.

        A run that ends on the Pi5's runtime cap short of its stop is not
        silence about that stop: it is the pack saying no. Last night the
        Kubota ran its full 120 minutes and finished well under 57, and the
        estimate went on treating 57 as reachable because no run had ever
        recorded a time for it.
        """
        out = []
        for t in self._usable(from_v):
            reached = self._arrival(t["trace"], from_v)
            if reached is None or t["max_v"] >= to_v - history.SOC_BIN_V:
                continue
            if t["minutes"] - reached + 1 >= window_h * 60:
                out.append(t)
        return out

    @staticmethod
    def _load_scale(t, expected_load_w):
        """How much longer this run would have taken under a different load.

        A run delivering gross G against load L put G - L into the pack. Under
        the load the window ahead expects it would put in G - L', so it would
        take (G - L) / (G - L') times as long. None where the run's own
        figures are missing or the arithmetic is not credible.
        """
        if expected_load_w is None:
            return 1.0
        gross, load = t.get("gross_w"), t.get("load_w")
        if gross is None or load is None:
            return None
        then, now_ = gross - load, gross - expected_load_w
        if then <= 0 or now_ <= 0:
            return None
        scale = then / now_
        # Beyond this the two loads are too far apart for one run to speak
        # about the other.
        return scale if 0.25 <= scale <= 4.0 else None

    def minutes_between(self, from_v, to_v, expected_load_w=None):
        """Observed minutes from one terminal voltage to another.

        Timed against the load each run actually faced, then rescaled to the
        load the window ahead expects.
        """
        deltas = []
        for t in self._usable(from_v):
            a = self._arrival(t["trace"], from_v)
            b = self._arrival(t["trace"], to_v)
            if a is None or b is None or b < a:
                continue
            scale = self._load_scale(t, expected_load_w)
            if scale is None:
                continue
            deltas.append((b - a) * scale)
        if len(deltas) < MIN_CHARGE_CURVE_RUNS:
            return None
        return _median(deltas), len(deltas)

    def voltage_after(self, from_v, hours, expected_load_w=None):
        """The terminal voltage reached `hours` after passing from_v.

        Under a heavier load than the run faced the same wall-clock hour buys
        less charge, so the budget is scaled the other way from the minutes.
        """
        reached = []
        for t in self._usable(from_v):
            a = self._arrival(t["trace"], from_v)
            if a is None:
                continue
            scale = self._load_scale(t, expected_load_w)
            if scale is None:
                continue
            v = self._voltage_by(t["trace"], a + hours * 60.0 / scale)
            if v is not None:
                reached.append(v)
        if len(reached) < MIN_CHARGE_CURVE_RUNS:
            return None
        return _median(reached)

    def soc_for_voltage(self, target_v):
        return LoadModel._interpolate(self.soc_points, target_v)

    def volts_for_soc(self, target_soc):
        return _invert(self.soc_points, target_soc)


# How much of a curve's top the slope above it is measured across. The
# charging curve is binned to 0.05 V and its state of charge is whole points,
# so consecutive points are usually flat and the last two of them say nothing.
# A volt is wide enough to have real rise in it and narrow enough to still be
# the top of the curve.
TOP_SLOPE_SPAN_V = 1.0


def _top_slope(points, span=TOP_SLOPE_SPAN_V):
    """Points of state of charge to the volt across the top of a curve.

    A weighted least-squares fit over every point in the top `span`, each
    weighted by how many observations made it, rather than the two end
    points. The tail of a learned curve is where the observations run out,
    and one thinly-seen bin should not set the slope everything above the
    curve is carried on: the Kubota's charge curve has a single sample
    reading 96% at 54.70 V against 88% at 54.65, and endpoint arithmetic
    turns that into 16 points a volt.

    None where the top does not rise. A flat top means the pack is full up
    there and the voltage is being made by current against internal
    resistance, which a state-of-charge model has nothing to say about.
    """
    if not points or len(points) < 2:
        return None
    top = points[-1][0]
    fit = [p for p in points if p[0] >= top - span] or list(points)
    if len(fit) < 2:
        return None
    sw = sum(max(n, 1) for _, _, n in fit)
    swx = sum(max(n, 1) * v for v, _, n in fit)
    swy = sum(max(n, 1) * y for _, y, n in fit)
    swxx = sum(max(n, 1) * v * v for v, _, n in fit)
    swxy = sum(max(n, 1) * v * y for v, y, n in fit)
    denom = sw * swxx - swx * swx
    if denom <= 0:
        return None
    slope = (sw * swxy - swx * swy) / denom
    return slope if slope > 0 else None


def _invert(curve, target_soc):
    """The voltage at a given state of charge on a (volts, soc, n) curve."""
    if not curve:
        return None
    volts = [v for v, _, _ in curve]
    socs = [s for _, s, _ in curve]
    if target_soc <= socs[0]:
        return volts[0]
    if target_soc >= socs[-1]:
        return volts[-1]
    for i in range(1, len(socs)):
        if socs[i] >= target_soc:
            s0, s1 = socs[i - 1], socs[i]
            v0, v1 = volts[i - 1], volts[i]
            if s1 == s0:
                return v1
            return v0 + (v1 - v0) * (target_soc - s0) / (s1 - s0)
    return volts[-1]


class LoadModel:
    def __init__(self, conn, cfg, as_of=False):
        # `as_of` makes every query respect the `now` it is given rather than
        # the newest row in the table. Only replay sets it: live, "now" and
        # "the newest row" are the same moment, and the extra bounds would
        # only cost query time.
        self.as_of = as_of
        # `conn` may be a connection or a provider of one; see history.resolve.
        self._conn = conn
        self.cfg = cfg

    def _at(self, now):
        """The moment a "newest row" question is asked as of, or None.

        None live, where the newest row and now are the same moment. Replay
        sets `as_of` and gets the newest row at or before the tick being
        replayed instead.
        """
        return int(now) if self.as_of and now else None

    @property
    def conn(self):
        return history.resolve(self._conn)

    # --- shared filtering ---------------------------------------------------

    def _clean_load_rows(self, month=None, weekend=None):
        """hourly load rows with every generator-affected hour removed.

        While a generator runs, the XW AC output is not house load, so those
        hours say nothing about consumption. Exercise runs are gen runs, so
        this drops them too.
        """
        rows = self._all_load_rows()
        out = []
        for t, wh in rows:
            if month is not None and t.month != month:
                continue
            if weekend is not None and (t.weekday() >= 5) != weekend:
                continue
            out.append((t, wh))
        return out

    def _all_load_rows(self):
        """Every clean hourly load row, cached until the table changes.

        A forecast walk asks for the profile once an hour of the window, and
        each ask used to rescan the table and recompute the generator hours.
        The cache is keyed on how many rows there are and how recent the
        newest is, so a rollup during a tick is picked up at once rather than
        waited out.
        """
        probe = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(hour_ts) AS newest FROM hourly "
            "WHERE device='load' AND wh_out IS NOT NULL").fetchone()
        key = (probe["n"], probe["newest"])
        cached = getattr(self, "_rows_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        rows = self.conn.execute(
            "SELECT hour_ts, wh_out FROM hourly "
            "WHERE device='load' AND wh_out IS NOT NULL ORDER BY hour_ts").fetchall()
        out = []
        if rows:
            excluded = history.gen_running_hours(
                self.conn, rows[0]["hour_ts"], rows[-1]["hour_ts"] + 3600)
            out = [(history.local(r["hour_ts"], self.cfg), r["wh_out"])
                   for r in rows if r["hour_ts"] not in excluded]
        self._rows_cache = (key, out)
        return out

    def _tiers(self, now, month=None):
        """(rows, label) in order of preference, newest evidence first."""
        rows = self._all_load_rows()
        today = history.local(now, self.cfg).date()
        month = month or history.local(now, self.cfg).month
        for days, label in RECENT_TIERS:
            yield [(t, wh) for t, wh in rows
                   if 0 <= (today - t.date()).days <= days], label
        yield ([(t, wh) for t, wh in rows
                if t.month == month and t.year < today.year],
               f"{calendar.month_abbr[month]} in prior years")
        yield rows, "all history"

    # --- load profile -------------------------------------------------------

    def load_profile(self, month=None, weekend=None, now=None):
        """{hour_of_day: median Wh}, from the newest evidence that covers a day.

        Recency first, then the weekday/weekend split within a tier if there
        is enough of it. The two recent tiers are already in season, so they
        are not filtered by month as well; the prior-year tier is the month
        by definition.
        """
        now = int(now or time.time())
        for rows, label in self._tiers(now, month):
            for w in (weekend, None):
                buckets = {}
                for t, wh in rows:
                    if w is not None and (t.weekday() >= 5) != w:
                        continue
                    buckets.setdefault(t.hour, []).append(wh)
                profile = {h: _median(v) for h, v in buckets.items()
                           if len(v) >= MIN_SAMPLES_PER_HOUR}
                if len(profile) >= 12:
                    return {"profile": profile, "month": month, "weekend": w,
                            "source": label, "hours_covered": len(profile),
                            "observations": sum(len(v) for v in buckets.values())}
        return {"profile": {}, "month": None, "weekend": None, "source": None,
                "hours_covered": 0, "observations": 0}

    def load_forecast(self, hours, now=None):
        """Expected Wh over the next N hours, hour by hour."""
        now = int(now or time.time())
        tz = history.tzinfo(self.cfg)
        by_hour, total, missing = [], 0.0, 0
        for i in range(hours):
            t = datetime.fromtimestamp(now + i * 3600, tz)
            p = self.load_profile(month=t.month, weekend=t.weekday() >= 5)
            wh = p["profile"].get(t.hour)
            if wh is None:
                wh = _median(list(p["profile"].values()))
            if wh is None:
                missing += 1
                continue
            by_hour.append({"hour": t.hour, "wh": round(wh)})
            total += wh
        return {"hours": hours, "total_wh": round(total),
                "by_hour": by_hour, "hours_unknown": missing,
                "learned": missing == 0 and bool(by_hour)}

    @staticmethod
    def _whole_nights(rows, sunset_h, sunrise_h):
        """{night: Wh} for nights with every hour present.

        A night missing hours - to a generator run, or to a sampling gap -
        would understate the drawdown, so it is left out rather than counted
        short.
        """
        by_night = {}
        for t, wh in rows:
            if t.hour >= sunset_h or t.hour < sunrise_h:
                # Hours after midnight belong to the night that began yesterday.
                night = (t.date() if t.hour >= sunset_h
                         else (t - timedelta(days=1)).date())
                by_night.setdefault(night, []).append(wh)
        night_hours = 24 - sunset_h + sunrise_h
        return {n: sum(v) for n, v in by_night.items() if len(v) >= night_hours}

    def overnight_drawdown(self, month=None, now=None):
        """Median Wh consumed between sunset and sunrise, newest evidence first.

        Reports which tier it leaned on, because "15,200 Wh" from a fortnight
        of this year and "15,200 Wh" from three Augusts ago are not the same
        claim and the plan record should not present them as one.
        """
        now = int(now or time.time())
        month = month or history.local(now, self.cfg).month
        times = sun.times(self.cfg, now=now)
        if not times:
            return None
        sunset_h = history.local(times[1], self.cfg).hour
        sunrise_h = history.local(times[0], self.cfg).hour
        for rows, label in self._tiers(now, month):
            nights = self._whole_nights(rows, sunset_h, sunrise_h)
            enough = (MIN_NIGHTS_PER_TIER if label != "all history" else 1)
            if len(nights) >= enough:
                return {"month": month, "wh": round(_median(list(nights.values()))),
                        "nights": len(nights), "source": label}
        return None

    # --- energy against voltage ---------------------------------------------

    def _discharge_nights(self, now):
        """{night: {bin: [Wh, volts crossed]}} for clean overnight discharges.

        One record per hour the pack was doing nothing but supplying the
        house: between sunset and sunrise, no generator producing, no solar,
        and the voltage falling. The hour's load Wh is spread across the
        voltage bins between the hour's high and low reading in proportion to
        how much of each bin it crossed, so an hour that fell 54.40 to 53.90
        contributes a rate to each of the two bins it passed through instead
        of a lump to one of them.

        Cached on the shape of the table, like the load rows: a forecast walk
        asks for this many times and the query is a three-way join.
        """
        probe = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(hour_ts) AS newest FROM hourly").fetchone()
        times = sun.times(self.cfg, now=now)
        if not times:
            return {}
        sunset_h = history.local(times[1], self.cfg).hour
        sunrise_h = history.local(times[0], self.cfg).hour
        key = (probe["n"], probe["newest"], sunset_h, sunrise_h)
        cached = getattr(self, "_nights_cache", None)
        if cached and cached[0] == key:
            return cached[1]

        rows = self.conn.execute(
            "SELECT b.hour_ts, b.min_v, b.max_v, l.wh_out AS load_wh, "
            "       COALESCE(g.wh_in, 0) AS gen_wh, "
            "       COALESCE(s.wh_in, 0) AS solar_wh "
            "FROM hourly b "
            "JOIN hourly l ON l.hour_ts = b.hour_ts AND l.device = 'load' "
            "LEFT JOIN hourly g ON g.hour_ts = b.hour_ts AND g.device = 'gen' "
            "LEFT JOIN hourly s ON s.hour_ts = b.hour_ts AND s.device = 'solar' "
            "WHERE b.device = 'battery' AND b.min_v IS NOT NULL "
            "  AND b.max_v IS NOT NULL AND l.wh_out IS NOT NULL "
            "ORDER BY b.hour_ts").fetchall()
        running = set()
        if rows:
            # `gen` energy covers the scraped years; gen_runs covers the live
            # period. Neither alone spans the history this is built from.
            running = history.gen_running_hours(
                self.conn, rows[0]["hour_ts"], rows[-1]["hour_ts"] + 3600)

        nights = {}
        for r in rows:
            t = history.local(r["hour_ts"], self.cfg)
            if not (t.hour >= sunset_h or t.hour < sunrise_h):
                continue
            if r["gen_wh"] > 0 or r["solar_wh"] > 0 or r["hour_ts"] in running:
                continue
            lo, hi, wh = r["min_v"], r["max_v"], r["load_wh"]
            if hi <= lo or not wh or wh <= 0:
                continue
            night = (t.date() if t.hour >= sunset_h
                     else (t - timedelta(days=1)).date())
            bins = nights.setdefault(night, {})
            span = hi - lo
            b = int(lo // ENERGY_BIN_V)
            while b * ENERGY_BIN_V < hi:
                a = max(lo, b * ENERGY_BIN_V)
                z = min(hi, (b + 1) * ENERGY_BIN_V)
                if z > a:
                    cell = bins.setdefault(b, [0.0, 0.0])
                    cell[0] += wh * (z - a) / span
                    cell[1] += z - a
                b += 1
        self._nights_cache = (key, nights)
        return nights

    def _night_tiers(self, now, month=None):
        """({night: bins}, label) newest evidence first - the profile's tiers.

        Deliberately the same windows overnight_drawdown walks, so "last 14
        nights" means the same fortnight in both and the plan record cannot
        quote one against the other.
        """
        nights = self._discharge_nights(now)
        today = history.local(now, self.cfg).date()
        month = month or history.local(now, self.cfg).month
        for days, label in RECENT_TIERS:
            yield {n: v for n, v in nights.items()
                   if 0 <= (today - n).days <= days}, label
        yield ({n: v for n, v in nights.items()
                if n.month == month and n.year < today.year},
               f"{calendar.month_abbr[month]} in prior years")
        yield nights, "all history"

    @staticmethod
    def _energy_bins(nights):
        """{bin: (median Wh across nights, nights behind it)}.

        The median is taken over watt-hours per volt and multiplied back up,
        so a night that crossed half a bin and one that crossed all of it are
        the same measurement rather than one being half the other.
        """
        rates = {}
        for bins in nights.values():
            for b, (wh, volts) in bins.items():
                if volts >= ENERGY_BIN_V * MIN_BIN_FRACTION:
                    rates.setdefault(b, []).append(wh / volts)
        return {b: (_median(v) * ENERGY_BIN_V, len(v))
                for b, v in rates.items() if len(v) >= MIN_NIGHTS_PER_BIN}

    @staticmethod
    def _sum_bins(bins, v_from, v_to):
        """(Wh between two voltages, nights behind the thinnest bin, gap).

        `gap` names the first stretch no night crossed, because a curve with
        a hole in it cannot answer for a range that spans the hole and saying
        so is better than adding up the parts that are there.
        """
        total, fewest = 0.0, None
        b = int(v_from // ENERGY_BIN_V)
        while b * ENERGY_BIN_V < v_to:
            a = max(v_from, b * ENERGY_BIN_V)
            z = min(v_to, (b + 1) * ENERGY_BIN_V)
            if z > a:
                if b not in bins:
                    return None, None, (f"no night crossed "
                                        f"{b * ENERGY_BIN_V:.2f}-"
                                        f"{(b + 1) * ENERGY_BIN_V:.2f} V")
                wh, n = bins[b]
                total += wh * (z - a) / ENERGY_BIN_V
                fewest = n if fewest is None else min(fewest, n)
            b += 1
        return total, fewest, None

    def energy_above(self, v_from, v_to, month=None, now=None):
        """Watt-hours the pack holds between two voltages, learned.

        The successor to state of charge times capacity. That asked the
        Battery Monitor what fraction of the pack was left and multiplied by
        a size derived from the same shunt, so a shunt that had drifted was
        both the measurement and its own check. This asks the house instead:
        on nights the pack was doing nothing but supplying it, how many
        watt-hours went out between these two voltages.

        {"wh", "nights", "source", ...} or {"wh": None, "reason": ...}.
        """
        now = int(now or time.time())
        if v_from is None or v_to is None:
            return {"wh": None, "reason": "no pack voltage"}
        if v_to <= v_from:
            return {"wh": 0, "nights": None, "source": "at or below the floor",
                    "from_v": v_from, "to_v": v_to}
        if not self._discharge_nights(now):
            return {"wh": None,
                    "reason": "no overnight discharge on record yet; run "
                              "scrape_gateway.py --backfill"}
        tried = []
        for nights, label in self._night_tiers(now, month):
            if len(nights) < MIN_NIGHTS_PER_TIER and label != "all history":
                tried.append(f"{label}: {len(nights)} night"
                             f"{'' if len(nights) == 1 else 's'}")
                continue
            total, fewest, gap = self._sum_bins(self._energy_bins(nights),
                                                v_from, v_to)
            if gap:
                tried.append(f"{label}: {gap}")
                continue
            return {"wh": round(total), "nights": fewest, "source": label,
                    "from_v": v_from, "to_v": v_to}
        return {"wh": None, "reason": "; ".join(tried)}

    def soc_disagreement(self, soc_pct, voltage, floor_v=52.0, now=None):
        """How far the Battery Monitor's state of charge sits above the curve.

        Two answers to one question - how many watt-hours are in the pack
        above the floor. One is the shunt's percentage against a capacity
        derived from the same shunt; the other is what the house has actually
        taken out between those voltages on nights the pack was doing nothing
        else. Nothing decides on the first any more, which is exactly why it
        is worth watching: a shunt that has drifted is invisible once you
        stop believing it.

        {"implied_wh", "learned_wh", "excess"} where excess is the fraction
        by which the shunt is the higher of the two, or None when either side
        cannot be computed.
        """
        now = int(now or time.time())
        if soc_pct is None or voltage is None or voltage <= floor_v:
            return None
        soc_floor = self.soc_for_voltage(floor_v)
        capacity = self.capacity_wh()
        if soc_floor is None or capacity is None:
            return None
        learned = self.energy_above(floor_v, voltage, now=now)
        if learned.get("wh") is None or learned["wh"] <= 0:
            return None
        implied = (soc_pct - soc_floor) / 100.0 * capacity
        return {"implied_wh": round(implied), "learned_wh": learned["wh"],
                "excess": implied / learned["wh"] - 1.0,
                "nights": learned["nights"], "source": learned["source"],
                "floor_v": floor_v, "voltage": voltage, "soc_pct": soc_pct}

    def energy_curve_status(self):
        """What the curve covers, for diagnostics and the plan record."""
        now = int(time.time())
        nights = self._discharge_nights(now)
        bins = self._energy_bins(nights)
        return {
            "nights": len(nights),
            "bins": len(bins),
            "volts_low": round(min(bins) * ENERGY_BIN_V, 2) if bins else None,
            "volts_high": round((max(bins) + 1) * ENERGY_BIN_V, 2) if bins else None,
            "bin_v": ENERGY_BIN_V,
        }

    # --- solar --------------------------------------------------------------

    def solar_model(self, month=None, now=None):
        """Solar yield against cloud cover for one month.

        Fits Wh = clear * (1 - k * cloud/100) by least squares over days that
        have both a measured yield and an archived cloud mean.
        """
        now = int(now or time.time())
        month = month or history.local(now, self.cfg).month
        rows = self.conn.execute(
            "SELECT day, solar_wh FROM daily WHERE solar_wh IS NOT NULL "
            "AND solar_wh > 0 ORDER BY day").fetchall()
        days = [r for r in rows
                if datetime.strptime(r["day"], "%Y-%m-%d").month == month]
        if len(days) < MIN_DAYS_FOR_SOLAR_FIT:
            return {"month": month, "days": len(days), "learned": False}

        arch = weather.archive_daily(self.cfg, days[0]["day"], days[-1]["day"])
        pairs = [(arch[r["day"]]["cloud"], r["solar_wh"])
                 for r in days if r["day"] in arch]
        if len(pairs) < MIN_DAYS_FOR_SOLAR_FIT:
            return {"month": month, "days": len(pairs), "learned": False}

        # Clear-day reference: the best yield among the least cloudy quarter.
        pairs.sort(key=lambda p: p[0])
        clear_pool = [wh for _, wh in pairs[:max(2, len(pairs) // 4)]]
        clear = max(clear_pool)

        xs = [c / 100.0 for c, _ in pairs]
        ys = [wh / clear for _, wh in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
                 if denom else 0.0)
        # Derating must be non-negative and cannot exceed total loss.
        k = min(1.0, max(0.0, -slope))
        return {"month": month, "days": len(pairs), "learned": True,
                "clear_day_wh": round(clear), "cloud_derate": round(k, 3)}

    def remaining_solar_wh(self, now=None, until=None):
        """Wh of solar still expected today, from now to sunset.

        The same walk project_voltage does, so the two cannot disagree about
        how much of the day is left. None until the solar model is learned.
        """
        now = int(now or time.time())
        m = self.solar_model(now=now)
        if not m.get("learned"):
            return None
        forecast = weather.hourly(self.cfg, hours=24, now=now)
        if not forecast:
            return None
        day_rad = sum(f["radiation"] for f in forecast[:24]) or 1
        per_unit = m["clear_day_wh"] / day_rad
        today = history.local_day(now, self.cfg)
        total = 0.0
        for f in forecast:
            if f["ts"] < now - 3600 or history.local_day(f["ts"], self.cfg) != today:
                continue
            if until is not None and f["ts"] >= until:
                continue
            total += f["radiation"] * per_unit
        return round(total)

    def estimate_solar_wh(self, cloud_pct, month=None, now=None):
        m = self.solar_model(month, now)
        if not m.get("learned"):
            return None
        est = m["clear_day_wh"] * (1 - m["cloud_derate"] * cloud_pct / 100.0)
        return {"wh": round(max(0.0, est)), "clear_day_wh": m["clear_day_wh"],
                "cloud_pct": cloud_pct, "days": m["days"]}

    # --- generators ---------------------------------------------------------

    def charge_rate(self, gen=None, solo=None, days=180, now=None):
        """What a generator delivers, gross, in watts.

        Gross is what came out of the engine: what went into the pack plus
        what the house took at the same minute. That is a property of the
        generator. The net figure the shunt alone reports is not - it is the
        generator minus whatever the house happened to be doing, which is why
        the MEP's 20:09 run through a 7 kW steam bath once measured as
        0.864 V/h and a Kubota top-up was sized at the pair's 214 A.

        Learning gross means a run under an unusual load is no longer a run to
        throw away: subtracting the load it actually faced recovers the same
        figure as any other run. There is no load filter any more.

        Exercise runs are still excluded: 30 minutes at 09:00 with the sun up
        says nothing about lifting the pack at night.

        The Kubota's runs also drive a Magnum MS4048, which is not on Modbus.
        Gross is measured at the shunt and at the inverters' load output, so
        it includes whatever the Magnum put in without needing to see it.

        `gen` of None pools every generator's runs, which is how the two of
        them running together are measured.
        """
        now = int(now or time.time())
        sql = ("SELECT gen, rate_v_per_h, rate_a, load_w, gross_w, duration_min "
               "FROM gen_runs WHERE kind != 'exercise' AND gross_w IS NOT NULL "
               "AND start_ts >= ?")
        args = [now - days * 86400]
        if gen is not None:
            sql += " AND gen=?"
            args.append(gen)
        if solo is not None:
            sql += " AND solo=?"
            args.append(int(solo))
        rows = [r for r in self.conn.execute(sql, args).fetchall()
                if r["duration_min"] >= MIN_RUN_MINUTES and r["gross_w"] > 0]
        if not rows:
            return None

        gross = _median([r["gross_w"] for r in rows])
        loads = [r["load_w"] for r in rows if r["load_w"] is not None]
        observed_v = [r["rate_v_per_h"] for r in rows if r["rate_v_per_h"] is not None]
        return {
            "gen": gen, "solo": solo, "runs": len(rows),
            "gross_w": round(gross),
            "capacity_wh": self.capacity_wh(),
            "run_load_w": round(_median(loads)) if loads else None,
            # Recorded because they happened. Nothing plans from either: both
            # are the generator minus the house on the day.
            "observed_net_a": (round(_median([r["rate_a"] for r in rows
                                              if r["rate_a"] is not None]), 1)
                               if any(r["rate_a"] is not None for r in rows)
                               else None),
            "observed_v_per_h": (round(_median(observed_v), 3)
                                 if observed_v else None),
        }

    def expected_load_w(self, now=None, hours=2.0):
        """Mean house load over the next `hours`, from the learned profile.

        The profile is by hour of day and weekday against weekend, so a run
        starting at nine on a Saturday is netted against a Saturday evening
        rather than against the year's average.
        """
        now = int(now or time.time())
        tz = history.tzinfo(self.cfg)
        whole = max(1, int(round(hours)))
        seen = []
        for i in range(whole):
            t = datetime.fromtimestamp(now + i * 3600, tz)
            p = self.load_profile(month=t.month, weekend=t.weekday() >= 5, now=now)
            wh = p["profile"].get(t.hour)
            if wh is None:
                wh = _median(list(p["profile"].values()))
            if wh is not None:
                seen.append(wh)
        return round(sum(seen) / len(seen)) if seen else None

    def net_from_gross(self, rate, expected_load_w):
        """(net watts into the pack, %SOC per hour) for a rate and a load.

        None where the generator cannot keep up with the load the window
        expects: there is no charge rate to quote when nothing is going in.
        """
        if not rate or rate.get("gross_w") is None:
            return None, None
        if rate.get("assumed_net"):
            net = rate["gross_w"]            # the assumption is already net
        elif expected_load_w is None:
            return None, None
        else:
            net = rate["gross_w"] - expected_load_w
        if net <= 0:
            return net, None
        capacity = rate.get("capacity_wh") or self.capacity_wh()
        return net, (round(100.0 * net / capacity, 2) if capacity else None)

    def charge_rates(self, now=None):
        out = {}
        for gen in history.GENS:
            for solo in (True, False):
                r = self.charge_rate(gen, solo=solo, now=now)
                if r:
                    out[f"{gen}_{'solo' if solo else 'paired'}"] = r
            r = self.charge_rate(gen, solo=None, now=now)
            if r:
                out[gen] = r
        both = self.charge_rate(None, solo=False, now=now)
        if both:
            out["both_running"] = both
        return out

    def mean_load_w(self, now=None):
        """The house's mean load in W, from the learned hourly profile.

        Wh in an hour is W, so the mean of the profile's cells is the mean
        load. None until the profile has been learned, and a rate computed
        without it says so rather than quietly skipping the spike filter.
        """
        now = int(now or time.time())
        p = self.load_profile(month=history.local(now, self.cfg).month)
        values = list(p["profile"].values())
        return sum(values) / len(values) if values else None

    # --- battery ------------------------------------------------------------

    def _live_soc_counts(self, v_bin_lo, v_bin_hi):
        """{(v_bin, soc): n} from live samples, filtered as before."""
        lo = history.soc_bin_volts(v_bin_lo) - history.SOC_BIN_V / 2
        hi = history.soc_bin_volts(v_bin_hi) + history.SOC_BIN_V / 2
        rows = self.conn.execute(
            "SELECT battery_v, batt_soc FROM samples WHERE battery_v BETWEEN ? AND ? "
            "AND batt_soc IS NOT NULL AND batt_power < 0 "
            "AND mep_action != ? AND kub_action != ?",
            (lo, hi, history.GEN_RUNNING, history.GEN_RUNNING)).fetchall()
        counts = {}
        for r in rows:
            key = (history.soc_bin(r["battery_v"]), int(round(r["batt_soc"])))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def soc_observations(self, v_bin_lo, v_bin_hi):
        """{(v_bin, soc): n} over a bin range, from both sources.

        Live samples and the backfilled histogram are pooled: they are the
        same measurement from the same shunt, so weighting them equally is
        right, and the backfill is what makes a rarely-seen voltage like the
        start threshold learnable at all.
        """
        counts = dict(self._live_soc_counts(v_bin_lo, v_bin_hi))
        for r in history.soc_histogram(self.conn, v_bin_lo, v_bin_hi):
            key = (r["v_bin"], r["soc"])
            counts[key] = counts.get(key, 0) + r["n"]
        return counts

    def voltage_soc_curve(self, min_observations=MIN_SOC_OBSERVATIONS):
        """The learned mapping, as [(volts, soc, observations)] by voltage.

        One point per voltage bin that has enough observations, each the
        weighted median SOC seen at that voltage, then forced non-decreasing
        so surface charge cannot make the curve turn back on itself.
        """
        counts = self.soc_observations(history.soc_bin(CURVE_V_LOW),
                                       history.soc_bin(CURVE_V_HIGH))
        by_bin = {}
        for (v_bin, soc), n in counts.items():
            by_bin.setdefault(v_bin, []).append((soc, n))
        out = []
        for v_bin in sorted(by_bin):
            pairs = by_bin[v_bin]
            total = sum(n for _, n in pairs)
            if total < min_observations:
                continue
            out.append((history.soc_bin_volts(v_bin),
                        _weighted_median(pairs), total))
        return _isotonic(out)

    def soc_for_voltage(self, target_v, min_observations=MIN_SOC_OBSERVATIONS):
        """SOC at a given resting-discharge voltage.

        Only readings with the pack discharging and no generator running are
        used: a charging pack sits well above its resting voltage.

        The exact voltage is often thinly observed - the pack passes through
        the start threshold rarely - so this reads the whole learned curve and
        interpolates, rather than requiring enough samples in one narrow bin.
        """
        return self._interpolate(self.voltage_soc_curve(min_observations), target_v)

    @staticmethod
    def _interpolate(curve, target_v):
        if not curve:
            return None
        volts = [v for v, _, _ in curve]
        socs = [s for _, s, _ in curve]
        if target_v <= volts[0]:
            # Extrapolating below the lowest voltage ever seen would be a
            # guess; allow only the half-bin rounding margin.
            return socs[0] if volts[0] - target_v <= history.SOC_BIN_V else None
        if target_v >= volts[-1]:
            return socs[-1] if target_v - volts[-1] <= history.SOC_BIN_V else None
        for i in range(1, len(volts)):
            if volts[i] >= target_v:
                v0, v1 = volts[i - 1], volts[i]
                s0, s1 = socs[i - 1], socs[i]
                if v1 == v0:
                    return s1
                return s0 + (s1 - s0) * (target_v - v0) / (v1 - v0)
        return socs[-1]

    def soc_curve_status(self):
        """What the curve knows, for the plan record and diagnostics."""
        span = history.soc_curve_span(self.conn)
        curve = self.voltage_soc_curve()
        start_v = self.cfg["default_start"]
        at_start = self._interpolate(curve, start_v)
        return {
            "points": len(curve),
            "volts_low": round(curve[0][0], 2) if curve else None,
            "volts_high": round(curve[-1][0], 2) if curve else None,
            "observations": sum(n for _, _, n in curve),
            "scraped_observations": span[2] if span else 0,
            "start_threshold_v": start_v,
            "soc_at_start_threshold": (round(at_start, 1)
                                       if at_start is not None else None),
        }

    def capacity_ah(self):
        """Pack capacity in Ah, from the monitor's own Ah-remaining vs SOC."""
        rows = self.conn.execute(
            "SELECT batt_ah_remaining, batt_soc FROM samples "
            "WHERE batt_ah_remaining IS NOT NULL AND batt_soc > 5 "
            "ORDER BY ts DESC LIMIT 2000").fetchall()
        est = [r["batt_ah_remaining"] / (r["batt_soc"] / 100.0)
               for r in rows if r["batt_soc"]]
        if len(est) < 10:
            return None
        return round(_median(est))

    def volts_for_soc(self, target_soc):
        """The inverse of the learned resting curve: the voltage at a given SOC."""
        return _invert(self.voltage_soc_curve(), target_soc)

    def capacity_wh(self):
        """Usable pack size, from the Battery Monitor's own Ah-remaining vs SOC."""
        rows = self.conn.execute(
            "SELECT batt_ah_remaining, batt_soc, battery_v FROM samples "
            "WHERE batt_ah_remaining IS NOT NULL AND batt_soc > 5 "
            "ORDER BY ts DESC LIMIT 2000").fetchall()
        est = [r["batt_ah_remaining"] / (r["batt_soc"] / 100.0) * r["battery_v"]
               for r in rows if r["batt_soc"] and r["battery_v"]]
        if len(est) < 10:
            return None
        return round(_median(est))

    def project_voltage(self, target_v, now=None, hours=24):
        """When the pack is expected to fall to target_v.

        Walks forward hour by hour, draining the forecast load and adding back
        the forecast solar, until the charge left equals what the pack holds at
        target_v. Returns None with a reason when the inputs are not learned.
        """
        now = int(now or time.time())
        sample = history.latest_sample(self.conn, at=self._at(now))
        if not sample or sample["battery_v"] is None:
            return {"reached": None, "reason": "no battery sample"}

        # The same question the deficit asks, off the same curve, so the
        # projection and the deficit cannot disagree about what the pack is
        # holding tonight.
        holds = self.energy_above(target_v, sample["battery_v"], now=now)
        if holds.get("wh") is None:
            return {"reached": None,
                    "reason": f"the learned Wh-vs-V curve cannot say what the "
                              f"pack holds above {target_v:.1f} V "
                              f"({holds['reason']})"}
        available_wh = holds["wh"]
        if available_wh <= 0:
            # There is nothing left above the target, so the answer is "now",
            # not an unknown. Leaving `at` unset printed "?" from 03:10 onward
            # on the first live night, exactly when the number mattered most.
            return {"reached": now, "at": "now", "hours": 0.0,
                    "reason": "already at or below target",
                    "voltage": sample["battery_v"],
                    "available_source": f"learned Wh-vs-V, {holds['nights']} nights",
                    "available_wh": available_wh}

        forecast = weather.hourly(self.cfg, hours=hours, now=now)
        solar_by_ts = {f["ts"]: f for f in forecast}
        month = history.local(now, self.cfg).month
        sm = self.solar_model(month, now)
        # Wh per hour of sun at this site, scaled from the learned clear day.
        peak_hourly = None
        if sm.get("learned"):
            day_rad = sum(f["radiation"] for f in forecast[:24]) or 1
            peak_hourly = sm["clear_day_wh"] / day_rad

        remaining = available_wh
        for i in range(hours):
            ts = history.hour_floor(now) + i * 3600
            t = datetime.fromtimestamp(ts, history.tzinfo(self.cfg))
            p = self.load_profile(month=t.month, weekend=t.weekday() >= 5)
            load_wh = p["profile"].get(t.hour) or _median(list(p["profile"].values()))
            if load_wh is None:
                return {"reached": None, "reason": "load profile not learned"}
            solar_wh = 0.0
            f = solar_by_ts.get(ts)
            if f and peak_hourly:
                solar_wh = f["radiation"] * peak_hourly
            net = load_wh - solar_wh
            if net <= 0:
                continue
            if remaining <= net:
                frac = remaining / net
                reached = ts + int(frac * 3600)
                return {"reached": reached,
                        "at": self._at_label(reached, now),
                        "hours": round(max(0.0, reached - now) / 3600.0, 2),
                        "voltage": sample["battery_v"],
                        "available_source": f"learned Wh-vs-V, "
                                            f"{holds['nights']} nights",
                        "available_wh": available_wh}
            remaining -= net
        return {"reached": None, "reason": f"not reached within {hours} h",
                "voltage": sample["battery_v"],
                "available_source": f"learned Wh-vs-V, {holds['nights']} nights",
                "available_wh": available_wh}

    # --- what a generator can do in a run window ----------------------------

    def charge_curve(self, gen=None, solo=None, days=180, now=None):
        """The charge-side curve for one generator, or for the pair.

        Built from the minute samples inside real runs. Runs taken under an
        exceptional house load are left out for the same reason they are left
        out of the rate: the terminal voltage they show is the house's, not
        the generator's.
        """
        now = int(now or time.time())
        rows = history.charge_samples(self.conn, gen=gen, solo=solo,
                                      since=now - days * 86400,
                                      until=now if self.as_of else None)
        traces, counts = {}, {}
        for r in rows:
            t = traces.get(r["run_id"])
            if t is None:
                # A run's own load and gross travel with it, so its timings
                # can be read against a different load rather than discarded
                # for having faced an unusual one.
                t = traces[r["run_id"]] = {"start_v": r["start_v"], "trace": {},
                                           "minutes": 0.0, "max_v": r["battery_v"],
                                           "load_w": r["load_w"],
                                           "gross_w": r["gross_w"]}
            v_bin = history.soc_bin(r["battery_v"])
            minutes = (r["ts"] - r["start_ts"]) / 60.0
            t["minutes"] = max(t["minutes"], minutes)
            t["max_v"] = max(t["max_v"], r["battery_v"])
            # First arrival: the pack passes a voltage once on the way up.
            if v_bin not in t["trace"] or minutes < t["trace"][v_bin]:
                t["trace"][v_bin] = minutes
            if r["batt_soc"] is not None:
                key = (v_bin, int(round(r["batt_soc"])))
                counts[key] = counts.get(key, 0) + 1

        by_bin = {}
        for (v_bin, soc), n in counts.items():
            by_bin.setdefault(v_bin, []).append((soc, n))
        points = []
        for v_bin in sorted(by_bin):
            pairs = by_bin[v_bin]
            points.append((history.soc_bin_volts(v_bin),
                           _weighted_median(pairs), sum(n for _, n in pairs)))
        return ChargeCurve(gen, solo, list(traces.values()), _isotonic(points))

    def _rate_for(self, gen, solo, now):
        """This generator's own rate, or a conservative assumption.

        Never another generator's, and never the paired figure. A paired run
        measures the pack with both engines and a Magnum on it; using it to
        size a Kubota run on its own is how last night's top-up was sized at
        214 A, ran its full two hours and never reached its stop. An
        assumption that is too low costs a longer run; one that is too high
        costs a night.
        """
        rate = self.charge_rate(gen, solo=solo, now=now)
        if rate is not None:
            return rate
        assumed = self.cfg.get("assumed_charge_a") or {}
        amps = (sum(assumed.get(g, 0) for g in history.GENS) if gen is None
                else assumed.get(gen))
        if not amps:
            return None
        # The configured assumption is a conservative net figure - it already
        # allows for an ordinary house - so it is not netted a second time.
        volts = self.cfg.get("default_stop", 56.0) - 3.0
        return {"gen": gen, "solo": solo, "runs": 0,
                "gross_w": round(float(amps) * volts),
                "capacity_wh": self.capacity_wh(),
                "assumed": True, "assumed_net": True}

    def hours_to_target(self, from_v, target_v, rate):
        """Hours for `rate` to lift the pack from from_v to target_v.

        Amps become a state-of-charge rate against the learned capacity, and
        the learned curve says what SOC the target voltage corresponds to.
        That curve is built from resting discharge readings, so a charging
        pack shows the target voltage at a lower SOC than it names and reaches
        it sooner. The answer is therefore an over-estimate, which is the safe
        direction for a run window.
        """
        if not rate or not rate.get("soc_per_h"):
            return None
        soc_target = self.soc_for_voltage(target_v)
        if soc_target is None:
            return None
        soc_now = self.soc_for_voltage(from_v)
        if soc_now is None:
            return None
        if soc_target <= soc_now:
            return 0.0
        return (soc_target - soc_now) / rate["soc_per_h"]

    def reach(self, gen, from_v, target_v, window_h, solo=None, now=None):
        """Whether `gen` can lift the pack to target_v inside window_h.

        One place, so the guard's refusal and the POLICY detail can never
        quote different arithmetic for the same question.

        Where the pack stands is read from `from_v` against a learned curve
        and never from the Battery Monitor's live state of charge. A shunt
        reading high says a target is nearer than it is, and prices a run
        short at the moment the run is being decided on.

        Three ways of answering, best evidence first. What the pack was
        observed to do between these two voltages while charging; failing
        that, the charge-side curve against the observed current; and only
        when a generator has fewer than three runs on record, the resting
        curve, which is what the Pi5 stops on only after the charge is over
        and so reads a target as harder than it is. `basis` says which.
        """
        now = int(now or time.time())
        curve = self.charge_curve(gen, solo=solo, now=now)
        expected_load = self.expected_load_w(now=now, hours=window_h)
        base = {"gen": gen, "window_h": window_h, "target_v": target_v,
                "curve": curve, "expected_load_w": expected_load}

        short = curve.fell_short(from_v, target_v, window_h)
        if short:
            n = len(short)
            best = max(t["max_v"] for t in short)
            basis = (f"observed while charging ({curve.label}): "
                     f"{n} run{'' if n == 1 else 's'} had the window and "
                     f"stopped at {best:.1f}")
            return dict(base, ok=False, rate=None, hours=None, basis=basis,
                        why=f"{target_v:.1f} was not reached — {basis}")

        if curve.learned:
            observed = curve.minutes_between(from_v, target_v,
                                             expected_load_w=expected_load)
            if observed is not None:
                minutes, n = observed
                hours = minutes / 60.0
                basis = f"observed while charging ({curve.label})"
                ok = hours <= window_h + 1e-9
                return dict(base, ok=ok, hours=hours, rate=None, basis=basis,
                            why=(f"{target_v:.1f} reachable in {hours:.1f} h, "
                                 f"{basis}" if ok else
                                 f"{target_v:.1f} took {hours:.1f} h {basis} "
                                 f"but the run window is {window_h:.1f} h"))

        rate = self._rate_for(gen, solo, now)
        net_w, soc_per_h = self.net_from_gross(rate, expected_load)
        if rate is None:
            return dict(base, ok=False, rate=rate, hours=None, basis=None,
                        why=f"no observed charge rate for {gen}, so "
                            f"{target_v:.1f} V cannot be shown to be reachable")
        if net_w is not None and net_w <= 0:
            return dict(base, ok=False, rate=rate, hours=None, basis=None,
                        net_w=net_w,
                        why=f"{gen} delivers {_kw(rate['gross_w'])} and the "
                            f"house is expected to take {_kw(expected_load)}, "
                            f"so nothing would go into the pack")
        if soc_per_h is None:
            missing = ("the pack capacity is not learned"
                       if expected_load is not None
                       else "the load profile is not learned")
            return dict(base, ok=False, rate=rate, hours=None, basis=None,
                        why=f"{missing}, so {target_v:.1f} V cannot be shown "
                            f"to be reachable for {gen}")
        rate = dict(rate, soc_per_h=soc_per_h, net_w=net_w)

        # A learned charge curve still cannot speak for a voltage no run has
        # ever reached, and 57.0 is exactly that until a run is allowed to go
        # there. What is left is an estimate, said out loud as one.
        soc_target = curve.soc_for_voltage(target_v) if curve.learned else None
        if soc_target is not None:
            basis = f"charging curve ({curve.label})"
        else:
            delta, basis = self.estimated_soc_delta(from_v, target_v, curve)
            if delta is None:
                return dict(base, ok=False, rate=rate, hours=None, basis=None,
                            why=f"{target_v:.1f} V cannot be shown to be "
                                f"reachable: {basis}")
            soc_target = None
        hours = (delta / soc_per_h if soc_target is None
                 else self._hours_from(soc_target, from_v, rate))
        if hours is None:
            return dict(base, ok=False, rate=rate, hours=None, basis=basis,
                        why=f"neither the charging nor the resting curve "
                            f"reaches {target_v:.1f} V, so it cannot be shown "
                            f"to be reachable")
        ok = hours <= window_h + 1e-9
        phrase = rate_phrase(rate, expected_load, net_w, soc_per_h)
        return dict(base, ok=ok, rate=rate, hours=hours, basis=basis,
                    net_w=net_w,
                    why=(f"{target_v:.1f} reachable in {hours:.1f} h at "
                         f"{phrase}, {basis}" if ok else
                         f"{target_v:.1f} needs {hours:.1f} h at "
                         f"{phrase} but the run window is "
                         f"{window_h:.1f} h, {basis}"))

    def estimated_soc_delta(self, from_v, target_v, curve):
        """(points of charge between two voltages, basis), one of them unseen.

        This is what printed "56.1 reachable in 0.0 h" three times on
        2026-08-30, for a Kubota that had never once reached 56.1 V. The
        target was read off the resting curve, which is built from a settled
        pack, while the starting point was the shunt's live reading taken
        during a charge - and a charging pack reads several points of charge
        higher at the same voltage. The pack was therefore already "past" a
        voltage it had never been to, the delta came out negative, and the
        run was priced at nothing.

        The two ends are taken off one curve now, so the offset that made
        them incomparable cancels: the charging curve's own points where it
        is learned, the resting curve otherwise, both carried above their
        highest reading on the slope of their top segment. That is an
        estimate and the basis says so, but the deficit asking for the target
        is real, and an estimate stated as one is a better answer to it than
        an arithmetic accident.
        """
        points = curve.soc_points if curve.learned else self.voltage_soc_curve()
        if not points:
            return None, "no voltage-to-charge curve has been learned yet"
        lo = self._carried(points, from_v)
        hi = self._carried(points, target_v)
        if lo is None or hi is None:
            return None, (f"the curve cannot be carried from {from_v:.1f} V to "
                          f"{target_v:.1f} V")
        if hi > 100.0:
            return None, (f"carrying the curve to {target_v:.1f} V puts it past "
                          f"a full pack ({hi:.0f}%)")
        if hi <= lo:
            return None, (f"the curve is flat from {from_v:.1f} V to "
                          f"{target_v:.1f} V, so charge is not what the "
                          f"difference is made of")
        head = ("estimated, charging curve has no run to this voltage"
                if curve.learned else
                f"estimated from the resting curve, {curve.runs} charging "
                f"run{'' if curve.runs == 1 else 's'} on record")
        return hi - lo, (f"{head} ({lo:.0f}% at {from_v:.1f} V to {hi:.0f}% at "
                         f"{target_v:.1f} V)")

    @classmethod
    def _carried(cls, points, v):
        """A curve's state of charge at `v`, carried above its highest reading.

        Inside the curve this is plain interpolation. Above it the top
        segment's slope is extended, which is the only honest thing left to
        do for a voltage nothing has recorded - and it is refused outright
        where the top is flat, because a flat top means the pack is full up
        there and the voltage is being made by current against internal
        resistance, which a state-of-charge model has nothing to say about.
        """
        inside = cls._interpolate(points, v)
        if inside is not None:
            return inside
        top_v, top_soc = points[-1][0], points[-1][1]
        if v < top_v:
            return None
        slope = _top_slope(points)
        if slope is None:
            return None
        return top_soc + slope * (v - top_v)

    def _hours_from(self, soc_target, from_v, rate):
        if soc_target is None:
            return None
        soc_now = self.soc_for_voltage(from_v)
        if soc_now is None:
            return None
        if soc_target <= soc_now:
            return 0.0
        return (soc_target - soc_now) / rate["soc_per_h"]

    def voltage_after(self, from_v, hours, rate, curve=None, soc_per_h=None):
        """The voltage the pack reaches after `hours` at this rate."""
        per_h = soc_per_h if soc_per_h is not None else (rate or {}).get("soc_per_h")
        if not rate or not per_h:
            return None
        soc = self.soc_for_voltage(from_v)
        if soc is None:
            return None
        soc = min(100.0, soc + per_h * hours)
        if curve is not None and curve.learned:
            return curve.volts_for_soc(soc)
        return self.volts_for_soc(soc)

    def best_reachable_target(self, gen, from_v, window_h, ceiling, floor,
                              step=0.5, solo=None, now=None):
        """The highest stop voltage reachable in the window, rounded down.

        Read off the same evidence reach() uses, so a rule that is told 57.0
        is out of range is not then handed a lower target from a different
        curve. None when even `floor` is out of reach, so a caller cannot
        propose a target that is not worth running for.
        """
        now = int(now or time.time())
        curve = self.charge_curve(gen, solo=solo, now=now)
        expected_load = self.expected_load_w(now=now, hours=window_h)
        v = (curve.voltage_after(from_v, window_h, expected_load_w=expected_load)
             if curve.learned else None)
        if v is None:
            rate = self._rate_for(gen, solo, now)
            _, soc_per_h = self.net_from_gross(rate, expected_load)
            v = self.voltage_after(from_v, window_h, rate,
                                   curve=curve, soc_per_h=soc_per_h)
        if v is None:
            return None
        v = math.floor(min(v, ceiling) / step) * step
        return round(v, 2) if v >= floor - 1e-9 else None

    def _at_label(self, reached, now):
        """How a projected time is written. Never "?"."""
        if reached <= now:
            return "now"
        if reached - now <= PROJECTION_SOON_SECONDS:
            return f"≤ {PROJECTION_SOON_SECONDS // 60} min"
        return history.clock(reached, self.cfg)

    def projection_label(self, projection, now=None):
        """The display string for a projection, whatever shape it arrived in."""
        p = projection or {}
        if not p.get("reached"):
            return None
        return p.get("at") or self._at_label(p["reached"], int(now or time.time()))

    # --- the overnight deficit (POLICY 4) ----------------------------------

    def _drain_wh(self, now, until, hours=24):
        """Wh the house takes out of the pack between now and `until`.

        The same hour-by-hour walk project_voltage does, load against the
        forecast solar, so the two cannot disagree about the night ahead. An
        hour where solar beats the load is not counted as a credit: it fills
        the pack it was going to fill either way.
        """
        forecast = weather.hourly(self.cfg, hours=hours, now=now)
        solar_by_ts = {f["ts"]: f for f in forecast}
        sm = self.solar_model(now=now)
        per_unit = None
        if sm.get("learned") and forecast:
            day_rad = sum(f["radiation"] for f in forecast[:24]) or 1
            per_unit = sm["clear_day_wh"] / day_rad
        total = 0.0
        for i in range(hours):
            ts = history.hour_floor(now) + i * 3600
            if ts >= until:
                break
            t = datetime.fromtimestamp(ts, history.tzinfo(self.cfg))
            p = self.load_profile(month=t.month, weekend=t.weekday() >= 5, now=now)
            load_wh = p["profile"].get(t.hour) or _median(list(p["profile"].values()))
            if load_wh is None:
                return None, None
            solar_wh = 0.0
            f = solar_by_ts.get(ts)
            if f and per_unit:
                solar_wh = f["radiation"] * per_unit
            total += max(0.0, load_wh - solar_wh)
        return round(total), p.get("source")

    def overnight_deficit(self, until, floor_v=52.0, now=None):
        """How many Wh short the pack is of reaching `until` above floor_v.

        Positive means the night cannot be got through on what the pack
        holds, and by how much. Negative means it can, with that much to
        spare. None with a reason where the inputs are not learned.
        """
        now = int(now or time.time())
        if not until or until <= now:
            return {"deficit_wh": None, "reason": "no sunrise to reach"}
        sample = history.latest_sample(self.conn, at=self._at(now))
        if not sample or sample["battery_v"] is None:
            return {"deficit_wh": None, "reason": "no battery sample"}
        capacity = self.capacity_wh()
        if capacity is None:
            return {"deficit_wh": None, "reason": "pack capacity not learned"}
        needed, source = self._drain_wh(now, until)
        if needed is None:
            return {"deficit_wh": None, "reason": "load profile not learned"}

        # What the pack holds above the floor, from what the house has
        # actually taken out between those two voltages - not from the
        # Battery Monitor's state of charge, which is a reading of the shunt
        # and was being multiplied by a capacity derived from the same shunt.
        holds = self.energy_above(floor_v, sample["battery_v"], now=now)
        if holds.get("wh") is None:
            return {"deficit_wh": None,
                    "reason": f"the learned Wh-vs-V curve cannot say what the "
                              f"pack holds above {floor_v:.1f} V "
                              f"({holds['reason']})"}
        available = holds["wh"]
        return {"deficit_wh": round(needed - available),
                "needed_wh": needed, "available_wh": available,
                "available_source": f"learned Wh-vs-V, {holds['nights']} nights",
                "available_tier": holds["source"],
                "hours": round((until - now) / 3600.0, 1),
                "voltage": sample["battery_v"],
                # Display only. The Battery Monitor's own figure is kept
                # beside the answer so the two can be compared, and is used
                # in no part of it.
                "soc_now_display": sample["batt_soc"],
                "capacity_wh": capacity, "floor_v": floor_v, "source": source}

    def topup_target(self, deficit_wh, margin_pct, from_v, capacity_wh,
                     low, high, gen=None, solo=None, now=None):
        """The stop voltage that puts the deficit, plus its margin, in the pack.

        The stop is a terminal voltage read while charging, so the charge-side
        curve is what turns the state of charge wanted into the voltage to
        stop at; the resting curve stands in until that is learned, and says
        so. Rounded up to the half volt - the cost of a little too much is
        minutes of run time, and of too little is not getting through the
        night - then clamped.
        """
        # Where the pack stands comes from its voltage through the learned
        # curve, never from the Battery Monitor's live state of charge: a
        # shunt reading high would set a target lower than the deficit needs.
        soc_now = self.soc_for_voltage(from_v) if from_v is not None else None
        if not capacity_wh or soc_now is None:
            return None
        padded = deficit_wh * (1.0 + margin_pct / 100.0)
        target_soc = min(100.0, soc_now + padded / capacity_wh * 100.0)
        curve = self.charge_curve(gen, solo=solo, now=now)
        if curve.learned:
            volts, basis = curve.volts_for_soc(target_soc), f"charging curve ({curve.label})"
        else:
            volts, basis = self.volts_for_soc(target_soc), "resting curve"
        if volts is None:
            return None
        volts = math.ceil(round(volts, 6) / 0.5) * 0.5
        return {"volts": round(min(high, max(low, volts)), 2),
                "uncapped_volts": round(volts, 2),
                "padded_wh": round(padded), "target_soc": round(target_soc, 1),
                "basis": basis}

    # --- data coverage (guard rule 6) --------------------------------------

    def learning_status(self, now=None):
        """Whether history is deep enough for the guard to permit writes.

        Two conditions, both from SPEC section 7 rule 6:
          - `hourly` holds the current calendar month from some earlier year
          - `samples` covers at least learning_live_days consecutive days
        """
        now = int(now or time.time())
        this_month = history.local(now, self.cfg).month
        this_year = history.local(now, self.cfg).year

        prior_years = set()
        for r in self.conn.execute("SELECT DISTINCT hour_ts FROM hourly"):
            t = history.local(r["hour_ts"], self.cfg)
            if t.month == this_month and t.year < this_year:
                prior_years.add(t.year)

        days = sorted({history.local_day(r["ts"], self.cfg) for r in
                       self.conn.execute("SELECT ts FROM samples ORDER BY ts")})
        streak = best = 0
        prev = None
        for d in days:
            cur = datetime.strptime(d, "%Y-%m-%d").date()
            streak = streak + 1 if prev and (cur - prev).days == 1 else 1
            best = max(best, streak)
            prev = cur

        need = self.cfg["learning_live_days"]
        return {
            "prior_year_months": sorted(prior_years),
            "has_prior_year": bool(prior_years),
            "live_days": best,
            "live_days_required": need,
            "has_live_days": best >= need,
            "open": bool(prior_years) and best >= need,
        }
