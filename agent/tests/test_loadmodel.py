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
            rate=1.5, amps=90.0, minutes=60, load_w=600.0):
    start = ts_at(cfg, day, hour)
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, start_v, "
        "stop_v, rate_v_per_h, rate_a, load_w, solo, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (gen, start, start + minutes * 60, minutes, 52.0, 52.0 + rate,
         rate, amps, load_w, solo, kind))
    conn.commit()


def add_capacity(conn, cfg, ah=2000, day="2026-08-19"):
    """Enough monitor readings for capacity_ah: 1000 Ah remaining at 50%."""
    base = ts_at(cfg, day, 2)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 53.0, 50, -1200, ah=ah / 2)


def test_a_charge_rate_is_amps_into_the_pack(conn, cfg, lm):
    for i, amps in enumerate([80.0, 90.0, 100.0]):
        add_run(conn, cfg, "mep", f"2026-08-{10+i:02d}", 2, amps=amps)
    add_capacity(conn, cfg, ah=2000)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["a"] == 90.0 and r["runs"] == 3
    assert r["capacity_ah"] == 2000
    assert r["soc_per_h"] == 4.5, "90 A into 2000 Ah is 4.5% an hour"


def test_the_rate_is_not_volts_per_hour(conn, cfg, lm):
    """The 20:09 MEP run: a real 90 A into the pack, but the terminal voltage
    barely moved because the house was drawing 7 kW at the time."""
    add_run(conn, cfg, "mep", "2026-08-10", 20, rate=0.864, amps=90.0)
    add_capacity(conn, cfg, ah=2000)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["soc_per_h"] == 4.5
    assert r["observed_v_per_h"] == 0.864, "recorded, but not what is planned from"


