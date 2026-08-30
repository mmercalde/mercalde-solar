"""Statistical model of the house, learned from history. Pure Python, no LLM.

SPEC section 5:
  - hourly load profile by hour-of-day and weekday/weekend, seasonal by month
  - overnight drawdown Wh (sunset -> sunrise) by month
  - solar yield vs cloud cover, learned per month
  - charge rate per generator, solo and paired
  - excludes every hour a generator was running, exercise runs included

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
# A run taken while the house drew more than this multiple of its mean load
# says more about the load than about the generator, so it is left out of the
# learned rate. The 20:09 MEP run on the first live night was one of these: a
# 7 kW steam bath against a mean nearer 1.5 kW.
LOAD_SPIKE_MULTIPLE = 2.0
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


def rate_phrase(rate):
    """How a charge rate is written wherever the owner or the model sees one."""
    if not rate or rate.get("a") is None:
        return "no observed rate"
    if rate.get("soc_per_h") is None:
        return f"{rate['a']:.0f} A into the pack"
    return f"{rate['a']:.0f} A into the pack ({rate['soc_per_h']:.1f}% SOC/h)"


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

    def minutes_between(self, from_v, to_v):
        """Observed minutes from one terminal voltage to another."""
        deltas = []
        for t in self._usable(from_v):
            a = self._arrival(t["trace"], from_v)
            b = self._arrival(t["trace"], to_v)
            if a is None or b is None or b < a:
                continue
            deltas.append(b - a)
        if len(deltas) < MIN_CHARGE_CURVE_RUNS:
            return None
        return _median(deltas), len(deltas)

    def voltage_after(self, from_v, hours):
        """The terminal voltage reached `hours` after passing from_v."""
        reached = []
        for t in self._usable(from_v):
            a = self._arrival(t["trace"], from_v)
            if a is None:
                continue
            v = self._voltage_by(t["trace"], a + hours * 60.0)
            if v is not None:
                reached.append(v)
        if len(reached) < MIN_CHARGE_CURVE_RUNS:
            return None
        return _median(reached)

    def soc_for_voltage(self, target_v):
        return LoadModel._interpolate(self.soc_points, target_v)

    def volts_for_soc(self, target_soc):
        return _invert(self.soc_points, target_soc)


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
    def __init__(self, conn, cfg):
        # `conn` may be a connection or a provider of one; see history.resolve.
        self._conn = conn
        self.cfg = cfg

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
        """Observed charge rate, as current into the pack.

        Volts per hour is not a generator's rate. It is the generator minus
        whatever the house happened to be drawing, and on the first live night
        the MEP's 20:09 run was measured through a 7 kW steam bath and came
        out at 0.864 V/h - a number about the bath, not the generator. The
        shunt's net current is Ah/h into the pack, which against the learned
        capacity is a state-of-charge rate, and the learned voltage/SOC curve
        turns that into volts when a target needs one.

        Runs taken while the house drew more than LOAD_SPIKE_MULTIPLE times
        its mean load are still not the generator's rate, so they are left
        out. Exercise runs are too: 30 minutes at 09:00 with the sun already
        up says nothing about lifting the pack at night.

        `gen` of None pools every generator's runs, which is how the two of
        them running together are measured.
        """
        now = int(now or time.time())
        sql = ("SELECT gen, rate_v_per_h, rate_a, load_w, duration_min "
               "FROM gen_runs WHERE kind != 'exercise' AND rate_a IS NOT NULL "
               "AND start_ts >= ?")
        args = [now - days * 86400]
        if gen is not None:
            sql += " AND gen=?"
            args.append(gen)
        if solo is not None:
            sql += " AND solo=?"
            args.append(int(solo))
        rows = [r for r in self.conn.execute(sql, args).fetchall()
                if r["duration_min"] >= MIN_RUN_MINUTES and r["rate_a"] > 0]

        mean_load = self.mean_load_w(now=now)
        spikes = 0
        if mean_load:
            ceiling = LOAD_SPIKE_MULTIPLE * mean_load
            kept = [r for r in rows if r["load_w"] is None or r["load_w"] <= ceiling]
            spikes = len(rows) - len(kept)
            rows = kept
        if not rows:
            return None

        amps = _median([r["rate_a"] for r in rows])
        capacity_ah = self.capacity_ah()
        observed_v = [r["rate_v_per_h"] for r in rows if r["rate_v_per_h"] is not None]
        return {
            "gen": gen, "solo": solo, "runs": len(rows),
            "a": round(amps, 1),
            "capacity_ah": capacity_ah,
            "soc_per_h": (round(100.0 * amps / capacity_ah, 2)
                          if capacity_ah else None),
            "mean_load_w": round(mean_load) if mean_load else None,
            "excluded_load_spikes": spikes,
            # Recorded because it happened. Nothing plans from it.
            "observed_v_per_h": (round(_median(observed_v), 3)
                                 if observed_v else None),
        }

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
        sample = history.latest_sample(self.conn)
        if not sample or sample["batt_soc"] is None:
            return {"reached": None, "reason": "no battery monitor sample"}

        soc_target = self.soc_for_voltage(target_v)
        if soc_target is None:
            status = self.soc_curve_status()
            if status["points"]:
                reason = (f"the learned voltage/SOC curve only covers "
                          f"{status['volts_low']}-{status['volts_high']} V, "
                          f"so {target_v} V is off the end of it")
            else:
                reason = (f"no voltage/SOC history yet, so SOC at {target_v} V "
                          f"is unknown; run scrape_gateway.py --backfill")
            return {"reached": None, "reason": reason}
        capacity = self.capacity_wh()
        if capacity is None:
            return {"reached": None, "reason": "pack capacity not learned"}

        available_wh = (sample["batt_soc"] - soc_target) / 100.0 * capacity
        if available_wh <= 0:
            # There is nothing left above the target, so the answer is "now",
            # not an unknown. Leaving `at` unset printed "?" from 03:10 onward
            # on the first live night, exactly when the number mattered most.
            return {"reached": now, "at": "now", "hours": 0.0,
                    "reason": "already at or below target",
                    "soc_now": sample["batt_soc"], "soc_target": round(soc_target, 1),
                    "available_wh": round(available_wh)}

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
                        "soc_now": sample["batt_soc"],
                        "soc_target": round(soc_target, 1),
                        "capacity_wh": capacity,
                        "available_wh": round(available_wh)}
            remaining -= net
        return {"reached": None, "reason": f"not reached within {hours} h",
                "soc_now": sample["batt_soc"], "soc_target": round(soc_target, 1),
                "available_wh": round(available_wh)}

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
                                      since=now - days * 86400)
        ceiling = None
        mean_load = self.mean_load_w(now=now)
        if mean_load:
            ceiling = LOAD_SPIKE_MULTIPLE * mean_load

        traces, counts = {}, {}
        for r in rows:
            if ceiling is not None and r["load_w"] is not None and r["load_w"] > ceiling:
                continue
            t = traces.get(r["run_id"])
            if t is None:
                t = traces[r["run_id"]] = {"start_v": r["start_v"], "trace": {}}
            v_bin = history.soc_bin(r["battery_v"])
            minutes = (r["ts"] - r["start_ts"]) / 60.0
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
        """The best-evidenced rate for this generator, solo history first."""
        rate = self.charge_rate(gen, solo=solo, now=now)
        if rate is None:
            rate = self.charge_rate(gen, solo=None, now=now)
        return rate

    def hours_to_target(self, from_v, target_v, rate, soc_now=None):
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
        if soc_now is None:
            soc_now = self.soc_for_voltage(from_v)
        if soc_now is None:
            return None
        if soc_target <= soc_now:
            return 0.0
        return (soc_target - soc_now) / rate["soc_per_h"]

    def reach(self, gen, from_v, target_v, window_h, solo=None, soc_now=None,
              now=None):
        """Whether `gen` can lift the pack to target_v inside window_h.

        One place, so the guard's refusal and the POLICY detail can never
        quote different arithmetic for the same question.

        Three ways of answering, best evidence first. What the pack was
        observed to do between these two voltages while charging; failing
        that, the charge-side curve against the observed current; and only
        when a generator has fewer than three runs on record, the resting
        curve, which is what the Pi5 stops on only after the charge is over
        and so reads a target as harder than it is. `basis` says which.
        """
        curve = self.charge_curve(gen, solo=solo, now=now)
        base = {"gen": gen, "window_h": window_h, "target_v": target_v,
                "curve": curve}

        if curve.learned:
            observed = curve.minutes_between(from_v, target_v)
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
        if rate is None or not rate.get("soc_per_h"):
            missing = ("no observed charge rate" if rate is None
                       else "the pack capacity is not learned")
            return dict(base, ok=False, rate=rate, hours=None, basis=None,
                        why=f"{missing} for {gen}, so {target_v:.1f} V cannot "
                            f"be shown to be reachable")

        # A learned charge curve still cannot speak for a voltage no run has
        # ever reached, and 57.0 is exactly that until a run is allowed to go
        # there. Falling back to the resting curve keeps a conservative answer
        # rather than refusing on a gap in the evidence; the basis says so.
        soc_target = curve.soc_for_voltage(target_v) if curve.learned else None
        if soc_target is not None:
            basis = f"charging curve ({curve.label})"
        else:
            soc_target = self.soc_for_voltage(target_v)
            basis = (f"resting curve ({curve.label}, none of them reached "
                     f"{target_v:.1f} V)" if curve.learned else
                     f"resting curve, {curve.runs} charging "
                     f"run{'' if curve.runs == 1 else 's'} on record")
        hours = self._hours_from(soc_target, from_v, rate, soc_now)
        if hours is None:
            return dict(base, ok=False, rate=rate, hours=None, basis=basis,
                        why=f"neither the charging nor the resting curve "
                            f"reaches {target_v:.1f} V, so it cannot be shown "
                            f"to be reachable")
        ok = hours <= window_h + 1e-9
        return dict(base, ok=ok, rate=rate, hours=hours, basis=basis,
                    why=(f"{target_v:.1f} reachable in {hours:.1f} h at "
                         f"{rate_phrase(rate)}, {basis}" if ok else
                         f"{target_v:.1f} needs {hours:.1f} h at "
                         f"{rate_phrase(rate)} but the run window is "
                         f"{window_h:.1f} h, {basis}"))

    def _hours_from(self, soc_target, from_v, rate, soc_now):
        if soc_target is None:
            return None
        if soc_now is None:
            soc_now = self.soc_for_voltage(from_v)
        if soc_now is None:
            return None
        if soc_target <= soc_now:
            return 0.0
        return (soc_target - soc_now) / rate["soc_per_h"]

    def voltage_after(self, from_v, hours, rate, soc_now=None, curve=None):
        """The voltage the pack reaches after `hours` at this rate."""
        if not rate or not rate.get("soc_per_h"):
            return None
        soc = soc_now if soc_now is not None else self.soc_for_voltage(from_v)
        if soc is None:
            return None
        soc = min(100.0, soc + rate["soc_per_h"] * hours)
        if curve is not None and curve.learned:
            return curve.volts_for_soc(soc)
        return self.volts_for_soc(soc)

    def best_reachable_target(self, gen, from_v, window_h, ceiling, floor,
                              step=0.5, solo=None, soc_now=None, now=None):
        """The highest stop voltage reachable in the window, rounded down.

        Read off the same evidence reach() uses, so a rule that is told 57.0
        is out of range is not then handed a lower target from a different
        curve. None when even `floor` is out of reach, so a caller cannot
        propose a target that is not worth running for.
        """
        curve = self.charge_curve(gen, solo=solo, now=now)
        v = curve.voltage_after(from_v, window_h) if curve.learned else None
        if v is None:
            rate = self._rate_for(gen, solo, now)
            v = self.voltage_after(from_v, window_h, rate, soc_now=soc_now,
                                   curve=curve)
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
