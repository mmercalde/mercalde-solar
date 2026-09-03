"""What the pack has done, and what a model makes of it.

Measurement and reporting. The last test in the file is the one that says so.
"""

import math
from datetime import datetime, timedelta

import pytest

import health
import history


def ts_at(cfg, day, hour=12):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, tzinfo=tz).timestamp())


def battery_hours(conn, cfg, first="2026-05-01", days=100, wh_out=1800.0,
                  mean_v=54.0, hours=(20, 21, 22, 23, 0, 1, 2, 3)):
    """`days` days of battery discharge, a known amount every night.

    1,800 Wh an hour at 54.0 V is 33.33 Ah, so eight hours is 266.67 Ah a
    night - and against a 1,800 Ah pack that is a depth of 14.8%. May onward
    so the window crosses no daylight-saving change: an hour that does not
    exist locally collides with its neighbour and loses a row.
    """
    d0 = datetime.strptime(first, "%Y-%m-%d").date()
    for i in range(days):
        for h in hours:
            day = d0 + timedelta(days=i + (0 if h >= 19 else 1))
            history.put_hourly(conn, ts_at(cfg, day.strftime("%Y-%m-%d"), h),
                               "battery", mean_v, None, 0, wh_out,
                               mean_v - 0.25, mean_v + 0.25, 60, "live")
    conn.commit()


# --- throughput ------------------------------------------------------------------

def test_amp_hours_are_watt_hours_over_the_hour_s_own_voltage(conn, cfg):
    """One hour, 1,800 Wh at 54.0 V, is 33.3 Ah out."""
    history.put_hourly(conn, ts_at(cfg, "2026-01-01", 20), "battery",
                       54.0, None, 0, 1800.0, 53.8, 54.2, 60, "live")
    conn.commit()
    tp = health.throughput(conn, cfg)
    assert tp["total_ah_out"] == round(1800.0 / 54.0)


def test_cycles_are_amp_hours_over_nominal(conn, cfg):
    """100 nights at 266.67 Ah is 26,667 Ah, which on an 1,800 Ah pack is
    14.8 equivalent full cycles."""
    battery_hours(conn, cfg, days=100)
    tp = health.throughput(conn, cfg)
    assert tp["total_ah_out"] == pytest.approx(100 * 8 * 1800 / 54.0, rel=1e-3)
    assert tp["nominal_ah"] == 1800
    assert tp["equivalent_full_cycles"] == pytest.approx(14.8, abs=0.1)


def test_cycles_per_year_scales_by_the_span_not_the_calendar(conn, cfg):
    """100 days of it is 0.27 of a year, so 14.8 cycles is 54 a year."""
    battery_hours(conn, cfg, days=100)
    tp = health.throughput(conn, cfg)
    span_years = tp["span_days"] / 365.25
    assert tp["cycles_per_year"] == pytest.approx(
        tp["equivalent_full_cycles"] / span_years, rel=0.01)
    assert 50 < tp["cycles_per_year"] < 58


def test_mean_daily_depth_is_a_day_s_amp_hours_over_nominal(conn, cfg):
    """266.67 Ah a night against 1,800 Ah is 14.8%."""
    battery_hours(conn, cfg, days=100)
    tp = health.throughput(conn, cfg)
    assert tp["mean_daily_ah_out"] == pytest.approx(266.67, abs=1)
    assert tp["mean_daily_dod_pct"] == pytest.approx(14.8, abs=0.2)


def test_the_partial_first_and_last_days_are_left_out(conn, cfg):
    """They are partial by construction and would drag the mean down for a
    reason that has nothing to do with use."""
    battery_hours(conn, cfg, days=10)
    tp = health.throughput(conn, cfg)
    assert tp["days_counted"] < len(set()) + 11
    assert tp["mean_daily_dod_pct"] == pytest.approx(14.8, abs=0.2)


def test_with_no_hourly_rows_it_says_so_rather_than_returning_nought(conn, cfg):
    tp = health.throughput(conn, cfg)
    assert tp["total_ah_out"] is None and "no hourly" in tp["basis"]


def test_the_throughput_says_the_cells_are_second_hand(conn, cfg):
    battery_hours(conn, cfg, days=10)
    assert "second-life" in health.throughput(conn, cfg)["basis"]


# --- the measured series ----------------------------------------------------------

