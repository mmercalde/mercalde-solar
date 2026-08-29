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
import statistics
import time
from datetime import datetime, timedelta

import history
import weather

log = logging.getLogger(__name__)

# A profile cell needs this many observations before it is worth quoting.
MIN_SAMPLES_PER_HOUR = 3
MIN_DAYS_FOR_SOLAR_FIT = 8
# Voltage bin width for the empirical V->SOC curve.
SOC_BIN_V = 0.25


def _median(values):
    return statistics.median(values) if values else None


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

    def charge_rate(self, gen, solo=None, days=180, now=None):
        """Observed charge rate for a generator, in V/h and A.

        Exercise runs are excluded: 30 minutes at 09:00 with the sun already up
        says nothing about how fast that generator lifts the pack at night.
        """
        now = int(now or time.time())
        sql = ("SELECT rate_v_per_h, rate_a, duration_min FROM gen_runs "
               "WHERE gen=? AND kind != 'exercise' AND rate_v_per_h IS NOT NULL "
               "AND start_ts >= ?")
        args = [gen, now - days * 86400]
        if solo is not None:
            sql += " AND solo=?"
            args.append(int(solo))
        rows = self.conn.execute(sql, args).fetchall()
        # A run too short to move the pack gives a meaningless rate.
        rows = [r for r in rows if r["duration_min"] >= 10 and r["rate_v_per_h"] > 0]
        if not rows:
            return None
        return {"gen": gen, "solo": solo, "runs": len(rows),
                "v_per_h": round(_median([r["rate_v_per_h"] for r in rows]), 3),
                "a": round(_median([r["rate_a"] for r in rows if r["rate_a"] is not None]), 1)
                     if any(r["rate_a"] is not None for r in rows) else None}

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
        return out

    # --- battery ------------------------------------------------------------

    def soc_for_voltage(self, target_v):
        """Median SOC observed at a given resting-discharge voltage.

        Only samples with no generator running and the pack discharging are
        used: a charging pack sits well above its resting voltage.
        """
        lo, hi = target_v - SOC_BIN_V, target_v + SOC_BIN_V
        rows = self.conn.execute(
            "SELECT batt_soc FROM samples WHERE battery_v BETWEEN ? AND ? "
            "AND batt_soc IS NOT NULL AND batt_power < 0 "
            "AND mep_action != ? AND kub_action != ?",
            (lo, hi, history.GEN_RUNNING, history.GEN_RUNNING)).fetchall()
        vals = [r["batt_soc"] for r in rows]
        if len(vals) < 10:
            return None
        return _median(vals)

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
            return {"reached": None,
                    "reason": f"no observed SOC at {target_v} V yet"}
        capacity = self.capacity_wh()
        if capacity is None:
            return {"reached": None, "reason": "pack capacity not learned"}

        available_wh = (sample["batt_soc"] - soc_target) / 100.0 * capacity
        if available_wh <= 0:
            return {"reached": now, "hours": 0.0, "reason": "already at or below target",
                    "soc_now": sample["batt_soc"], "soc_target": round(soc_target, 1)}

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
                        "at": datetime.fromtimestamp(reached, history.tzinfo(self.cfg))
                                      .strftime("%H:%M"),
                        "hours": round((reached - now) / 3600.0, 2),
                        "soc_now": sample["batt_soc"],
                        "soc_target": round(soc_target, 1),
                        "capacity_wh": capacity,
                        "available_wh": round(available_wh)}
            remaining -= net
        return {"reached": None, "reason": f"not reached within {hours} h",
                "soc_now": sample["batt_soc"], "soc_target": round(soc_target, 1),
                "available_wh": round(available_wh)}

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
