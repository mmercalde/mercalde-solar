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

import logging
import math
import statistics
import time
from datetime import datetime, timedelta

import history
import weather

log = logging.getLogger(__name__)

# A profile cell needs this many observations before it is worth quoting.
MIN_SAMPLES_PER_HOUR = 3
MIN_DAYS_FOR_SOLAR_FIT = 8
# A run taken while the house drew more than this multiple of its mean load
# says more about the load than about the generator, so it is left out of the
# learned rate. The 20:09 MEP run on the first live night was one of these: a
# 7 kW steam bath against a mean nearer 1.5 kW.
LOAD_SPIKE_MULTIPLE = 2.0
# A run shorter than this cannot say anything about a rate.
MIN_RUN_MINUTES = 10

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
        rows = self.conn.execute(
            "SELECT hour_ts, wh_out FROM hourly "
            "WHERE device='load' AND wh_out IS NOT NULL ORDER BY hour_ts").fetchall()
        if not rows:
            return []
        excluded = history.gen_running_hours(
            self.conn, rows[0]["hour_ts"], rows[-1]["hour_ts"] + 3600)
        out = []
        for r in rows:
            if r["hour_ts"] in excluded:
                continue
            t = history.local(r["hour_ts"], self.cfg)
            if month is not None and t.month != month:
                continue
            if weekend is not None and (t.weekday() >= 5) != weekend:
                continue
            out.append((t, r["wh_out"]))
        return out

    # --- load profile -------------------------------------------------------

    def load_profile(self, month=None, weekend=None):
        """{hour_of_day: median Wh}. Falls back to all months, then all days,
        whenever a narrower slice has too little evidence."""
        for m, w in ((month, weekend), (month, None), (None, weekend), (None, None)):
            buckets = {}
            for t, wh in self._clean_load_rows(m, w):
                buckets.setdefault(t.hour, []).append(wh)
            profile = {h: _median(v) for h, v in buckets.items()
                       if len(v) >= MIN_SAMPLES_PER_HOUR}
            if len(profile) >= 12:
                return {"profile": profile, "month": m, "weekend": w,
                        "hours_covered": len(profile),
                        "observations": sum(len(v) for v in buckets.values())}
        return {"profile": {}, "month": None, "weekend": None,
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

    def overnight_drawdown(self, month=None, now=None):
        """Median Wh consumed between sunset and sunrise, by month."""
        now = int(now or time.time())
        month = month or history.local(now, self.cfg).month
        sun = weather.sun_times(self.cfg)
        if not sun:
            return None
        sunset_h = history.local(sun[1], self.cfg).hour
        sunrise_h = history.local(sun[0], self.cfg).hour
        by_night = {}
        for t, wh in self._clean_load_rows(month=month):
            if t.hour >= sunset_h or t.hour < sunrise_h:
                # Hours after midnight belong to the night that began yesterday.
                night = (t.date() if t.hour >= sunset_h
                         else (t - timedelta(days=1)).date())
                by_night.setdefault(night, []).append(wh)
        # Only whole nights count: a night with hours missing (to a generator
        # run, or to a sampling gap) would understate the drawdown.
        night_hours = 24 - sunset_h + sunrise_h
        totals = [sum(v) for v in by_night.values() if len(v) >= night_hours]
        if not totals:
            return None
        return {"month": month, "wh": round(_median(totals)), "nights": len(totals)}

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
        """The inverse of the learned curve: the voltage at a given SOC."""
        curve = self.voltage_soc_curve()
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
        """
        rate = self._rate_for(gen, solo, now)
        if rate is None or not rate.get("soc_per_h"):
            missing = ("no observed charge rate" if rate is None
                       else "the pack capacity is not learned")
            return {"ok": False, "rate": rate, "hours": None, "gen": gen,
                    "window_h": window_h, "target_v": target_v,
                    "why": f"{missing} for {gen}, so {target_v:.1f} V cannot be "
                           f"shown to be reachable"}
        hours = self.hours_to_target(from_v, target_v, rate, soc_now=soc_now)
        if hours is None:
            return {"ok": False, "rate": rate, "hours": None, "gen": gen,
                    "window_h": window_h, "target_v": target_v,
                    "why": f"the learned voltage/SOC curve does not reach "
                           f"{target_v:.1f} V, so it cannot be shown to be "
                           f"reachable"}
        ok = hours <= window_h + 1e-9
        return {"ok": ok, "rate": rate, "hours": hours, "gen": gen,
                "window_h": window_h, "target_v": target_v,
                "why": (f"{target_v:.1f} reachable in {hours:.1f} h at "
                        f"{rate_phrase(rate)}" if ok else
                        f"{target_v:.1f} needs {hours:.1f} h at "
                        f"{rate_phrase(rate)} but the run window is "
                        f"{window_h:.1f} h")}

    def voltage_after(self, from_v, hours, rate, soc_now=None):
        """The voltage the pack reaches after `hours` at this rate."""
        if not rate or not rate.get("soc_per_h"):
            return None
        soc = soc_now if soc_now is not None else self.soc_for_voltage(from_v)
        if soc is None:
            return None
        return self.volts_for_soc(min(100.0, soc + rate["soc_per_h"] * hours))

    def best_reachable_target(self, gen, from_v, window_h, ceiling, floor,
                              step=0.5, solo=None, soc_now=None, now=None):
        """The highest stop voltage reachable in the window, rounded down.

        None when even `floor` is out of reach, so a caller cannot end up
        proposing a target that is not worth running for.
        """
        rate = self._rate_for(gen, solo, now)
        v = self.voltage_after(from_v, window_h, rate, soc_now=soc_now)
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