def charge_day(conn, cfg, day, wh_per_v, bottom=52.0, top=56.0,
               hours=(8, 9, 10, 11, 12, 13, 14, 15)):
    """One clear day: the sun walks the pack from `bottom` to `top`.

    Four volts in eight equal steps, giving up wh_per_v * 0.5 Wh a step, so
    the day's watt-hours per volt is wh_per_v exactly and a test can state
    what the series should say. Hours 8 to 15 sit inside sunrise and sunset
    in every month of the year at this latitude.
    """
    span = top - bottom
    step = span / len(hours)
    wh = wh_per_v * step
    for i, h in enumerate(hours):
        ts = ts_at(cfg, day, h)
        lo = bottom + i * step
        history.put_hourly(conn, ts, "battery", (lo + lo + step) / 2, None,
                           wh, 0.0, lo, lo + step, 60, "live")
        history.put_hourly(conn, ts, "gen", None, None, 0, None, None, None,
                           60, "live")
    conn.commit()


def charge_month(conn, cfg, year, month, days, wh_per_v, **kw):
    for n in range(days):
        charge_day(conn, cfg, f"{year}-{month:02d}-{2 + n:02d}", wh_per_v, **kw)


def test_a_day_gives_up_its_watt_hours_per_volt(conn, cfg):
    charge_day(conn, cfg, "2026-06-10", 5000)
    segs = health.charge_segments(conn, cfg, now=ts_at(cfg, "2026-06-20"))
    seg = segs["2026-06-10"]
    assert seg["span_v"] == pytest.approx(4.0)
    assert seg["wh_per_v"] == pytest.approx(5000, rel=1e-6)
    assert seg["from_v"] == 52.0 and seg["to_v"] == 56.0


def test_a_day_that_barely_moves_is_not_a_measurement(conn, cfg):
    """A cloudy day nudges the pack half a volt; that is not one point of a
    capacity series."""
    charge_day(conn, cfg, "2026-06-10", 5000, bottom=54.0, top=56.0)
    assert health.charge_segments(conn, cfg,
                                  now=ts_at(cfg, "2026-06-20")) == {}


def test_a_day_a_generator_ran_in_is_not_the_sun_s(conn, cfg):
    charge_day(conn, cfg, "2026-06-10", 5000)
    history.put_hourly(conn, ts_at(cfg, "2026-06-10", 11), "gen",
                       None, None, 4000, None, None, None, 60, "live")
    conn.commit()
    assert health.charge_segments(conn, cfg,
                                  now=ts_at(cfg, "2026-06-20")) == {}


def test_a_flat_series_has_no_trend(conn, cfg):
    for m in range(1, 9):
        charge_month(conn, cfg, 2026, m, 5, 5000)
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-09-15"))
    assert f["months"] == 8 and f["days_measured"] == 40
    assert all(s["wh_per_v"] == pytest.approx(5000, rel=0.02)
               for s in f["monthly"])
    assert f["trend_pct_per_year"] == pytest.approx(0.0, abs=0.5)


def test_a_known_slope_comes_back_as_that_slope(conn, cfg):
    """5,000 Wh/V falling by 50 a month is 1% of the mean a month, which is
    12% a year. The sign is negative because the pack is losing."""
    for i, m in enumerate(range(1, 9)):
        charge_month(conn, cfg, 2026, m, 5, 5000 - i * 50)
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-09-15"))
    assert f["months"] == 8
    mean = sum(s["wh_per_v"] for s in f["monthly"]) / 8
    assert f["trend_pct_per_year"] == pytest.approx(-50 * 12 / mean * 100,
                                                    abs=0.3)
    assert f["r_squared"] == pytest.approx(1.0, abs=0.01)
    assert f["usable_for_projection"] is True


def test_both_watt_hours_and_amp_hours_per_volt_are_reported(conn, cfg):
    charge_month(conn, cfg, 2026, 6, 5, 5000)
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-07-15"))
    m = f["monthly"][0]
    assert m["wh_per_v"] == pytest.approx(5000, rel=0.02)
    # Amp-hours over the same volts, so roughly the watt-hours over the
    # voltage the pack sat at.
    assert m["ah_per_v"] == pytest.approx(5000 / 54.0, rel=0.05)
    assert m["median_span_v"] == pytest.approx(4.0)


