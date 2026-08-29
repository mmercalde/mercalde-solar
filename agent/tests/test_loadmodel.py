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


def test_reach_without_a_rate_is_a_refusal_not_a_guess(conn, cfg, lm):
    r = lm.reach("mep", 54.0, 57.0, 2.0)
    assert not r["ok"] and r["hours"] is None
    assert "no observed charge rate for mep" in r["why"]


def test_reach_off_the_end_of_the_curve_says_so(conn, cfg, lm):
    scraped(conn, {52.0: (40, 900), 53.0: (50, 900)})
    add_capacity(conn, cfg, ah=2000)
    add_run(conn, cfg, "mep", "2026-08-10", 2, amps=90.0)
    r = lm.reach("mep", 52.0, 57.0, 2.0, now=ts_at(cfg, "2026-08-20", 22))
    assert not r["ok"] and r["hours"] is None
    assert "does not reach 57.0 V" in r["why"]


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