def test_a_run_under_an_exceptional_load_is_left_out(conn, cfg, lm):
    """Mean load is 650 W here, so a 7 kW run is well past twice it."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=900, day_wh=400)
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=90.0, load_w=600.0)
    add_run(conn, cfg, "mep", "2026-08-11", 20, amps=20.0, load_w=7000.0)
    add_capacity(conn, cfg, ah=2000)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["runs"] == 1 and r["a"] == 90.0
    assert r["excluded_load_spikes"] == 1
    assert r["mean_load_w"] == 650


def test_an_ordinary_load_is_not_a_spike(conn, cfg, lm):
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=900, day_wh=400)
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=90.0, load_w=600.0)
    add_run(conn, cfg, "mep", "2026-08-11", 2, amps=70.0, load_w=1290.0)
    add_capacity(conn, cfg, ah=2000)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["runs"] == 2 and r["excluded_load_spikes"] == 0


def test_without_a_learned_profile_nothing_can_be_called_a_spike(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=90.0, load_w=7000.0)
    add_capacity(conn, cfg, ah=2000)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["runs"] == 1 and r["mean_load_w"] is None


def test_a_rate_without_a_learned_capacity_has_no_soc_rate(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=90.0)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["a"] == 90.0 and r["soc_per_h"] is None


def test_exercise_runs_do_not_inform_charge_rate(conn, cfg, lm):
    add_run(conn, cfg, "kubota", "2026-08-10", 2, amps=60.0)
    for i in range(5):
        add_run(conn, cfg, "kubota", f"2026-08-{11+i:02d}", 9,
                kind="exercise", amps=200.0, minutes=30)
    r = lm.charge_rate("kubota", now=ts_at(cfg, "2026-08-20", 12))
    assert r["runs"] == 1 and r["a"] == 60.0


def test_solo_and_paired_rates_are_kept_apart(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, solo=1, amps=90.0)
    add_run(conn, cfg, "mep", "2026-08-11", 2, solo=0, amps=150.0)
    now = ts_at(cfg, "2026-08-20", 12)
    assert lm.charge_rate("mep", solo=True, now=now)["a"] == 90.0
    assert lm.charge_rate("mep", solo=False, now=now)["a"] == 150.0
    assert lm.charge_rates(now=now)["mep_solo"]["a"] == 90.0


def test_both_running_pools_every_generators_paired_runs(conn, cfg, lm):
    """A paired run measures the pack, not one engine, so either gen's rows
    describe the same thing."""
    add_run(conn, cfg, "mep", "2026-08-10", 2, solo=0, amps=140.0)
    add_run(conn, cfg, "kubota", "2026-08-10", 2, solo=0, amps=160.0)
    now = ts_at(cfg, "2026-08-20", 12)
    both = lm.charge_rate(None, solo=False, now=now)
    assert both["runs"] == 2 and both["a"] == 150.0
    assert lm.charge_rates(now=now)["both_running"]["a"] == 150.0


def test_runs_too_short_to_move_the_pack_are_ignored(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, minutes=5, amps=200.0)
    assert lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12)) is None


def test_a_run_that_lost_charge_is_not_a_charge_rate(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=-30.0)
    assert lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12)) is None


def test_no_runs_means_no_rate(lm):
    assert lm.charge_rate("mep") is None


def test_the_rate_phrase_reads_the_same_everywhere():
    assert loadmodel.rate_phrase({"a": 90.0, "soc_per_h": 4.5}) == \
        "90 A into the pack (4.5% SOC/h)"
    assert loadmodel.rate_phrase({"a": 90.0, "soc_per_h": None}) == \
        "90 A into the pack"
    assert loadmodel.rate_phrase(None) == "no observed rate"


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


# --- what a generator can reach in its run window ---------------------------

@pytest.fixture
def reachable(conn, cfg, lm):
    """A pack whose curve, capacity and MEP rate are all learned.

    The curve runs 52.0 V at 40% to 57.0 V at 90%, so a volt is ten points of
    state of charge; the MEP puts 90 A into a 2000 Ah pack, which is 4.5% an
    hour, or a volt every 2.2 hours.
    """
    scraped(conn, {52.0: (40, 900), 54.0: (60, 900),
                   56.0: (80, 900), 57.0: (90, 900)})
    add_capacity(conn, cfg, ah=2000)
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=90.0)
    return lm


def test_hours_to_target_is_state_of_charge_not_volts(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    rate = reachable.charge_rate("mep", now=now)
    # 54.0 V is 60%, 57.0 V is 90%: 30 points at 4.5 an hour.
    hours = reachable.hours_to_target(54.0, 57.0, rate)
    assert round(hours, 2) == round(30 / 4.5, 2)


def test_the_measured_state_of_charge_beats_the_curve_when_it_is_known(cfg,
                                                                       reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    rate = reachable.charge_rate("mep", now=now)
    assert (reachable.hours_to_target(54.0, 57.0, rate, soc_now=80)
            < reachable.hours_to_target(54.0, 57.0, rate))


def test_a_target_already_reached_takes_no_time(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    rate = reachable.charge_rate("mep", now=now)
    assert reachable.hours_to_target(57.0, 54.0, rate) == 0.0


def test_reach_says_yes_with_the_arithmetic(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    r = reachable.reach("mep", 56.0, 57.0, 3.0, soc_now=80, now=now)
    assert r["ok"] and round(r["hours"], 2) == round(10 / 4.5, 2)
    assert "57.0 reachable in 2.2 h at 90 A into the pack (4.5% SOC/h)" in r["why"]


def test_reach_says_no_with_the_arithmetic(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    r = reachable.reach("mep", 52.0, 57.0, 2.0, soc_now=40, now=now)
    assert not r["ok"]
    assert ("57.0 needs 11.1 h at 90 A into the pack (4.5% SOC/h) but the run "
            "window is 2.0 h") in r["why"]


def test_reach_without_a_rate_falls_back_to_the_assumed_one(conn, cfg, lm):
    scraped(conn, {52.0: (40, 900), 57.0: (90, 900)})
    add_capacity(conn, cfg, ah=2000)
    r = lm.reach("mep", 54.0, 57.0, 2.0, solo=True,
                 now=ts_at(cfg, "2026-08-20", 22))
    assert "140 A into the pack" in r["why"] and "assumed" in r["why"]


def test_no_rate_and_no_assumption_is_a_refusal_not_a_guess(conn, cfg, lm):
    lm.cfg = dict(cfg, assumed_charge_a={})
    r = lm.reach("mep", 54.0, 57.0, 2.0)
    assert not r["ok"] and r["hours"] is None
    assert "no observed charge rate for mep" in r["why"]


# --- a solo estimate is never the paired figure -----------------------------

def test_a_paired_rate_is_never_used_for_one_generator(conn, cfg, lm):
    """Last night's bug: the Kubota was sized at the pair's 214 A, ran its
    full two hours and never reached its stop."""
    add_run(conn, cfg, "kubota", "2026-08-10", 2, solo=0, amps=214.0)
    add_capacity(conn, cfg, ah=2000)
    now = ts_at(cfg, "2026-08-20", 12)
    rate = lm._rate_for("kubota", True, now)
    assert rate["a"] == 80.0 and rate["assumed"] is True
    assert lm.charge_rate("kubota", solo=False, now=now)["a"] == 214.0


def test_one_generators_rate_is_never_the_others(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, solo=1, amps=150.0)
    add_capacity(conn, cfg, ah=2000)
    rate = lm._rate_for("kubota", True, ts_at(cfg, "2026-08-20", 12))
    assert rate["a"] == 80.0 and rate["assumed"] is True


def test_its_own_solo_history_beats_the_assumption(conn, cfg, lm):
    add_run(conn, cfg, "kubota", "2026-08-10", 2, solo=1, amps=95.0)
    add_capacity(conn, cfg, ah=2000)
    rate = lm._rate_for("kubota", True, ts_at(cfg, "2026-08-20", 12))
    assert rate["a"] == 95.0 and not rate.get("assumed")


def test_the_pair_assumes_both_engines_when_it_has_no_history(conn, cfg, lm):
    add_capacity(conn, cfg, ah=2000)
    rate = lm._rate_for(None, False, ts_at(cfg, "2026-08-20", 12))
    assert rate["a"] == 220.0 and rate["assumed"] is True


# --- a run that had the window and fell short is evidence --------------------

def test_a_capped_run_that_fell_short_refuses_that_target(conn, cfg, lm):
    """Two hours, stopped at 55.8, so 57.0 is not reachable in two hours."""
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 120, 53.3, 55.8,
                         gen="kubota", solo=1, soc_start=45, soc_end=70)
    scraped(conn, {52.0: (40, 900), 57.0: (90, 900)})
    now = ts_at(cfg, "2026-08-20", 22)
    r = lm.reach("kubota", 53.3, 57.0, 2.0, solo=True, soc_now=45, now=now)
    assert not r["ok"] and r["hours"] is None
    assert "3 runs had the window and stopped at 55.8" in r["basis"]
    assert "57.0 was not reached" in r["why"]


def test_a_target_those_runs_did_reach_is_still_answered(conn, cfg, lm):
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 120, 53.3, 55.8,
                         gen="kubota", solo=1, soc_start=45, soc_end=70)
    now = ts_at(cfg, "2026-08-20", 22)
    r = lm.reach("kubota", 53.3, 55.5, 2.0, solo=True, soc_now=45, now=now)
    assert r["ok"] and r["basis"].startswith("observed while charging")


def test_a_run_cut_short_of_the_window_is_not_evidence(conn, cfg, lm):
    """Thirty minutes says nothing about what two hours would have done."""
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 30, 53.3, 54.2,
                         gen="kubota", solo=1, soc_start=45, soc_end=52)
    scraped(conn, {52.0: (40, 900), 57.0: (90, 900)})
    add_capacity(conn, cfg, ah=2000)
    now = ts_at(cfg, "2026-08-20", 22)
    r = lm.reach("kubota", 53.3, 57.0, 2.0, solo=True, soc_now=45, now=now)
    assert "had the window" not in (r["basis"] or "")


def test_reach_off_the_end_of_the_curve_says_so(conn, cfg, lm):
    scraped(conn, {52.0: (40, 900), 53.0: (50, 900)})
    add_capacity(conn, cfg, ah=2000)
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=90.0)
    r = lm.reach("mep", 52.0, 57.0, 2.0, now=ts_at(cfg, "2026-08-20", 22))
    assert not r["ok"] and r["hours"] is None
    assert "neither the charging nor the resting curve reaches 57.0 V" in r["why"]


def test_the_highest_reachable_target_rounds_down_to_a_half_volt(cfg, reachable):
    """From 54.0 V (60%) two hours at 4.5%/h reaches 69%, which is 54.9 V."""
    now = ts_at(cfg, "2026-08-20", 22)
    v = reachable.best_reachable_target("mep", 54.0, 2.0, ceiling=57.0,
                                        floor=52.0, soc_now=60, now=now)
    assert v == 54.5


def test_the_highest_reachable_target_is_capped_by_the_ceiling(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    v = reachable.best_reachable_target("mep", 56.0, 8.0, ceiling=57.0,
                                        floor=55.0, soc_now=80, now=now)
    assert v == 57.0


def test_a_target_below_the_floor_is_not_worth_running_for(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    assert reachable.best_reachable_target("mep", 52.0, 1.0, ceiling=57.0,
                                           floor=55.0, soc_now=40,
                                           now=now) is None


def test_volts_for_soc_inverts_the_curve(conn, cfg, lm):
    scraped(conn, {52.0: (40, 900), 54.0: (60, 900), 56.0: (80, 900)})
    assert lm.volts_for_soc(60) == 54.0
    assert lm.volts_for_soc(70) == 55.0
    assert lm.volts_for_soc(5) == 52.0, "clamped to the bottom of the curve"
    assert lm.volts_for_soc(99) == 56.0, "clamped to the top"


def test_capacity_in_ah_comes_from_the_monitor(conn, cfg, lm):
    add_capacity(conn, cfg, ah=2400)
    assert lm.capacity_ah() == 2400


def test_capacity_needs_evidence(conn, cfg, lm):
    add_sample(conn, cfg, ts_at(cfg, "2026-08-20", 2), 53.0, 50, -1200, ah=1000)
    assert lm.capacity_ah() is None


# --- the charge-side curve --------------------------------------------------

def add_charging_run(conn, cfg, day, hour, minutes, v_start, v_end, gen="mep",
                     solo=1, soc_start=40, soc_end=None, load_w=600.0,
                     amps=150.0, kind="auto"):
    """A run written as the sampler would have: one row a minute, plus the
    gen_runs row derive_gen_runs would have closed."""
    start = ts_at(cfg, day, hour)
    soc_end = soc_end if soc_end is not None else soc_start + 20
    for i in range(minutes + 1):
        frac = i / minutes
        history.record_sample(conn, {
            "batteryVoltage": round(v_start + (v_end - v_start) * frac, 2),
            "battSocBM": round(soc_start + (soc_end - soc_start) * frac),
            "battPower": 8000, "battCurrent": amps, "battAhRemaining": 1000,
            "battMonitorOnline": True,
            "mep803aAction": (history.GEN_RUNNING
                              if gen == "mep" or not solo else history.GEN_STOPPED),
            "kubotaAction": (history.GEN_RUNNING
                             if gen == "kubota" or not solo else history.GEN_STOPPED),
            "acPower1": load_w / 2, "acPower2": load_w / 2,
            "mppt80PVPower": 0, "southArrayPVPower": 0, "westArrayPVPower": 0,
        }, ts=start + i * 60)
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, start_v, "
        "stop_v, rate_v_per_h, rate_a, load_w, solo, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (gen, start, start + minutes * 60, minutes, v_start, v_end,
         (v_end - v_start) / (minutes / 60.0), amps, load_w, solo, kind))
    conn.commit()
    return start


def this_morning(conn, cfg, n=3, minutes=70):
    """Both generators, 52.0 to 56.0, in 70 minutes. What actually happened."""
    for i in range(n):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, minutes, 52.0, 56.0,
                         gen="mep", solo=0, soc_start=40, soc_end=62)


def test_the_charge_curve_reads_the_minutes_off_real_runs(conn, cfg, lm):
    this_morning(conn, cfg)
    curve = lm.charge_curve(None, solo=False, now=ts_at(cfg, "2026-08-20", 12))
    assert curve.learned and curve.runs == 3
    assert curve.minutes_between(52.0, 56.0) == (70.0, 3)


def test_reach_prefers_what_was_observed_over_either_curve(conn, cfg, lm):
    """The morning's run answers the question directly: 70 minutes."""
    this_morning(conn, cfg)
    r = lm.reach(None, 52.0, 56.0, 2.0, solo=False, soc_now=40,
                 now=ts_at(cfg, "2026-08-20", 12))
    assert r["ok"] and round(r["hours"], 3) == round(70 / 60, 3)
    assert r["basis"] == "observed while charging (both generators paired, 3 runs)"
    assert "reachable in 1.2 h, observed while charging" in r["why"]