def test_a_thin_month_is_left_out_of_the_series(conn, cfg):
    charge_month(conn, cfg, 2026, 1, 5, 5000)
    charge_month(conn, cfg, 2026, 2, 2, 9000)      # two days, dropped
    charge_month(conn, cfg, 2026, 3, 5, 5000)
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-04-15"))
    assert [s["month"] for s in f["monthly"]] == ["2026-01", "2026-03"]


def test_too_few_months_is_not_a_trend(conn, cfg):
    for m in (1, 2, 3):
        charge_month(conn, cfg, 2026, m, 5, 5000 - m * 50)
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-04-15"))
    assert f["usable_for_projection"] is False
    assert "fewer than the 6" in f["confidence"]


def test_a_noisy_series_is_refused_however_many_months_there_are(conn, cfg):
    """The real history's shape: the months move, but not in a line, and
    what moves them is the season."""
    for i, m in enumerate(range(1, 9)):
        charge_month(conn, cfg, 2026, m, 5, 4000 + (3000 if i % 2 else 0))
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-09-15"))
    assert f["months"] == 8 and f["r_squared"] < health.FADE_MIN_R2
    assert f["usable_for_projection"] is False
    assert "seasonal" in f["confidence"]


def test_a_pack_that_appears_to_gain_capacity_is_refused(conn, cfg):
    """A rising line fits perfectly and still means something other than the
    battery, because a pack gaining capacity is not a thing."""
    for i, m in enumerate(range(1, 9)):
        charge_month(conn, cfg, 2026, m, 5, 4000 + i * 60)
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-09-15"))
    assert f["r_squared"] > 0.9 and f["trend_pct_per_year"] > 0
    assert f["usable_for_projection"] is False
    assert "not a thing" in f["confidence"]


def test_the_same_month_a_year_apart_is_compared(conn, cfg):
    """A straight line through a seasonal signal measures the season. August
    against August does not."""
    charge_month(conn, cfg, 2025, 7, 5, 5000)
    charge_month(conn, cfg, 2025, 8, 5, 6000)
    charge_month(conn, cfg, 2026, 7, 5, 4750)      # 5% down on last July
    charge_month(conn, cfg, 2026, 8, 5, 5700)      # 5% down on last August
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-09-15"))
    yoy = f["year_over_year"]
    assert yoy["pairs"] == 2
    assert yoy["mean_pct"] == pytest.approx(-5.0, abs=0.3)
    assert {c["month"] for c in yoy["comparisons"]} == {"07", "08"}


def test_without_a_year_of_history_there_is_nothing_to_compare(conn, cfg):
    charge_month(conn, cfg, 2026, 6, 5, 5000)
    f = health.measured_fade(conn, cfg, now=ts_at(cfg, "2026-07-15"))
    assert f["year_over_year"]["pairs"] == 0
    assert "no month has a counterpart" in f["year_over_year"]["note"]


# --- the projection ----------------------------------------------------------------

def test_the_projection_arithmetic_from_known_inputs(cfg, conn):
    """77 cycles a year at 21% depth, 0.02%/EFC at full depth, depth^(n-1).

    An equivalent full cycle taken in 21% bites costs 0.21^0.5 = 0.46 of a
    full-depth one, so 0.0092%/EFC; a year of 77 of them is 0.71%, and 20
    points of fade is 28 years.
    """
    tp = {"cycles_per_year": 77.0, "mean_daily_dod_pct": 21.0,
          "mean_resting_v": None}
    fade = {"usable_for_projection": False, "confidence": "none"}
    p = health.projection(conn, cfg, tp=tp, fade=fade)
    per_efc = 0.02 * (0.21 ** 0.5)
    assert p["cycle_fade_pct_per_year"] == pytest.approx(77 * per_efc, abs=0.01)
    assert p["years_to_80pct_cycle"] == pytest.approx(20 / (77 * per_efc),
                                                      abs=0.5)


def test_shallow_cycling_costs_less_per_equivalent_full_cycle(cfg, conn):
    """The direction, which is the easy thing to get backwards and the
    invisible thing to get wrong. A pack worked from full to empty must fade
    faster per unit of throughput than one worked in shallow bites."""
    fade = {"usable_for_projection": False, "confidence": "none"}
    def at(dod):
        return health.projection(conn, cfg, fade=fade, tp={
            "cycles_per_year": 77.0, "mean_daily_dod_pct": dod,
            "mean_resting_v": None})["cycle_fade_pct_per_year"]
    assert at(21.0) < at(50.0) < at(100.0)
    # And at full depth the scaling does nothing at all: an EFC is a cycle.
    assert at(100.0) == pytest.approx(77 * 0.02, abs=0.01)
    # 0.3 to 0.6 of the full-depth rate at about a fifth depth.
    assert 0.3 < at(21.0) / at(100.0) < 0.6


