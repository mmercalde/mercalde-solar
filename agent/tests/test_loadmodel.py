"""The learned model: load profile, charge rates, projection, learning gate."""

from datetime import datetime, timedelta

import pytest

import history
import loadmodel
import weather


def ts_at(cfg, day, hour):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, tzinfo=tz).timestamp())


def add_load_hour(conn, cfg, day, hour, wh, source="live"):
    history.put_hourly(conn, ts_at(cfg, day, hour), "load",
                       None, None, None, wh, None, None, 60, source)


def build_load_history(conn, cfg, days=30, start="2026-07-01",
                       night_wh=900, day_wh=400):
    """A month of hourly load: heavier at night, lighter while the sun is up."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    for i in range(days):
        day = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        for hour in range(24):
            wh = day_wh if 7 <= hour < 19 else night_wh
            add_load_hour(conn, cfg, day, hour, wh)


@pytest.fixture
def lm(conn, cfg):
    return loadmodel.LoadModel(conn, cfg)


# --- load profile -----------------------------------------------------------

def test_load_profile_learns_hour_of_day(conn, cfg, lm):
    build_load_history(conn, cfg)
    p = lm.load_profile(month=7, weekend=False)
    assert p["hours_covered"] == 24
    assert p["profile"][3] == 900     # night
    assert p["profile"][12] == 400    # daylight


def test_load_profile_needs_evidence_before_it_reports_an_hour(conn, cfg, lm):
    add_load_hour(conn, cfg, "2026-07-01", 3, 900)
    add_load_hour(conn, cfg, "2026-07-02", 3, 900)
    assert lm.load_profile()["profile"] == {}, "two observations is not a profile"


def test_generator_hours_are_excluded_from_the_load_profile(conn, cfg, lm):
    """AC output during a generator run is not house load."""
    build_load_history(conn, cfg)
    # A run at 02:00 on every day of the month, with an absurd AC reading.
    for i in range(30):
        day = (datetime(2026, 7, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
        add_load_hour(conn, cfg, day, 2, 99000)
        start = ts_at(cfg, day, 2)
        conn.execute(
            "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, kind) "
            "VALUES ('mep', ?, ?, 50, 'auto')", (start, start + 3000))
    conn.commit()
    p = lm.load_profile(month=7, weekend=False)
    assert 2 not in p["profile"], "the generator hour must not appear at all"
    assert p["profile"][3] == 900


def test_load_forecast_totals_the_profile(conn, cfg, lm):
    build_load_history(conn, cfg)
    now = ts_at(cfg, "2026-07-20", 20)
    f = lm.load_forecast(4, now=now)
    assert f["learned"] and f["hours_unknown"] == 0
    assert f["total_wh"] == 900 * 4          # 20,21,22,23 are all night hours
    assert [h["hour"] for h in f["by_hour"]] == [20, 21, 22, 23]


def test_load_forecast_falls_back_when_the_month_is_unseen(conn, cfg, lm):
    build_load_history(conn, cfg)          # July only
    f = lm.load_forecast(3, now=ts_at(cfg, "2026-12-05", 21))
    assert f["learned"], "December should fall back to the all-month profile"


def test_load_forecast_reports_ignorance_with_no_history(lm):
    f = lm.load_forecast(6)
    assert not f["learned"] and f["hours_unknown"] == 6 and f["total_wh"] == 0


# --- charge rates -----------------------------------------------------------

def add_run(conn, cfg, gen, day, hour, kind="auto", solo=1,
            rate=1.5, amps=90.0, minutes=60):
    start = ts_at(cfg, day, hour)
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, start_v, "
        "stop_v, rate_v_per_h, rate_a, solo, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (gen, start, start + minutes * 60, minutes, 52.0, 52.0 + rate,
         rate, amps, solo, kind))
    conn.commit()


def test_charge_rate_is_the_median_of_real_runs(conn, cfg, lm):
    for i, rate in enumerate([1.2, 1.5, 1.8]):
        add_run(conn, cfg, "mep", f"2026-08-{10+i:02d}", 2, rate=rate)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["v_per_h"] == 1.5 and r["runs"] == 3


def test_exercise_runs_do_not_inform_charge_rate(conn, cfg, lm):
    add_run(conn, cfg, "kubota", "2026-08-10", 2, rate=1.0)
    for i in range(5):
        add_run(conn, cfg, "kubota", f"2026-08-{11+i:02d}", 9,
                kind="exercise", rate=9.9, minutes=30)
    r = lm.charge_rate("kubota", now=ts_at(cfg, "2026-08-20", 12))
    assert r["runs"] == 1 and r["v_per_h"] == 1.0


def test_solo_and_paired_rates_are_kept_apart(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, solo=1, rate=1.0)
    add_run(conn, cfg, "mep", "2026-08-11", 2, solo=0, rate=2.0)
    now = ts_at(cfg, "2026-08-20", 12)
    assert lm.charge_rate("mep", solo=True, now=now)["v_per_h"] == 1.0
    assert lm.charge_rate("mep", solo=False, now=now)["v_per_h"] == 2.0
    assert lm.charge_rates(now=now)["mep_solo"]["v_per_h"] == 1.0


def test_runs_too_short_to_move_the_pack_are_ignored(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, minutes=5, rate=12.0)
    assert lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12)) is None


def test_no_runs_means_no_rate(lm):
    assert lm.charge_rate("mep") is None


# --- battery and projection -------------------------------------------------

def add_sample(conn, cfg, ts, v, soc, power, ah=None, gen=False):
    history.record_sample(conn, {
        "batteryVoltage": v, "battSocBM": soc, "battPower": power,
        "battCurrent": power / v, "battAhRemaining": ah,
        "battMonitorOnline": True,
        "mep803aAction": history.GEN_RUNNING if gen else history.GEN_STOPPED,
        "kubotaAction": history.GEN_STOPPED,
        "acPower1": 500, "acPower2": 500,
        "mppt80PVPower": 0, "southArrayPVPower": 0, "westArrayPVPower": 0,
    }, ts=ts)


def test_soc_for_voltage_uses_only_discharging_samples(conn, cfg, lm):
    base = ts_at(cfg, "2026-08-20", 2)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 52.0, 40, -1200)
    # Charging samples at the same voltage sit at a very different SOC.
    for i in range(20):
        add_sample(conn, cfg, base + (100 + i) * 60, 52.0, 95, 3000, gen=True)
    assert lm.soc_for_voltage(52.0) == 40


def test_soc_for_voltage_needs_enough_observations(conn, cfg, lm):
    base = ts_at(cfg, "2026-08-20", 2)
    for i in range(5):
        add_sample(conn, cfg, base + i * 60, 52.0, 40, -1200)
    assert lm.soc_for_voltage(52.0) is None


# --- the curve learned from backfilled history ------------------------------

def scraped(conn, points, day="2025-08-01"):
    """points: {volts: (soc, n)} written as the scraper would."""
    counts = {(history.soc_bin(v), soc): n for v, (soc, n) in points.items()}
    history.record_soc_observations(conn, day, counts)


def test_the_curve_is_learned_from_the_backfill_alone(conn, cfg, lm):
    """The live table is empty; the projection must still know SOC at 52 V."""
    scraped(conn, {52.0: (40, 500), 53.0: (60, 500), 54.0: (80, 500)})
    assert lm.soc_for_voltage(52.0) == 40
    assert conn.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"] == 0


def test_soc_at_the_start_threshold_is_available_from_day_one(conn, cfg, lm):
    scraped(conn, {51.8: (36, 300), 52.2: (44, 300), 53.0: (60, 300)})
    soc = lm.soc_for_voltage(cfg["default_start"])
    assert soc == pytest.approx(40.0, abs=0.5), "interpolated between 51.8 and 52.2"
    status = lm.soc_curve_status()
    assert status["start_threshold_v"] == cfg["default_start"]
    assert status["soc_at_start_threshold"] == pytest.approx(40.0, abs=0.5)


def test_the_curve_interpolates_between_observed_bins(conn, cfg, lm):
    scraped(conn, {52.0: (40, 200), 54.0: (80, 200)})
    assert lm.soc_for_voltage(53.0) == pytest.approx(60.0, abs=0.5)


def test_the_curve_will_not_extrapolate_beyond_what_it_saw(conn, cfg, lm):
    scraped(conn, {53.0: (60, 200), 54.0: (80, 200)})
    assert lm.soc_for_voltage(50.0) is None
    assert lm.soc_for_voltage(57.0) is None
    # Within the half-bin rounding margin the endpoint still stands.
    assert lm.soc_for_voltage(52.98) == 60


def test_live_and_scraped_observations_are_pooled(conn, cfg, lm):
    """Both are the same shunt, so they weigh the same."""
    scraped(conn, {52.0: (40, 8)})
    assert lm.soc_for_voltage(52.0) is None, "8 observations is under the floor"
    base = ts_at(cfg, "2026-08-20", 2)
    for i in range(4):
        add_sample(conn, cfg, base + i * 60, 52.0, 40, -1200)
    assert lm.soc_for_voltage(52.0) == 40, "12 pooled observations clears it"


def test_a_thin_bin_is_dropped_from_the_curve(conn, cfg, lm):
    scraped(conn, {52.0: (40, 500), 53.0: (99, 2), 54.0: (80, 500)})
    volts = [v for v, _, _ in lm.voltage_soc_curve()]
    assert 53.0 not in [round(v, 2) for v in volts]
    # and the outlier does not bend the interpolation
    assert lm.soc_for_voltage(53.0) == pytest.approx(60.0, abs=0.5)


def test_the_median_resists_an_outlier_within_a_bin(conn, cfg, lm):
    counts = {(history.soc_bin(52.0), 40): 500, (history.soc_bin(52.0), 5): 40}
    history.record_soc_observations(conn, "2025-08-01", counts)
    assert lm.soc_for_voltage(52.0) == 40


def test_curve_status_reports_its_span(conn, cfg, lm):
    scraped(conn, {52.0: (40, 100), 55.0: (90, 100)})
    st = lm.soc_curve_status()
    assert st["points"] == 2
    assert st["volts_low"] == 52.0 and st["volts_high"] == 55.0
    assert st["observations"] == 200 and st["scraped_observations"] == 200


def test_no_history_reports_no_curve(lm):
    st = lm.soc_curve_status()
    assert st["points"] == 0 and st["soc_at_start_threshold"] is None


def test_projection_works_from_backfill_without_live_soc(conn, cfg, lm, monkeypatch):
    """The reported bug: 483 days of history, yet no SOC at 52.0 V."""
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    build_load_history(conn, cfg, days=30, start="2026-08-01", night_wh=1000)
    scraped(conn, {52.0: (50, 900), 54.0: (75, 900)})
    base = ts_at(cfg, "2026-08-20", 22)
    # Live samples supply only the present state and the pack size.
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 54.0, 60, -1000, ah=1200)
    p = lm.project_voltage(52.0, now=base + 1300)
    assert p["reached"] is not None, p.get("reason")
    assert p["soc_target"] == 50


def test_the_projection_says_when_the_curve_is_too_narrow(conn, cfg, lm, monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    scraped(conn, {55.0: (85, 400), 56.0: (95, 400)})
    base = ts_at(cfg, "2026-08-20", 22)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 55.5, 90, -1000, ah=1200)
    p = lm.project_voltage(52.0, now=base)
    assert p["reached"] is None
    assert "only covers 55.0-56.0 V" in p["reason"]


def test_with_no_curve_at_all_the_projection_points_at_the_backfill(conn, cfg, lm,
                                                                    monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    base = ts_at(cfg, "2026-08-20", 22)
    # Too few readings for any bin to qualify, so there is no curve at all.
    for i in range(4):
        add_sample(conn, cfg, base + i * 60, 54.0, 60, -1000, ah=1200)
    p = lm.project_voltage(52.0, now=base)
    assert p["reached"] is None and "--backfill" in p["reason"]


def test_capacity_from_ah_remaining(conn, cfg, lm):
    base = ts_at(cfg, "2026-08-20", 2)
    for i in range(20):
        # 1000 Ah remaining at 50% SOC and 53 V -> 2000 Ah -> 106 kWh
        add_sample(conn, cfg, base + i * 60, 53.0, 50, -1200, ah=1000)
    assert lm.capacity_wh() == 106000


def test_project_voltage_walks_the_load_forward(conn, cfg, lm, monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])   # night, no sun
    build_load_history(conn, cfg, days=30, start="2026-08-01", night_wh=1000)
    base = ts_at(cfg, "2026-08-20", 22)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 54.0, 60, -1000, ah=1200)
    for i in range(20):
        add_sample(conn, cfg, base - 86400 + i * 60, 52.0, 50, -1000, ah=1000)

    p = lm.project_voltage(52.0, now=base + 1300)
    assert p["reached"] is not None
    assert p["soc_target"] == 50 and p["soc_now"] == 60
    # 10% of a ~127 kWh pack at 1000 Wh/h is about 12.7 h.
    assert 10 < p["hours"] < 15


def test_project_voltage_says_why_it_cannot(conn, cfg, lm, monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    base = ts_at(cfg, "2026-08-20", 22)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 54.0, 60, -1000, ah=1200)
    p = lm.project_voltage(52.0, now=base)
    assert p["reached"] is None and "52.0 V" in p["reason"]


def test_project_voltage_when_already_below_target(conn, cfg, lm, monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    build_load_history(conn, cfg, days=30, start="2026-08-01")
    base = ts_at(cfg, "2026-08-20", 22)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 51.5, 45, -1000, ah=900)
    for i in range(20):
        add_sample(conn, cfg, base - 86400 + i * 60, 52.0, 50, -1000, ah=1000)
    p = lm.project_voltage(52.0, now=base + 1300)
    assert p["hours"] == 0.0
    # The first live night printed "?" here, from 03:10 on, when the pack sat
    # within 0.7 V of 52. There is nothing unknown about it.
    assert p["at"] == "now"
    assert lm.projection_label(p, base + 1300) == "now"


def test_a_projection_inside_the_quarter_hour_is_a_window_not_a_clock(conn, cfg, lm,
                                                                      monkeypatch):
    """A minute-precise time ten minutes out is spurious precision."""
    base = ts_at(cfg, "2026-08-20", 22)
    assert lm.projection_label({"reached": base + 600}, base) == "≤ 15 min"
    assert lm.projection_label({"reached": base + 1200}, base) == "10:20 pm"


def test_a_projection_that_was_never_reached_has_no_label(lm):
    assert lm.projection_label({"reached": None, "reason": "x"}) is None
    assert lm.projection_label(None) is None


def test_a_projection_missing_its_label_is_derived_not_dashed(conn, cfg, lm):
    """Nothing may put "?" back: an old record without `at` still reads."""
    base = ts_at(cfg, "2026-08-20", 22)
    assert lm.projection_label({"reached": base + 7200}, base) == "12:00 am"


# --- solar ------------------------------------------------------------------

def test_solar_model_learns_cloud_derating(conn, cfg, lm, monkeypatch):
    """A clear day yields 60 kWh; yield falls linearly with cloud cover."""
    arch = {}
    for i in range(20):
        day = f"2026-08-{i+1:02d}"
        cloud = i * 5
        conn.execute("INSERT INTO daily (day, solar_wh) VALUES (?,?)",
                     (day, 60000 * (1 - 0.6 * cloud / 100.0)))
        arch[day] = {"cloud": cloud, "radiation_mj": 28.0}
    conn.commit()
    monkeypatch.setattr(weather, "archive_daily", lambda *a, **k: arch)

    m = lm.solar_model(month=8)
    assert m["learned"] and m["days"] == 20
    assert m["clear_day_wh"] == 60000
    assert m["cloud_derate"] == pytest.approx(0.6, abs=0.02)

    est = lm.estimate_solar_wh(50, month=8)
    assert est["wh"] == pytest.approx(42000, rel=0.05)


def test_solar_model_will_not_fit_on_thin_evidence(conn, cfg, lm, monkeypatch):
    monkeypatch.setattr(weather, "archive_daily", lambda *a, **k: {})
    for i in range(3):
        conn.execute("INSERT INTO daily (day, solar_wh) VALUES (?,?)",
                     (f"2026-08-{i+1:02d}", 50000))
    conn.commit()
    assert lm.solar_model(month=8)["learned"] is False
    assert lm.estimate_solar_wh(20, month=8) is None


# --- learning gate (guard rule 6) ------------------------------------------

def test_gate_is_shut_with_no_history(lm):
    s = lm.learning_status()
    assert not s["open"] and not s["has_prior_year"] and not s["has_live_days"]


def test_gate_needs_the_same_month_from_a_prior_year(conn, cfg, lm):
    now = ts_at(cfg, "2026-08-20", 12)
    for i in range(10):
        add_load_hour(conn, cfg, f"2025-08-{i+1:02d}", 12, 500, source="insightlocal")
    s = lm.learning_status(now=now)
    assert s["has_prior_year"] and s["prior_year_months"] == [2025]
    assert not s["open"], "prior-year history alone does not open the gate"


def test_gate_needs_consecutive_live_days(conn, cfg, lm):
    now = ts_at(cfg, "2026-08-20", 12)
    for d in range(1, 8):
        add_sample(conn, cfg, ts_at(cfg, f"2026-08-{d:02d}", 12), 54.0, 80, -1000)
    s = lm.learning_status(now=now)
    assert s["live_days"] == 7 and s["has_live_days"]
    assert not s["open"], "live days alone do not open the gate"


def test_gap_breaks_the_consecutive_day_streak(conn, cfg, lm):
    now = ts_at(cfg, "2026-08-20", 12)
    for d in [1, 2, 3, 5, 6, 7, 8]:
        add_sample(conn, cfg, ts_at(cfg, f"2026-08-{d:02d}", 12), 54.0, 80, -1000)
    s = lm.learning_status(now=now)
    assert s["live_days"] == 4 and not s["has_live_days"]


def test_gate_opens_when_both_conditions_hold(conn, cfg, lm):
    now = ts_at(cfg, "2026-08-20", 12)
    for i in range(10):
        add_load_hour(conn, cfg, f"2025-08-{i+1:02d}", 12, 500, source="insightlocal")
    for d in range(1, 9):
        add_sample(conn, cfg, ts_at(cfg, f"2026-08-{d:02d}", 12), 54.0, 80, -1000)
    assert lm.learning_status(now=now)["open"]


# --- the curve must not turn back on itself ---------------------------------

def test_the_curve_is_forced_monotonic(conn, cfg, lm):
    """Observed live: surface charge put 54.1 V at 88% and 55.6 V at 82%.
    SOC cannot fall as voltage rises."""
    scraped(conn, {54.1: (88, 5795), 54.85: (84, 2326),
                   55.6: (82, 2320), 56.35: (85, 1574), 57.1: (88, 2093)})
    curve = lm.voltage_soc_curve()
    socs = [s for _, s, _ in curve]
    assert socs == sorted(socs), f"not monotonic: {socs}"


def test_an_already_monotonic_curve_is_left_alone(conn, cfg, lm):
    scraped(conn, {52.0: (40, 100), 53.0: (60, 100), 54.0: (80, 100)})
    assert [round(s) for _, s, _ in lm.voltage_soc_curve()] == [40, 60, 80]


def test_pooling_is_weighted_by_observations(conn, cfg, lm):
    """A bin with far more observations should dominate the merged value."""
    scraped(conn, {53.0: (90, 1000), 54.0: (10, 10)})
    socs = [s for _, s, _ in lm.voltage_soc_curve()]
    assert socs[0] == socs[1], "the inversion is pooled into one level"
    assert socs[0] > 80, f"the 1000-observation bin should dominate: {socs[0]}"


def test_monotonicity_does_not_disturb_the_low_end(conn, cfg, lm):
    """The dip is above 54 V; the start threshold must keep its own value."""
    scraped(conn, {51.85: (77, 2164), 52.6: (80, 3197), 53.35: (83, 4934),
                   54.1: (88, 5795), 54.85: (84, 2326), 55.6: (82, 2320)})
    assert lm.soc_for_voltage(52.0) == pytest.approx(77.4, abs=0.5)