def test_two_runs_are_not_yet_a_charge_curve(conn, cfg, lm):
    """Under three runs the resting curve is still what is used."""
    this_morning(conn, cfg, n=2)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (95, 900)})
    r = lm.reach(None, 52.0, 56.0, 2.0, solo=False, soc_now=40,
                 now=ts_at(cfg, "2026-08-20", 12))
    assert r["basis"] == "resting curve, 2 charging runs on record"
    assert "resting curve, 2 charging runs on record" in r["why"]


def test_the_charge_curve_gives_the_state_of_charge_a_voltage_really_costs(conn,
                                                                           cfg, lm):
    """56.0 V reads 62% while charging and 85% once settled. The resting curve
    would ask a run for 23 points of charge it never needs."""
    this_morning(conn, cfg)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (95, 900)})
    now = ts_at(cfg, "2026-08-20", 12)
    assert lm.charge_curve(None, solo=False, now=now).soc_for_voltage(56.0) == 62
    assert lm.soc_for_voltage(56.0) == 85


def runs_to_57(conn, cfg, n=3, minutes=100):
    for i in range(n):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, minutes, 52.0, 57.0,
                         gen="mep", solo=0, soc_start=40, soc_end=70)


def test_runs_that_reached_the_target_answer_it_from_the_minutes(conn, cfg, lm):
    runs_to_57(conn, cfg)
    scraped(conn, {52.0: (40, 900), 57.0: (95, 900)})
    r = lm.reach(None, 52.0, 57.0, 3.0, solo=False, soc_now=45,
                 now=ts_at(cfg, "2026-08-20", 12))
    assert r["basis"] == "observed while charging (both generators paired, 3 runs)"
    assert round(r["hours"], 3) == round(100 / 60, 3)


