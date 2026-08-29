"""Sunrise and sunset, computed rather than fetched.

The reference times are Open-Meteo's own for this site, checked against the
computation over a week: it agreed to within one minute every day. A rule
about whether the sun is up does not need better than that.
"""

from datetime import datetime, timedelta

import pytest

import history
import sun

# Rosarito, from config.example.json: 32.36 N, 117.06 W, America/Tijuana.
# (day, sunrise, sunset) as Open-Meteo gives them.
REFERENCE = [
    ("2026-08-29", "6:21 am", "7:16 pm"),
    ("2026-08-31", "6:22 am", "7:14 pm"),
    ("2026-09-02", "6:24 am", "7:11 pm"),
    ("2026-09-04", "6:25 am", "7:08 pm"),
]


def clock(ts, cfg):
    return history.clock(ts, cfg)


@pytest.mark.parametrize("day,sunrise,sunset", REFERENCE)
def test_it_agrees_with_the_forecast_it_replaces(cfg, day, sunrise, sunset):
    got = sun.times(cfg, day)
    assert clock(got[0], cfg) == sunrise
    assert clock(got[1], cfg) == sunset


def test_the_solstices_are_the_longest_and_shortest_days(cfg):
    june = sun.times(cfg, "2026-06-21")
    december = sun.times(cfg, "2026-12-21")
    # At 32 degrees north the year runs between roughly 10 and 14.3 hours.
    assert 14.2 * 3600 < june[1] - june[0] < 14.4 * 3600
    assert 9.9 * 3600 < december[1] - december[0] < 10.1 * 3600


def test_sunrise_is_before_sunset_every_day_of_a_year(cfg):
    d = datetime(2026, 1, 1).date()
    for i in range(365):
        day = (d + timedelta(days=i)).strftime("%Y-%m-%d")
        sr, ss = sun.times(cfg, day)
        assert sr < ss, day
        assert history.local_day(sr, cfg) == day, day
        assert history.local_day(ss, cfg) == day, day


def test_the_day_length_changes_smoothly(cfg):
    """No jump at a month end, a leap day or the daylight-saving change."""
    d = datetime(2026, 1, 1).date()
    previous = None
    for i in range(365):
        sr, ss = sun.times(cfg, (d + timedelta(days=i)).strftime("%Y-%m-%d"))
        length = ss - sr
        if previous is not None:
            assert abs(length - previous) < 200, d + timedelta(days=i)
        previous = length


def test_daylight_saving_does_not_move_the_sun(cfg):
    """The clock jumps an hour; the sun does not. Both are local readings."""
    before = sun.times(cfg, "2026-03-07")
    after = sun.times(cfg, "2026-03-09")
    assert (after[0] - before[0]) > 3000, "the clock time of sunrise jumps"
    assert abs((after[1] - after[0]) - (before[1] - before[0])) < 400, \
        "but the day is only minutes longer"


def test_the_next_sunrise_is_tomorrows_after_todays_has_passed(cfg):
    today = sun.times(cfg, "2026-08-29")
    assert sun.next_sunrise(cfg, now=today[0] - 60) == today[0]
    assert sun.next_sunrise(cfg, now=today[0] + 60) == sun.times(
        cfg, "2026-08-30")[0]


def test_daylight_brackets_the_day(cfg):
    sr, ss = sun.times(cfg, "2026-08-29")
    assert sun.daylight(cfg, sr - 60) is None
    assert sun.daylight(cfg, sr + 60) == (sr, ss)
    assert sun.daylight(cfg, ss - 60) == (sr, ss)
    assert sun.daylight(cfg, ss + 60) is None


def test_the_far_north_can_have_no_sunrise_or_no_sunset(cfg):
    tz = history.tzinfo(cfg)
    assert sun.sun_times(78.2, 15.6, "2026-06-21", tz) is None, "midnight sun"
    assert sun.sun_times(78.2, 15.6, "2026-12-21", tz) is None, "polar night"
    assert sun.sun_times(78.2, 15.6, "2026-09-21", tz) is not None


def test_the_equator_has_a_twelve_hour_day_at_the_equinox(cfg):
    sr, ss = sun.sun_times(0.0, 0.0, "2026-03-20", history.tzinfo(cfg))
    assert abs((ss - sr) - 12 * 3600) < 600


def test_it_needs_no_network(cfg, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("sun times must not reach the network")))
    assert sun.times(cfg, "2026-08-29") is not None
    assert sun.next_sunrise(cfg, now=history.day_bounds("2026-08-29", cfg)[0])
