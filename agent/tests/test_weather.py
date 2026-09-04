"""Which day the forecast is about.

At 00:13 on 2026-09-04 POLICY 3 raised both stops to 57.0 for "99% daylight
cloud". That was the 5th's forecast: `tomorrow` was the calendar day after
the instant, and fourteen minutes past midnight the calendar had already
turned. The 6:59 pm plan the same night had read the 4th at 38% and held.

The day the night in progress is charging for is the day the sun next comes
up on, which is what these tests pin: before midnight it is tomorrow, after
midnight it is today, and in daylight it is tomorrow again.
"""

from datetime import datetime, timedelta

import pytest

import history
import policy
import sun
import weather

# The forecast that produced the fault: a fair Friday, then a storm on
# Saturday. Cloud per local day, and the site's own hours of daylight.
CLOUD = {"2026-09-03": 10, "2026-09-04": 38, "2026-09-05": 99}
DAYLIGHT_HOURS = range(7, 19)


@pytest.fixture
def forecast(monkeypatch):
    """Three days of hourly rows, in the shape Open-Meteo returns them."""
    time, cloud, radiation, temp = [], [], [], []
    for day, pct in CLOUD.items():
        for hour in range(24):
            time.append(f"{day}T{hour:02d}:00")
            cloud.append(pct)
            radiation.append(700.0 if hour in DAYLIGHT_HOURS else 0.0)
            temp.append(24.0)
    data = {"hourly": {"time": time, "cloud_cover": cloud,
                       "shortwave_radiation": radiation,
                       "temperature_2m": temp}}
    monkeypatch.setattr(weather, "fetch", lambda *a, **k: data)
    return data


def ts_at(cfg, day, hour, minute=0):
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute,
                        tzinfo=history.tzinfo(cfg)).timestamp())


def facts_at(cfg, now):
    """What agent.gather puts in front of POLICY 3, for one instant."""
    wx = weather.summary(cfg, now=now)
    return {"now": now,
            "next_daylight_cloud": weather.cloud_of(wx.get("next_daylight")),
            "next_daylight_date": wx.get("next_daylight_date"),
            "next_daylight_label": wx.get("next_daylight_label"),
            "sunrise_ts": wx.get("next_sunrise_ts"),
            "sunset_ts": wx.get("sunset_ts"),
            "thresholds": {"mep_start": 52.0, "mep_stop": 54.5,
                           "kub_start": 52.0, "kub_stop": 54.5}}


# --- the day the forecast is about ------------------------------------------

def test_the_evening_and_the_small_hours_are_the_same_night(cfg, forecast):
    """19:00 on the 3rd and 00:13 on the 4th are one night, and the day they
    are charging for is the 4th at both."""
    evening = weather.summary(cfg, now=ts_at(cfg, "2026-09-03", 19, 0))
    small_hours = weather.summary(cfg, now=ts_at(cfg, "2026-09-04", 0, 13))
    assert evening["next_daylight_date"] == "2026-09-04"
    assert small_hours["next_daylight_date"] == "2026-09-04"
    assert (weather.cloud_of(evening["next_daylight"])
            == weather.cloud_of(small_hours["next_daylight"]) == 38)


def test_a_daytime_forecast_is_tomorrows(cfg, forecast):
    """Once the sun is up, the next sunrise is tomorrow's."""
    wx = weather.summary(cfg, now=ts_at(cfg, "2026-09-04", 14, 0))
    assert wx["next_daylight_date"] == "2026-09-05"
    assert weather.cloud_of(wx["next_daylight"]) == 99


def test_the_day_is_named_where_it_is_quoted(cfg, forecast):
    wx = weather.summary(cfg, now=ts_at(cfg, "2026-09-04", 0, 13))
    assert wx["next_daylight_label"] == "Fri Sep 4"


def test_next_daylight_is_the_date_of_the_next_sunrise(cfg, forecast):
    """The rule stated once, against the sun itself, every hour of two days."""
    start = ts_at(cfg, "2026-09-03", 0, 0)
    for step in range(0, 48):
        now = start + step * 3600
        wx = weather.summary(cfg, now=now)
        assert wx["next_daylight_date"] == history.local_day(
            sun.next_sunrise(cfg, now), cfg), history.stamp(now, cfg)


def test_tomorrow_is_still_readable_for_one_release(cfg, forecast):
    wx = weather.summary(cfg, now=ts_at(cfg, "2026-09-04", 0, 13))
    assert wx["tomorrow"] is wx["next_daylight"]


# --- POLICY 3 on that forecast ----------------------------------------------

def test_the_storm_rule_reads_one_forecast_all_night(cfg, forecast):
    """The bug, as a test: at 6:59 pm the rule held on the 4th's 38%, and at
    12:13 am it raised both stops to 57.0 on the 5th's 99%. Both instants
    are the same night and must read the same day."""
    evening = policy.storm_stop(cfg, facts_at(cfg, ts_at(cfg, "2026-09-03", 18, 59)))
    small_hours = policy.storm_stop(cfg, facts_at(cfg, ts_at(cfg, "2026-09-04", 0, 13)))
    assert evening["day"] == small_hours["day"] == "next daylight (Fri Sep 4)"
    assert not evening["fires"] and not small_hours["fires"]
    assert "38% daylight cloud < 70%" in small_hours["detail"]


def test_the_storm_rule_fires_on_the_night_before_the_storm(cfg, forecast):
    """The same 99% forecast, read from the night it actually belongs to."""
    r = policy.storm_stop(cfg, facts_at(cfg, ts_at(cfg, "2026-09-04", 20, 0)))
    assert r["fires"] and r["day"] == "next daylight (Sat Sep 5)"
    assert r["proposal"]["mep_stop"] == 57.0 and r["proposal"]["kub_stop"] == 57.0


def test_a_daytime_evaluation_is_held_and_still_names_tomorrow(cfg, forecast):
    """Held until sunset, as a stop for tonight must be - and saying which
    day's forecast it was looking at when it held."""
    r = policy.storm_stop(cfg, facts_at(cfg, ts_at(cfg, "2026-09-04", 14, 0)))
    assert not r["fires"] and r["held"]
    assert r["day"] == "next daylight (Sat Sep 5)"
    assert r["detail"].startswith("next daylight (Sat Sep 5) 99% daylight cloud;")