def test_the_charge_curve_prices_a_target_the_minutes_cannot_reach(conn, cfg, lm):
    """The pack is at 51.5, lower than any run has ever begun, so no run says
    how long it takes from there. The charge-side curve still prices 57.0."""
    runs_to_57(conn, cfg)
    scraped(conn, {51.0: (30, 900), 52.0: (40, 900), 57.0: (95, 900)})
    now = ts_at(cfg, "2026-08-20", 12)
    curve = lm.charge_curve(None, solo=False, now=now)
    assert curve.minutes_between(51.5, 57.0) is None, "no run started that low"
    r = lm.reach(None, 51.5, 57.0, 3.0, solo=False, soc_now=35, now=now)
    assert r["basis"] == "charging curve (both generators paired, 3 runs)"
    assert "charging curve (both generators paired, 3 runs)" in r["why"]


def test_minutes_from_mid_run_use_the_runs_that_passed_through(conn, cfg, lm):
    """A run that began at 52.0 did pass 54.0, so it can time 54.0 to 57.0 -
    optimistically, by whatever charge was already in when it got there."""
    runs_to_57(conn, cfg, minutes=100)
    curve = lm.charge_curve(None, solo=False, now=ts_at(cfg, "2026-08-20", 12))
    minutes, n = curve.minutes_between(54.0, 57.0)
    assert n == 3 and 0 < minutes < 100