def test_the_assumptions_name_the_depth_factor_and_its_direction(cfg, conn):
    p = health.projection(conn, cfg, fade={"usable_for_projection": False,
                                           "confidence": "none"},
                          tp={"cycles_per_year": 77.0,
                              "mean_daily_dod_pct": 21.0,
                              "mean_resting_v": None})
    cycle = next(a for a in p["assumptions"] if a.startswith("cycle fade"))
    assert "costs less per equivalent full cycle" in cycle
    assert "factor of 0.46" in cycle
    assert "depth^(n-1) with n=1.5" in cycle


def test_the_combined_figure_is_the_two_added(cfg, conn):
    """The headline. Adding them overstates the rate, because both mechanisms
    eat the same lithium, so the combined years are a floor."""
    p = health.projection(conn, cfg, fade={"usable_for_projection": False,
                                           "confidence": "none"},
                          tp={"cycles_per_year": 77.0,
                              "mean_daily_dod_pct": 21.0,
                              "mean_resting_v": None})
    assert p["combined_fade_pct_per_year"] == pytest.approx(
        p["cycle_fade_pct_per_year"] + p["calendar_fade_pct_per_year"],
        abs=0.02)
    assert p["years_to_80pct_combined"] == pytest.approx(
        20 / p["combined_fade_pct_per_year"], abs=0.1)
    # And it is the shorter of the two legs, by construction.
    assert p["years_to_80pct_combined"] < p["years_to_80pct_cycle"]
    assert p["years_to_80pct_combined"] < p["years_to_80pct_calendar"]
    assert any("floor rather than a best guess" in a for a in p["assumptions"])


def test_temperature_moves_the_calendar_term_on_arrhenius(cfg, conn):
    """0.5 eV is about a doubling per 10 C. 78 F against a 77 F reference is
    barely anything; 95 F is another matter."""
    tp = {"cycles_per_year": None, "mean_daily_dod_pct": None,
          "mean_resting_v": None}
    fade = {"usable_for_projection": False, "confidence": "none"}
    warm = health.projection(conn, dict(cfg, battery=dict(
        cfg["battery"], ambient_f=95)), tp=tp, fade=fade)
    mild = health.projection(conn, cfg, tp=tp, fade=fade)
    assert warm["calendar_fade_pct_per_year"] > mild["calendar_fade_pct_per_year"]
    assert warm["years_to_80pct_calendar"] < mild["years_to_80pct_calendar"]
    ratio = (warm["calendar_fade_pct_per_year"]
             / mild["calendar_fade_pct_per_year"])
    assert 1.8 < ratio < 3.0, "roughly a doubling over ten degrees C"


def test_the_shorter_of_the_two_is_the_dominant_mechanism(cfg, conn):
    fade = {"usable_for_projection": False, "confidence": "none"}
    hard = health.projection(conn, cfg, fade=fade, tp={
        "cycles_per_year": 300.0, "mean_daily_dod_pct": 60.0,
        "mean_resting_v": None})
    gentle = health.projection(conn, cfg, fade=fade, tp={
        "cycles_per_year": 5.0, "mean_daily_dod_pct": 5.0,
        "mean_resting_v": None})
    assert hard["dominant_mechanism"] == "cycling"
    assert gentle["dominant_mechanism"] == "calendar"


def test_a_believable_measured_trend_replaces_the_coefficient(conn, cfg):
    tp = {"cycles_per_year": 77.0, "mean_daily_dod_pct": 21.0,
          "mean_resting_v": None}
    fade = {"usable_for_projection": True, "trend_pct_per_year": -4.0,
            "months": 9, "confidence": "9 months"}
    p = health.projection(conn, cfg, tp=tp, fade=fade)
    assert p["calendar_fade_pct_per_year"] == 4.0
    assert p["years_to_80pct_calendar"] == pytest.approx(5.0, abs=0.1)
    assert any("replaced by the measured trend" in a for a in p["assumptions"])


