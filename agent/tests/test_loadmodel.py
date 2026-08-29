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