def test_a_target_no_run_has_reached_falls_back_and_says_so(conn, cfg, lm):
    """Every run stopped at 56.0, so the charge curve cannot price 57.0. The
    resting curve answers instead, conservatively, and the basis admits it."""
    this_morning(conn, cfg)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (95, 900)})
    now = ts_at(cfg, "2026-08-20", 12)
    r = lm.reach(None, 52.0, 57.0, 3.0, solo=False, soc_now=40, now=now)
    assert r["basis"] == ("resting curve (both generators paired, 3 runs, "
                          "none of them reached 57.0 V)")
    assert r["hours"] is not None


def test_the_charge_side_estimate_is_shorter_than_the_resting_one(conn, cfg, lm):
    """The whole point. The Pi5 stops on the charging voltage, not the settled
    one, so the resting curve prices every target as harder than it is: the
    same 52.0 to 57.0 is 1.7 h of observed run and over 7 h of resting
    arithmetic."""
    runs_to_57(conn, cfg)
    add_run(conn, cfg, "mep", "2026-08-15", 6, amps=150.0, solo=1)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (95, 900)})
    now = ts_at(cfg, "2026-08-20", 12)
    charging = lm.reach(None, 52.0, 57.0, 8.0, solo=False, soc_now=40, now=now)
    resting = lm.reach("mep", 52.0, 57.0, 8.0, solo=True, soc_now=40, now=now)
    assert charging["basis"].startswith("observed while charging")
    assert resting["basis"].startswith("resting curve, 0 charging runs")
    assert charging["hours"] < resting["hours"] / 3