def test_an_unbelievable_one_does_not(conn, cfg):
    """Taken literally, six months of any slope would override the
    literature. Six months of noise reading +25%/yr would then tell the owner
    the battery is improving, which is why the override is gated on the fit
    as well as on the count."""
    tp = {"cycles_per_year": 77.0, "mean_daily_dod_pct": 21.0,
          "mean_resting_v": None}
    fade = {"usable_for_projection": False, "trend_pct_per_year": 25.1,
            "months": 14, "confidence": "the months are spread 68% of the mean"}
    p = health.projection(conn, cfg, tp=tp, fade=fade)
    assert p["calendar_fade_pct_per_year"] < 3.0, "the literature figure stands"
    assert any("not used" in a for a in p["assumptions"])


def test_every_projection_carries_its_assumptions(cfg, conn):
    p = health.projection(conn, cfg, tp={
        "cycles_per_year": 77.0, "mean_daily_dod_pct": 21.0,
        "mean_resting_v": None}, fade={"usable_for_projection": False,
                                       "confidence": "none"})
    joined = " ".join(p["assumptions"])
    assert "second-life" in joined
    assert "floor rather than a best guess" in joined
    assert "80%" in joined


# --- and no state of charge anywhere ------------------------------------------------

def test_battery_health_reports_no_state_of_charge(conn, cfg):
    battery_hours(conn, cfg, days=30)
    h = health.battery_health(conn, cfg)
    assert "soc" not in _numeric_keys(h)
    assert "unreliable" in h["state_of_charge_note"]


def _numeric_keys(obj, out=None):
    """Every key in a nested structure whose value is a number."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.add(k.lower())
            _numeric_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numeric_keys(v, out)
    return out


def test_no_tool_returns_a_state_of_charge_number(conn, cfg, monkeypatch):
    """The scale is not trusted, so it is not offered. A note in its place
    says why, and the note is text - nothing can be quoted off it."""
    import tools as toolsmod
    data = {"batteryVoltage": 54.4, "battSocBM": 97, "battPower": -1400,
            "battCurrent": -26.0, "battMonitorOnline": True,
            "battAhRemaining": 1200, "battMinToDischarge": 480,
            "acPower1": 700, "acPower2": 700, "mppt80PVPower": 0,
            "southArrayPVPower": 0, "westArrayPVPower": 0,
            "mep803aAction": history.GEN_STOPPED,
            "kubotaAction": history.GEN_STOPPED, "clockTime": "22:00:00",
            "pollErrors": 0, "autoGenEnabled": True}
    monkeypatch.setattr(toolsmod.history, "fetch_data", lambda *a, **k: data)
    monkeypatch.setattr(toolsmod.history, "fetch_config", lambda *a, **k: {
        "mep803a": {"startVoltage": 52.0, "stopVoltage": 56.0,
                    "maxRuntime": 120, "chargeRate": 100, "cooldown": 5},
        "kubota": {"startVoltage": 52.0, "stopVoltage": 56.0,
                   "maxRuntime": 120, "chargeRate": 70, "cooldown": 5}})
    history.record_sample(conn, data, ts=ts_at(cfg, "2026-08-20", 22))
    conn.commit()

    t = toolsmod.Tools(conn, cfg)
    for name in ("get_status", "get_battery_detail"):
        keys = _numeric_keys(getattr(t, name)())
        assert "soc_pct" not in keys and "soc" not in keys, name
        assert "batt_soc" not in keys, name
    got = t.get_voltage_at("2026-08-20 10:00 pm")
    assert "soc_pct" not in _numeric_keys(got)
    assert 97 not in [v for v in got.values() if isinstance(v, (int, float))]


def test_the_ask_prompt_sends_a_longevity_question_to_the_tool():
    import prompts
    p = prompts.ask_prompt()
    assert "battery_health" in p
    assert "years_to_80pct_combined" in p
    assert "Name the field" in p
    assert "never infer one" in p or "never infer" in p
    assert "name the measurement that is missing" in p


def test_the_health_model_decides_nothing(cfg, conn):
    """No threshold moves because of a projected year, and no run is
    scheduled to spare the battery. If a later commit wants that, this is the
    test to delete on purpose."""
    import guard
    import policy
    src = open(policy.__file__).read() + open(guard.__file__).read()
    for word in ("battery_health", "years_to_80pct", "cycle_fade",
                 "calendar_fade", "import health"):
        assert word not in src, word
