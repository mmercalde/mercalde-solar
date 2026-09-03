"""Sunrise and sunset from latitude, longitude and the date. No network.

These used to be read off the Open-Meteo forecast, which made the guard's
daylight hold - the rule that stops a generator being run into a producing
day - depend on a third party being reachable. Where the sun is at a given
moment is not weather. It is geometry, it is known centuries ahead, and it
does not need anyone's API to be up.

NOAA's solar position algorithm, which is accurate to well under a minute at
this latitude - far finer than a rule about whether the sun is up. Open-Meteo
is still asked about cloud and irradiance, which it alone knows.
"""

import math
from datetime import datetime, timedelta, timezone

import history

# Where the centre of the solar disc sits when the upper limb touches the
# horizon: half a degree of disc plus about 34 minutes of arc of refraction.
ZENITH = 90.833

# Julian day number of the Unix epoch, and of J2000.0.
JD_UNIX_EPOCH = 2440587.5
JD_J2000 = 2451545.0


def _solar(t):
    """(declination in degrees, equation of time in minutes) at Julian century t."""
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    centre = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
              + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
              + math.sin(3 * mr) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * t)
    apparent = l0 + centre - 0.00569 - 0.00478 * math.sin(omega)
    obliquity = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))
                        / 60.0) / 60.0
    eps = math.radians(obliquity + 0.00256 * math.cos(omega))
    declination = math.degrees(math.asin(math.sin(eps)
                                         * math.sin(math.radians(apparent))))
    y = math.tan(eps / 2.0) ** 2
    l0r = math.radians(l0)
    eqtime = 4.0 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr))
    return declination, eqtime


def sun_times(lat, lon, day, tz):
    """(sunrise, sunset) as Unix timestamps for a local YYYY-MM-DD date.

    None where the sun neither rises nor sets that day, which cannot happen
    between the polar circles and so never happens at this site.
    """
    noon_local = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=tz)
    ts_noon = noon_local.timestamp()
    # The declination and the equation of time move slowly, so evaluating them
    # at local noon is good for the whole of that day.
    t = (ts_noon / 86400.0 + JD_UNIX_EPOCH - JD_J2000) / 36525.0
    declination, eqtime = _solar(t)

    lat_r, dec_r = math.radians(lat), math.radians(declination)
    cos_ha = (math.cos(math.radians(ZENITH)) / (math.cos(lat_r) * math.cos(dec_r))
              - math.tan(lat_r) * math.tan(dec_r))
    if not -1.0 <= cos_ha <= 1.0:
        return None
    half_day_minutes = 4.0 * math.degrees(math.acos(cos_ha))

    # Solar noon in UTC minutes past midnight, placed on the UTC day that
    # actually contains it: near the date line the local day and the UTC day
    # are not the same one.
    utc_noon = datetime.fromtimestamp(ts_noon, timezone.utc)
    midnight = utc_noon.replace(hour=0, minute=0, second=0,
                                microsecond=0).timestamp()
    solar_noon = midnight + (720.0 - 4.0 * lon - eqtime) * 60.0
    while solar_noon - ts_noon > 43200:
        solar_noon -= 86400
    while ts_noon - solar_noon > 43200:
        solar_noon += 86400

    return (int(solar_noon - half_day_minutes * 60.0),
            int(solar_noon + half_day_minutes * 60.0))


# --- the shapes the rest of the agent asks in --------------------------------

def times(cfg, day=None, now=None):
    """(sunrise, sunset) for a local day, defaulting to today."""
    day = day or history.local_day(int(now or datetime.now().timestamp()), cfg)
    return sun_times(cfg["lat"], cfg["lon"], day, history.tzinfo(cfg))


def next_sunrise(cfg, now=None, days=3):
    """The first sunrise after `now`."""
    now = int(now or datetime.now().timestamp())
    tz = history.tzinfo(cfg)
    start = datetime.fromtimestamp(now, tz).date()
    for i in range(days):
        st = times(cfg, (start + timedelta(days=i)).strftime("%Y-%m-%d"))
        if st and st[0] > now:
            return st[0]
    return None


WINDOWS = ("overnight", "today", "yesterday")


def window_span(cfg, name, now=None):
    """(start, end, label) for a window the owner named, or (None, None, why).

    The owner says "overnight". The tool had only a number of trailing hours,
    so the model reached for 24 and reported a whole day's load - 25,273 Wh -
    as the night's. A word the owner uses should be a value the tool takes.

    overnight is the most recent sunset to now while the night is still
    running, and the night that just ended once the sun is up: asked at
    lunchtime, "last night" is last night and not the twelve hours behind us.
    """
    now = int(now or datetime.now().timestamp())
    name = str(name or "").strip().lower().replace("_", " ")
    aliases = {"last night": "overnight", "tonight": "overnight",
               "since sunset": "overnight", "this morning": "today"}
    name = aliases.get(name, name)
    if name not in WINDOWS:
        return None, None, (f"{name!r} is not a window; use one of "
                            f"{', '.join(WINDOWS)}")

    tz = history.tzinfo(cfg)
    local_now = datetime.fromtimestamp(now, tz)
    midnight = int(local_now.replace(hour=0, minute=0, second=0,
                                     microsecond=0).timestamp())
    if name == "today":
        return midnight, now, "today, since local midnight"
    if name == "yesterday":
        return midnight - 86400, midnight, "yesterday, midnight to midnight"

    today = history.local_day(now, cfg)
    yesterday = history.local_day(now - 86400, cfg)
    t_today, t_yest = times(cfg, today), times(cfg, yesterday)
    if not t_today or not t_yest:
        return None, None, "sunrise and sunset are not computable here"
    sunrise, sunset = t_today
    if now >= sunset:
        # The evening: the night is running and has not ended yet.
        return sunset, now, "overnight, since sunset"
    if now < sunrise:
        # The small hours: still last night, which began yesterday evening.
        return t_yest[1], now, "overnight, since sunset yesterday"
    # Daylight: the night that just ended, sunset to sunrise.
    return t_yest[1], sunrise, "last night, sunset to sunrise"


def daylight(cfg, ts):
    """(sunrise, sunset) when `ts` falls between them, else None."""
    st = times(cfg, history.local_day(ts, cfg))
    return st if st and st[0] <= ts <= st[1] else None