def test_a_run_under_an_exceptional_load_does_not_shape_the_curve(conn, cfg, lm):
    """Its terminal voltage is the house's, not the generator's."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=900, day_wh=400)
    this_morning(conn, cfg)
    add_charging_run(conn, cfg, "2026-08-14", 20, 70, 52.0, 52.6, gen="mep",
                     solo=0, load_w=7000.0)
    curve = lm.charge_curve(None, solo=False, now=ts_at(cfg, "2026-08-20", 12))
    assert curve.runs == 3, "the steam-bath run is not one of them"


def test_a_run_that_started_above_the_question_is_not_evidence_for_it(conn, cfg,
                                                                      lm):
    """A run beginning at 54 never charged from 52."""
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 40, 54.0, 56.0,
                         gen="mep", solo=1)
    curve = lm.charge_curve("mep", solo=True, now=ts_at(cfg, "2026-08-20", 12))
    assert curve.learned
    assert curve.minutes_between(52.0, 56.0) is None
    assert curve.minutes_between(54.0, 56.0) == (40.0, 3)


def test_the_highest_reachable_target_reads_off_the_charge_curve(conn, cfg, lm):
    """Half the 70 minutes reaches about 54.0, so it asks for 54.0."""
    this_morning(conn, cfg)
    v = lm.charge_curve(None, solo=False,
                        now=ts_at(cfg, "2026-08-20", 12)).voltage_after(52.0, 35 / 60)
    assert 53.9 <= v <= 54.1
    got = lm.best_reachable_target(None, 52.0, 35 / 60, ceiling=57.0, floor=52.0,
                                   solo=False, soc_now=40,
                                   now=ts_at(cfg, "2026-08-20", 12))
    assert got == 54.0


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


# --- recency: this year's pattern beats last year's -------------------------

def nights_of(conn, cfg, start, days, wh):
    """`days` whole nights of a given hourly load, from `start`."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    for i in range(days):
        day = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        for hour in range(24):
            add_load_hour(conn, cfg, day, hour, wh)


@pytest.fixture
def august(cfg, monkeypatch):
    """Fixed sun times: a night from 19:00 to 06:00, eleven hours long."""
    monkeypatch.setattr(loadmodel.sun, "times",
                        lambda _cfg, day=None, now=None: (
                            ts_at(cfg, "2026-08-20", 6), ts_at(cfg, "2026-08-20", 19)))
    return ts_at(cfg, "2026-08-20", 22)


