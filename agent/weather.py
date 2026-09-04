"""Open-Meteo forecast for the site. No API key.

Hourly cloud cover, shortwave radiation and temperature, in the site's local
timezone (SPEC section 2). Cloud and irradiance are the only things here that
genuinely need asking: sunrise and sunset are computed in sun.py, so nothing
that depends on where the sun is depends on this being reachable.
"""

import logging
import time
from datetime import datetime, timedelta

import requests

import history
import sun

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = "cloud_cover,shortwave_radiation,temperature_2m"
CACHE_SECONDS = 900

_cache = {"ts": 0, "data": None}


def fetch(cfg, days=3, timeout=20, force=False):
    """Raw Open-Meteo response, cached for 15 minutes.

    The forecast does not change faster than that, and every tick asks for it.
    """
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["data"]
    r = requests.get(URL, params={
        "latitude": cfg["lat"], "longitude": cfg["lon"],
        "hourly": HOURLY,
        "timezone": cfg["tz"], "forecast_days": days,
    }, timeout=timeout)
    r.raise_for_status()
    _cache.update(ts=now, data=r.json())
    return _cache["data"]


def _parse_local(stamp, cfg):
    """Open-Meteo returns naive local times when `timezone` is set."""
    return int(datetime.fromisoformat(stamp)
               .replace(tzinfo=history.tzinfo(cfg)).timestamp())


def hourly(cfg, hours=48, now=None, data=None):
    """[{ts, hour, cloud, radiation, temp}] for the next N hours."""
    data = data or fetch(cfg)
    now = int(now or time.time())
    h = data["hourly"]
    out = []
    for i, stamp in enumerate(h["time"]):
        ts = _parse_local(stamp, cfg)
        if ts < history.hour_floor(now) or ts > now + hours * 3600:
            continue
        out.append({
            "ts": ts,
            "hour": datetime.fromisoformat(stamp).hour,
            "cloud": h["cloud_cover"][i],
            "radiation": h["shortwave_radiation"][i],
            "temp": h["temperature_2m"][i],
        })
    return out


def cloud_of(window):
    """The cloud figure the rules read from one of `summary`'s day windows.

    The daylight mean where the day has daylight hours in the forecast, the
    round-the-clock mean where it does not. What a stop voltage is set for is
    how much sun the day will make, and the cloud at 3 am is not part of that.
    """
    if not window:
        return None
    daylight = window.get("daylight_cloud_pct")
    return daylight if daylight is not None else window.get("cloud_pct")


def summary(cfg, hours=48, now=None):
    """Condensed forecast for the model and the plan record."""
    data = fetch(cfg)
    now = int(now or time.time())
    rows = hourly(cfg, hours, now=now, data=data)
    if not rows:
        return {"error": "no forecast rows"}
    today = history.local_day(now, cfg)
    # The day the forecast is about is the one the sun next comes up on, not
    # the calendar day after this instant. Between midnight and sunrise those
    # are two different days: at 00:13 on 2026-09-04 the coming daylight was
    # the 4th, and calendar tomorrow was the 5th. POLICY 3 read the 5th's 99%
    # cloud and raised both stops to 57.0 for a storm a day and a night away,
    # after the 6:59 pm plan the same night had correctly read the 4th at 38%
    # and held. Before midnight it is tomorrow, after midnight it is today,
    # in daylight it is tomorrow again - which is what the next sunrise says
    # without any of the cases being written out.
    nxt = sun.next_sunrise(cfg, now)
    next_daylight = (history.local_day(nxt, cfg) if nxt else
                     (datetime.fromtimestamp(now, history.tzinfo(cfg))
                      + timedelta(days=1)).strftime("%Y-%m-%d"))

    def window(day):
        sel = [r for r in rows if history.local_day(r["ts"], cfg) == day]
        if not sel:
            return None
        daylight = [r for r in sel if r["radiation"] > 0]
        return {
            "cloud_pct": round(sum(r["cloud"] for r in sel) / len(sel)),
            "daylight_cloud_pct": (round(sum(r["cloud"] for r in daylight) / len(daylight))
                                   if daylight else None),
            # Wh/m2 over the day: one row per hour, so radiation sums directly.
            "radiation_wh_m2": round(sum(r["radiation"] for r in sel)),
            "max_temp_c": round(max(r["temp"] for r in sel), 1),
            "min_temp_c": round(min(r["temp"] for r in sel), 1),
        }

    out = {"hours": hours, "today": window(today), "today_date": today,
           "next_daylight": window(next_daylight),
           "next_daylight_date": next_daylight,
           "next_daylight_label": history.day_label(next_daylight)}
    # Alias, for one release. `tomorrow` was the calendar day after now, which
    # between midnight and sunrise names the day after the one about to dawn.
    # Read `next_daylight`; this key goes away. It is the same dict, so
    # anything a caller adds to one is on the other.
    out["tomorrow"] = out["next_daylight"]
    # Computed, not fetched: see sun.py.
    times = sun.times(cfg, today)
    if times:
        out["sunrise"] = history.clock(times[0], cfg)
        out["sunset"] = history.clock(times[1], cfg)
        out["sunrise_ts"], out["sunset_ts"] = times
    if nxt:
        out["next_sunrise"] = history.clock(nxt, cfg)
        out["next_sunrise_ts"] = nxt
    return out


# --- historical cloud cover -------------------------------------------------
#
# The solar model learns yield against cloud cover, but nothing stores what the
# forecast said on a past day. Open-Meteo's archive endpoint (same provider, no
# key) supplies the observed daily mean for any day already in `daily`, so the
# model can learn from backfilled history instead of waiting to accumulate it.

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ARCHIVE_CACHE = None  # set lazily to agent/data/weather_archive.json


def _archive_cache_path():
    global ARCHIVE_CACHE
    if ARCHIVE_CACHE is None:
        import os
        import config
        ARCHIVE_CACHE = os.path.join(config.DATA_DIR, "weather_archive.json")
    return ARCHIVE_CACHE


def archive_daily(cfg, start, end, timeout=60):
    """{day: {"cloud": pct, "radiation_mj": MJ/m2}} for a local date range.

    Cached on disk; only days not already cached are requested. The archive
    lags real time by a few days, so recent days may simply be absent.
    """
    import json
    import os

    path = _archive_cache_path()
    cache = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}

    wanted = []
    d = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= last:
        key = d.strftime("%Y-%m-%d")
        if key not in cache:
            wanted.append(key)
        d += timedelta(days=1)

    if wanted:
        try:
            r = requests.get(ARCHIVE_URL, params={
                "latitude": cfg["lat"], "longitude": cfg["lon"],
                "start_date": min(wanted), "end_date": max(wanted),
                "daily": "cloud_cover_mean,shortwave_radiation_sum",
                "timezone": cfg["tz"]}, timeout=timeout)
            r.raise_for_status()
            dd = r.json().get("daily", {})
            for i, day in enumerate(dd.get("time", [])):
                cloud = dd["cloud_cover_mean"][i]
                rad = dd["shortwave_radiation_sum"][i]
                if cloud is None:
                    continue
                cache[day] = {"cloud": cloud, "radiation_mj": rad}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(cache, f)
        except requests.RequestException as e:
            log.warning("weather archive fetch failed: %s", e)

    out = {}
    d = datetime.strptime(start, "%Y-%m-%d").date()
    while d <= last:
        key = d.strftime("%Y-%m-%d")
        if key in cache:
            out[key] = cache[key]
        d += timedelta(days=1)
    return out
