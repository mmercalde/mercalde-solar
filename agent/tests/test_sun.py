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


# --- Windows the owner names -------------------------------------------------
#
# "load energy used overnight?" was answered with get_history(hours=24):
# 25,273 Wh, a correct day reported as a night. The tool could not say
# "overnight", so the model picked the nearest number it could pass.

def at(cfg, when):
    tz = history.tzinfo(cfg)
    return int(datetime(*when, tzinfo=tz).timestamp())


def test_overnight_in_the_evening_runs_from_tonights_sunset(cfg):
    now = at(cfg, (2026, 9, 2, 22, 0))
    start, end, label = sun.window_span(cfg, "overnight", now)
    assert clock(start, cfg) == "7:11 pm"       # sunset, 2026-09-02
    assert history.local_day(start, cfg) == "2026-09-02"
    assert end == now
    assert "sunset" in label


def test_overnight_in_the_small_hours_reaches_back_to_yesterdays_sunset(cfg):
    """3 am belongs to the night that began the previous evening."""
    now = at(cfg, (2026, 9, 3, 3, 0))
    start, end, _ = sun.window_span(cfg, "overnight", now)
    assert history.local_day(start, cfg) == "2026-09-02"
    assert clock(start, cfg) == "7:11 pm"
    assert end == now
    assert (end - start) / 3600 == pytest.approx(7.8, abs=0.1)


def test_overnight_after_sunrise_is_the_night_that_just_ended(cfg):
    """Asked at lunchtime, "last night" is last night - not the last 12 hours."""
    now = at(cfg, (2026, 9, 3, 12, 0))
    start, end, label = sun.window_span(cfg, "overnight", now)
    assert clock(start, cfg) == "7:11 pm"
    assert clock(end, cfg) == "6:24 am"          # sunrise, 2026-09-03
    assert end < now
    assert "sunrise" in label


def test_the_window_flips_at_sunrise_and_not_before(cfg):
    sunrise, _ = sun.times(cfg, "2026-09-03")
    before = sun.window_span(cfg, "overnight", sunrise - 60)
    after = sun.window_span(cfg, "overnight", sunrise + 60)
    assert before[0] == after[0]                 # same night either side
    assert before[1] == sunrise - 60             # still running: ends now
    assert after[1] == sunrise                   # ended: ends at sunrise


def test_today_is_local_midnight_to_now(cfg):
    now = at(cfg, (2026, 9, 3, 12, 0))
    start, end, _ = sun.window_span(cfg, "today", now)
    assert history.clock(start, cfg) == "12:00 am"
    assert history.local_day(start, cfg) == "2026-09-03"
    assert end == now


def test_yesterday_is_a_whole_local_day(cfg):
    now = at(cfg, (2026, 9, 3, 12, 0))
    start, end, _ = sun.window_span(cfg, "yesterday", now)
    assert history.local_day(start, cfg) == "2026-09-02"
    assert end - start == 24 * 3600
    assert end == at(cfg, (2026, 9, 3, 0, 0))


def test_the_owners_other_words_for_the_same_night(cfg):
    now = at(cfg, (2026, 9, 3, 3, 0))
    for word in ("last night", "since sunset", "OVERNIGHT", " tonight "):
        assert sun.window_span(cfg, word, now)[0] == \
            sun.window_span(cfg, "overnight", now)[0], word


def test_a_word_it_does_not_know_says_so_rather_than_guessing(cfg):
    start, end, why = sun.window_span(cfg, "last fortnight", at(cfg, (2026, 9, 3, 12, 0)))
    assert start is None and end is None
    assert "overnight" in why and "today" in why