def test_the_last_fortnight_wins_outright(conn, cfg, lm, august):
    """The house changed. Three Augusts of 500 Wh do not get to argue."""
    nights_of(conn, cfg, "2023-08-01", 31, 500)
    nights_of(conn, cfg, "2024-08-01", 31, 500)
    nights_of(conn, cfg, "2026-08-10", 10, 1500)
    d = lm.overnight_drawdown(now=august)
    assert d["source"] == "last 14 nights"
    assert d["wh"] == 1500 * 11, "eleven hours of the new pattern"


def test_sixty_days_when_the_fortnight_is_thin(conn, cfg, lm, august):
    nights_of(conn, cfg, "2026-07-01", 20, 1200)
    nights_of(conn, cfg, "2024-08-01", 31, 500)
    d = lm.overnight_drawdown(now=august)
    assert d["source"] == "last 60 days" and d["wh"] == 1200 * 11


def test_prior_years_when_this_year_has_nothing(conn, cfg, lm, august):
    nights_of(conn, cfg, "2024-08-01", 31, 500)
    d = lm.overnight_drawdown(now=august)
    assert d["source"] == "Aug in prior years" and d["wh"] == 500 * 11


def test_a_thin_fortnight_does_not_win(conn, cfg, lm, august):
    """Two nights is not a pattern; the tier is skipped, not trusted."""
    nights_of(conn, cfg, "2026-08-18", 2, 9000)
    nights_of(conn, cfg, "2026-07-01", 20, 1200)
    d = lm.overnight_drawdown(now=august)
    assert d["source"] == "last 60 days"


def test_anything_at_all_beats_nothing(conn, cfg, lm, august):
    """One night from an odd month is still better than saying nothing."""
    nights_of(conn, cfg, "2026-02-10", 2, 800)
    d = lm.overnight_drawdown(now=august)
    assert d["source"] == "all history" and d["nights"] == 1


def test_no_history_at_all_is_still_none(conn, cfg, lm, august):
    assert lm.overnight_drawdown(now=august) is None


def test_the_hourly_profile_follows_the_same_order(conn, cfg, lm):
    """The deficit walks this profile, so it must lean the same way."""
    now = ts_at(cfg, "2026-08-20", 22)
    nights_of(conn, cfg, "2024-08-01", 31, 500)
    assert lm.load_profile(month=8, now=now)["source"] == "Aug in prior years"
    nights_of(conn, cfg, "2026-08-10", 10, 1500)
    p = lm.load_profile(month=8, now=now)
    assert p["source"] == "last 14 nights" and p["profile"][3] == 1500


def test_the_cleaned_rows_are_not_re_read_for_every_hour(conn, cfg, lm,
                                                         monkeypatch):
    """A 24 hour walk asks for the profile 24 times."""
    build_load_history(conn, cfg, days=30, start="2026-08-01")
    reads = []
    real = lm.conn.execute
    monkeypatch.setattr(type(lm), "conn", property(
        lambda self: type("C", (), {
            "execute": lambda _s, *a, **k: (reads.append(a[0]), real(*a, **k))[1]})()))
    lm.load_forecast(12, now=ts_at(cfg, "2026-08-20", 20))
    built = [q for q in reads if "SELECT hour_ts" in q]
    assert len(built) == 1, "the rows are built once, not once an hour"


# --- the overnight deficit (POLICY 4) ---------------------------------------

@pytest.fixture
def deficit_pack(conn, cfg, lm, monkeypatch):
    """A learned pack: 52.0 V is 40% of a 100 kWh bank, 1,000 Wh a night hour."""
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])   # night, no sun
    scraped(conn, {52.0: (40, 900), 54.0: (60, 900),
                   56.0: (80, 900), 57.0: (90, 900)})
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=1000, day_wh=1000)
    base = ts_at(cfg, "2026-08-20", 20)
    for i in range(20):
        # 1,850 Ah at 100 kWh: 1,000 Ah remaining is 54% of it.
        add_sample(conn, cfg, base + i * 60, 54.0, 60, -1000, ah=1000)
    return lm


def test_the_deficit_is_what_the_night_needs_less_what_the_pack_holds(cfg,
                                                                      deficit_pack):
    now = ts_at(cfg, "2026-08-20", 21)
    sunrise = ts_at(cfg, "2026-08-21", 6)
    d = deficit_pack.overnight_deficit(sunrise, now=now)
    assert d["needed_wh"] == 9000, "nine hours at a kilowatt"
    # 60% now, 40% at the floor: a fifth of the pack.
    assert d["available_wh"] == round(0.20 * d["capacity_wh"])
    assert d["deficit_wh"] == d["needed_wh"] - d["available_wh"]


def test_a_pack_with_room_to_spare_has_a_negative_deficit(cfg, deficit_pack):
    now = ts_at(cfg, "2026-08-20", 23)
    d = deficit_pack.overnight_deficit(ts_at(cfg, "2026-08-21", 2), now=now)
    assert d["deficit_wh"] < 0


def test_the_deficit_says_why_it_cannot_be_computed(conn, cfg, lm, monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    now = ts_at(cfg, "2026-08-20", 21)
    d = lm.overnight_deficit(ts_at(cfg, "2026-08-21", 6), now=now)
    assert d["deficit_wh"] is None and "no battery monitor sample" in d["reason"]


def test_a_sunrise_already_past_is_not_a_deficit(cfg, deficit_pack):
    now = ts_at(cfg, "2026-08-20", 21)
    d = deficit_pack.overnight_deficit(now - 3600, now=now)
    assert d["deficit_wh"] is None and "no sunrise to reach" in d["reason"]


def test_the_deficit_reports_which_load_history_it_used(cfg, deficit_pack):
    now = ts_at(cfg, "2026-08-20", 21)
    d = deficit_pack.overnight_deficit(ts_at(cfg, "2026-08-21", 6), now=now)
    assert d["source"] == "last 14 nights"


# --- the target the deficit implies -----------------------------------------

def test_the_target_is_the_deficit_plus_its_margin_in_volts(cfg, deficit_pack):
    """9,000 Wh and 15% is 10,350: 10.35% of a 100 kWh pack. From 60% that is
    70.35%, which the curve puts at 55.2 V, rounded up to 55.5."""
    t = deficit_pack.topup_target(9000, 15, 60, 100000, low=55.0, high=57.0)
    assert t["padded_wh"] == 10350 and t["target_soc"] == 70.3
    assert t["volts"] == 55.5
    assert t["basis"] == "resting curve"


def test_the_target_is_rounded_up_never_down(cfg, deficit_pack):
    """Too much is minutes of run time. Too little is not getting through."""
    t = deficit_pack.topup_target(100, 0, 60, 100000, low=52.0, high=57.0)
    assert t["uncapped_volts"] == 54.5, "60.1% is 54.005 V, which rounds up"


def test_the_target_is_clamped_at_both_ends(cfg, deficit_pack):
    assert deficit_pack.topup_target(100, 0, 60, 100000,
                                     low=55.0, high=57.0)["volts"] == 55.0
    assert deficit_pack.topup_target(60000, 0, 60, 100000,
                                     low=55.0, high=57.0)["volts"] == 57.0


def test_the_target_uses_the_charge_curve_when_there_is_one(conn, cfg,
                                                            deficit_pack):
    """The stop is a terminal voltage read while charging, so the charge-side
    curve is what turns the charge wanted into the voltage to stop at."""
    this_morning(conn, cfg)
    t = deficit_pack.topup_target(9000, 15, 60, 100000, low=55.0, high=57.0,
                                  solo=False)
    assert t["basis"].startswith("charging curve")


def test_no_curve_at_all_gives_no_target(conn, cfg, lm):
    assert lm.topup_target(9000, 15, 60, 100000, low=55.0, high=57.0) is None
