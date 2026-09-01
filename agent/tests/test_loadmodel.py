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


def add_discharge_night(conn, cfg, night, top_v=56.0, bottom_v=52.0,
                        wh_per_hour=1000, hours=(20, 21, 22, 23, 0, 1, 2, 3),
                        source="live"):
    """One clean overnight discharge, for the Wh-vs-V curve.

    The pack walks down in equal steps while the house draws the same each
    hour, so the curve is a flat Wh per volt and a test can state what it
    should say: 56.0 to 52.0 over eight hours at 1,000 Wh is 2,000 Wh a volt.
    """
    d0 = datetime.strptime(night, "%Y-%m-%d").date()
    step = (top_v - bottom_v) / len(hours)
    for i, h in enumerate(hours):
        day = d0 if h >= 19 else d0 + timedelta(days=1)
        ts = ts_at(cfg, day.strftime("%Y-%m-%d"), h)
        hi = top_v - i * step
        lo = hi - step
        history.put_hourly(conn, ts, "battery", (hi + lo) / 2, None, 0,
                           wh_per_hour, lo, hi, 60, source)
        history.put_hourly(conn, ts, "load", None, None, None, wh_per_hour,
                           None, None, 60, source)
        history.put_hourly(conn, ts, "solar", None, None, 0, None, None, None,
                           60, source)
        history.put_hourly(conn, ts, "gen", None, None, 0, None, None, None,
                           60, source)
    conn.commit()


def add_discharge_nights(conn, cfg, first="2026-08-10", n=5, **kw):
    d0 = datetime.strptime(first, "%Y-%m-%d").date()
    for i in range(n):
        add_discharge_night(conn, cfg, (d0 + timedelta(days=i)).strftime("%Y-%m-%d"),
                            **kw)


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
            rate=1.5, gross_w=5400.0, minutes=60, load_w=600.0):
    """One recorded run. gross_w is what the generator delivered; load_w is
    what the house took while it did."""
    start = ts_at(cfg, day, hour)
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, start_v, "
        "stop_v, rate_v_per_h, rate_a, load_w, gross_w, solo, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (gen, start, start + minutes * 60, minutes, 52.0, 52.0 + rate,
         rate, (gross_w - load_w) / 53.0, load_w, gross_w, solo, kind))
    conn.commit()


def add_capacity(conn, cfg, ah=2000, day="2026-08-19"):
    """Enough monitor readings for capacity_ah: 1000 Ah remaining at 50%."""
    base = ts_at(cfg, day, 2)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 53.0, 50, -1200, ah=ah / 2)


def test_a_charge_rate_is_what_the_generator_delivers(conn, cfg, lm):
    """Gross: into the pack plus out to the house at the same minute."""
    for i, gross in enumerate([5000.0, 5400.0, 5800.0]):
        add_run(conn, cfg, "mep", f"2026-08-{10+i:02d}", 2, gross_w=gross)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["gross_w"] == 5400 and r["runs"] == 3
    assert r["run_load_w"] == 600


def test_the_rate_does_not_move_with_the_house(conn, cfg, lm):
    """The whole point. The same engine under a 7 kW load and under 600 W
    delivers the same gross, so both runs teach the same figure."""
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=5400.0, load_w=600.0)
    add_run(conn, cfg, "mep", "2026-08-11", 20, gross_w=5400.0, load_w=7000.0)
    add_run(conn, cfg, "mep", "2026-08-12", 2, gross_w=5400.0, load_w=1500.0)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["gross_w"] == 5400 and r["runs"] == 3, "no run is thrown away"


def test_there_is_no_load_filter_any_more(conn, cfg, lm):
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=900, day_wh=400)
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=5400.0, load_w=600.0)
    add_run(conn, cfg, "mep", "2026-08-11", 20, gross_w=5400.0, load_w=9000.0)
    r = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert r["runs"] == 2
    assert "excluded_load_spikes" not in r


def test_the_net_is_the_gross_less_what_the_window_expects(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=7100.0)
    add_capacity(conn, cfg, ah=2000)
    rate = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    net, soc_per_h = lm.net_from_gross(rate, 1900)
    assert net == 5200
    assert soc_per_h == round(100.0 * 5200 / rate["capacity_wh"], 2)


def test_a_generator_the_house_outruns_has_no_rate_to_quote(conn, cfg, lm):
    add_run(conn, cfg, "kubota", "2026-08-10", 2, gross_w=4000.0)
    add_capacity(conn, cfg, ah=2000)
    rate = lm.charge_rate("kubota", now=ts_at(cfg, "2026-08-20", 12))
    net, soc_per_h = lm.net_from_gross(rate, 4500)
    assert net == -500 and soc_per_h is None


def test_the_expected_load_comes_from_the_hour_and_the_day(conn, cfg, lm):
    """Night hours are 900 Wh here and daylight 400, by hour of day."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=900, day_wh=400)
    assert lm.expected_load_w(now=ts_at(cfg, "2026-08-20", 20), hours=2) == 900
    assert lm.expected_load_w(now=ts_at(cfg, "2026-08-20", 12), hours=2) == 400


def test_no_learned_profile_means_no_expected_load(conn, cfg, lm):
    assert lm.expected_load_w(now=ts_at(cfg, "2026-08-20", 20), hours=2) is None


def test_a_rate_without_a_learned_capacity_has_no_soc_rate(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=5400.0)
    rate = lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12))
    assert rate["gross_w"] == 5400
    assert lm.net_from_gross(rate, 600)[1] is None


def test_exercise_runs_do_not_inform_charge_rate(conn, cfg, lm):
    add_run(conn, cfg, "kubota", "2026-08-10", 2, gross_w=4000.0)
    for i in range(5):
        add_run(conn, cfg, "kubota", f"2026-08-{11+i:02d}", 9,
                kind="exercise", gross_w=20000.0, minutes=30)
    r = lm.charge_rate("kubota", now=ts_at(cfg, "2026-08-20", 12))
    assert r["runs"] == 1 and r["gross_w"] == 4000


def test_solo_and_paired_rates_are_kept_apart(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, solo=1, gross_w=5400.0)
    add_run(conn, cfg, "mep", "2026-08-11", 2, solo=0, gross_w=12000.0)
    now = ts_at(cfg, "2026-08-20", 12)
    assert lm.charge_rate("mep", solo=True, now=now)["gross_w"] == 5400
    assert lm.charge_rate("mep", solo=False, now=now)["gross_w"] == 12000
    assert lm.charge_rates(now=now)["mep_solo"]["gross_w"] == 5400


def test_both_running_pools_every_generators_paired_runs(conn, cfg, lm):
    """A paired run measures the pack, not one engine, so either gen's rows
    describe the same thing."""
    add_run(conn, cfg, "mep", "2026-08-10", 2, solo=0, gross_w=11000.0)
    add_run(conn, cfg, "kubota", "2026-08-10", 2, solo=0, gross_w=13000.0)
    now = ts_at(cfg, "2026-08-20", 12)
    both = lm.charge_rate(None, solo=False, now=now)
    assert both["runs"] == 2 and both["gross_w"] == 12000
    assert lm.charge_rates(now=now)["both_running"]["gross_w"] == 12000


def test_runs_too_short_to_move_the_pack_are_ignored(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, minutes=5, gross_w=20000.0)
    assert lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12)) is None


def test_a_run_that_delivered_nothing_is_not_a_charge_rate(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=-100.0)
    assert lm.charge_rate("mep", now=ts_at(cfg, "2026-08-20", 12)) is None


def test_no_runs_means_no_rate(lm):
    assert lm.charge_rate("mep") is None


def test_the_rate_phrase_shows_both_halves():
    """The owner sees what the engine gives and what the house takes."""
    assert loadmodel.rate_phrase({"gross_w": 7100}, 1900, 5200, 4.9) == \
        "gross 7.1 kW − expected load 1.9 kW = 5.2 kW into pack (4.9% SOC/h)"
    assert loadmodel.rate_phrase({"gross_w": 7100}) == "gross 7.1 kW"
    assert loadmodel.rate_phrase(
        {"gross_w": 7400, "assumed": True, "assumed_net": True},
        1900, 7400, 7.0) == "7.4 kW into pack, assumed (7.0% SOC/h)"
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
    """The reported bug: 483 days of history, yet nothing to project from."""
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    build_load_history(conn, cfg, days=30, start="2026-08-01", night_wh=1000)
    add_discharge_nights(conn, cfg)
    base = ts_at(cfg, "2026-08-20", 22)
    # Live samples supply only the present voltage and the pack size.
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 54.0, 60, -1000, ah=1200)
    p = lm.project_voltage(52.0, now=base + 1300)
    assert p["reached"] is not None, p.get("reason")
    # 2,000 Wh a volt, two volts above the floor.
    assert p["available_wh"] == 4000
    assert p["available_source"] == "learned Wh-vs-V, 5 nights"


def test_the_projection_says_when_the_curve_is_too_narrow(conn, cfg, lm, monkeypatch):
    """Nights that never went below 54.0 cannot say what is under it."""
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    add_discharge_nights(conn, cfg, top_v=56.0, bottom_v=54.0)
    base = ts_at(cfg, "2026-08-20", 22)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 55.5, 90, -1000, ah=1200)
    p = lm.project_voltage(52.0, now=base)
    assert p["reached"] is None
    assert "no night crossed 52.00-52.25 V" in p["reason"]


def test_with_no_curve_at_all_the_projection_points_at_the_backfill(conn, cfg, lm,
                                                                    monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    base = ts_at(cfg, "2026-08-20", 22)
    # No overnight discharge on record at all.
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
    add_discharge_nights(conn, cfg)
    base = ts_at(cfg, "2026-08-20", 22)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 54.0, 60, -1000, ah=1200)

    p = lm.project_voltage(52.0, now=base + 1300)
    assert p["reached"] is not None
    # 4,000 Wh above the floor at 1,000 Wh an hour is four hours.
    assert p["available_wh"] == 4000
    assert 3.5 < p["hours"] < 4.5


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
    """A pack whose curve, capacity, house and MEP rate are all learned.

    The curve runs 52.0 V at 40% to 57.0 V at 90%, so a volt is ten points of
    state of charge. The house takes a flat 600 W, and the MEP delivers a
    gross 5,370 W, so 4,770 W goes into a ~108 kWh pack: 4.4 points an hour.
    """
    scraped(conn, {52.0: (40, 900), 54.0: (60, 900),
                   56.0: (80, 900), 57.0: (90, 900)})
    add_capacity(conn, cfg, ah=2000)
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=5370.0, load_w=600.0)
    return lm


def netted(lm, gen, now, expected_load_w=600):
    """The rate as reach() uses it: gross netted against the window's load."""
    rate = lm.charge_rate(gen, now=now)
    net, soc_per_h = lm.net_from_gross(rate, expected_load_w)
    return dict(rate, net_w=net, soc_per_h=soc_per_h), soc_per_h


def test_hours_to_target_is_state_of_charge_not_volts(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    rate, per_h = netted(reachable, "mep", now)
    # 54.0 V is 60%, 57.0 V is 90%: thirty points at the netted rate.
    hours = reachable.hours_to_target(54.0, 57.0, rate)
    assert round(hours, 2) == round(30 / per_h, 2)


def test_where_the_pack_stands_is_read_from_its_voltage(cfg, reachable):
    """It used to prefer the Battery Monitor's live state of charge when it
    was given one. There is no way to give it one now: a shunt reading high
    made every target look nearer than it was, at the moment a run was being
    decided on."""
    import inspect
    for name in ("reach", "best_reachable_target", "hours_to_target",
                 "voltage_after"):
        params = inspect.signature(getattr(reachable, name)).parameters
        assert "soc_now" not in params, f"{name} still accepts a live SOC"


def test_a_target_already_reached_takes_no_time(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    rate, _ = netted(reachable, "mep", now)
    assert reachable.hours_to_target(57.0, 54.0, rate) == 0.0


def test_reach_says_yes_with_the_arithmetic(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    r = reachable.reach("mep", 56.0, 57.0, 3.0, now=now)
    assert r["ok"] and r["net_w"] == 4770
    assert ("reachable in 2.2 h at gross 5.4 kW − expected load 0.6 kW = "
            "4.8 kW into pack") in r["why"]


def test_reach_says_no_with_the_arithmetic(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    r = reachable.reach("mep", 52.0, 57.0, 2.0, now=now)
    assert not r["ok"]
    assert "57.0 needs 11.1 h at gross 5.4 kW − expected load 0.6 kW" in r["why"]
    assert "but the run window is 2.0 h" in r["why"]


def test_reach_without_a_rate_falls_back_to_the_assumed_one(conn, cfg, lm):
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    scraped(conn, {52.0: (40, 900), 57.0: (90, 900)})
    add_capacity(conn, cfg, ah=2000)
    r = lm.reach("mep", 54.0, 57.0, 2.0, solo=True,
                 now=ts_at(cfg, "2026-08-20", 22))
    assert "7.4 kW into pack, assumed" in r["why"]


def test_no_rate_and_no_assumption_is_a_refusal_not_a_guess(conn, cfg, lm):
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    lm.cfg = dict(lm.cfg, assumed_charge_a={})
    r = lm.reach("mep", 54.0, 57.0, 2.0)
    assert not r["ok"] and r["hours"] is None
    assert "no observed charge rate for mep" in r["why"]


# --- a solo estimate is never the paired figure -----------------------------

def test_a_paired_rate_is_never_used_for_one_generator(conn, cfg, lm):
    """Last night's bug: the Kubota was sized at the pair's 214 A, ran its
    full two hours and never reached its stop."""
    add_run(conn, cfg, "kubota", "2026-08-10", 2, solo=0, gross_w=11942.0)
    add_capacity(conn, cfg, ah=2000)
    now = ts_at(cfg, "2026-08-20", 12)
    rate = lm._rate_for("kubota", True, now)
    assert rate["gross_w"] == round(80.0 * 53.0) and rate["assumed"] is True
    assert lm.charge_rate("kubota", solo=False, now=now)["gross_w"] == 11942


def test_one_generators_rate_is_never_the_others(conn, cfg, lm):
    add_run(conn, cfg, "mep", "2026-08-10", 2, solo=1, gross_w=8550.0)
    add_capacity(conn, cfg, ah=2000)
    rate = lm._rate_for("kubota", True, ts_at(cfg, "2026-08-20", 12))
    assert rate["gross_w"] == round(80.0 * 53.0) and rate["assumed"] is True


def test_its_own_solo_history_beats_the_assumption(conn, cfg, lm):
    add_run(conn, cfg, "kubota", "2026-08-10", 2, solo=1, gross_w=5635.0)
    add_capacity(conn, cfg, ah=2000)
    rate = lm._rate_for("kubota", True, ts_at(cfg, "2026-08-20", 12))
    assert rate["gross_w"] == 5635 and not rate.get("assumed")


def test_the_pair_assumes_both_engines_when_it_has_no_history(conn, cfg, lm):
    add_capacity(conn, cfg, ah=2000)
    rate = lm._rate_for(None, False, ts_at(cfg, "2026-08-20", 12))
    assert rate["gross_w"] == round(220.0 * 53.0) and rate["assumed"] is True


# --- a run that had the window and fell short is evidence --------------------

def test_a_capped_run_that_fell_short_refuses_that_target(conn, cfg, lm):
    """Two hours, stopped at 55.8, so 57.0 is not reachable in two hours."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 120, 53.3, 55.8,
                         gen="kubota", solo=1, soc_start=45, soc_end=70)
    scraped(conn, {52.0: (40, 900), 57.0: (90, 900)})
    now = ts_at(cfg, "2026-08-20", 22)
    r = lm.reach("kubota", 53.3, 57.0, 2.0, solo=True, now=now)
    assert not r["ok"] and r["hours"] is None
    assert "3 runs had the window and stopped at 55.8" in r["basis"]
    assert "57.0 was not reached" in r["why"]


def test_a_target_those_runs_did_reach_is_still_answered(conn, cfg, lm):
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 120, 53.3, 55.8,
                         gen="kubota", solo=1, soc_start=45, soc_end=70)
    now = ts_at(cfg, "2026-08-20", 22)
    r = lm.reach("kubota", 53.3, 55.5, 2.0, solo=True, now=now)
    assert r["ok"] and r["basis"].startswith("observed while charging")


def test_a_run_cut_short_of_the_window_is_not_evidence(conn, cfg, lm):
    """Thirty minutes says nothing about what two hours would have done."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 30, 53.3, 54.2,
                         gen="kubota", solo=1, soc_start=45, soc_end=52)
    scraped(conn, {52.0: (40, 900), 57.0: (90, 900)})
    add_capacity(conn, cfg, ah=2000)
    now = ts_at(cfg, "2026-08-20", 22)
    r = lm.reach("kubota", 53.3, 57.0, 2.0, solo=True, now=now)
    assert "had the window" not in (r["basis"] or "")


def test_reach_off_the_end_of_the_curve_is_estimated_and_says_so(conn, cfg, lm):
    """The curve stops at 53.0 and the target is 57.0. Carrying its 10 points
    a volt up gives 90% at 57.0, which is 50 points away and far outside a
    two hour window - and the answer says it was an estimate."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    scraped(conn, {52.0: (40, 900), 53.0: (50, 900)})
    add_capacity(conn, cfg, ah=2000)
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=5370.0)
    r = lm.reach("mep", 52.0, 57.0, 2.0, now=ts_at(cfg, "2026-08-20", 22))
    assert not r["ok"] and r["hours"] > 2.0
    assert r["basis"].startswith("estimated from the resting curve")
    assert "40% at 52.0 V to 90% at 57.0 V" in r["basis"]


def test_a_flat_top_is_not_priced_at_all(conn, cfg, lm):
    """A curve that stops rising is a full pack, and the volts above it are
    made by current against internal resistance. There is no charge in them
    to estimate."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    scraped(conn, {52.0: (99, 900), 53.0: (99, 900)})
    add_capacity(conn, cfg, ah=2000)
    add_run(conn, cfg, "mep", "2026-08-10", 2, gross_w=5370.0)
    r = lm.reach("mep", 52.0, 57.0, 2.0, now=ts_at(cfg, "2026-08-20", 22))
    assert not r["ok"] and r["hours"] is None
    assert "cannot be shown to be reachable" in r["why"]


def test_the_highest_reachable_target_rounds_down_to_a_half_volt(cfg, reachable):
    """From 54.0 V (60%) two hours at 4.5%/h reaches 69%, which is 54.9 V."""
    now = ts_at(cfg, "2026-08-20", 22)
    v = reachable.best_reachable_target("mep", 54.0, 2.0, ceiling=57.0,
                                        floor=52.0, now=now)
    assert v == 54.5


def test_the_highest_reachable_target_is_capped_by_the_ceiling(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    v = reachable.best_reachable_target("mep", 56.0, 8.0, ceiling=57.0,
                                        floor=55.0, now=now)
    assert v == 57.0


def test_a_target_below_the_floor_is_not_worth_running_for(cfg, reachable):
    now = ts_at(cfg, "2026-08-20", 22)
    assert reachable.best_reachable_target("mep", 52.0, 1.0, ceiling=57.0,
                                           floor=55.0,
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
        "stop_v, rate_v_per_h, rate_a, load_w, gross_w, solo, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (gen, start, start + minutes * 60, minutes, v_start, v_end,
         (v_end - v_start) / (minutes / 60.0), amps, load_w,
         amps * 53.0 + load_w, solo, kind))
    conn.commit()
    return start


def this_morning(conn, cfg, n=3, minutes=70):
    """Both generators, 52.0 to 56.0, in 70 minutes. What actually happened."""
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
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
    r = lm.reach(None, 52.0, 56.0, 2.0, solo=False,
                 now=ts_at(cfg, "2026-08-20", 12))
    assert r["ok"] and round(r["hours"], 3) == round(70 / 60, 3)
    assert r["basis"] == "observed while charging (both generators paired, 3 runs)"
    assert "reachable in 1.2 h, observed while charging" in r["why"]


def test_two_runs_are_not_yet_a_charge_curve(conn, cfg, lm):
    """Under three runs the resting curve is still what is used."""
    this_morning(conn, cfg, n=2)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (95, 900)})
    r = lm.reach(None, 52.0, 56.0, 2.0, solo=False,
                 now=ts_at(cfg, "2026-08-20", 12))
    assert r["basis"].startswith("estimated from the resting curve, "
                                 "2 charging runs on record")
    assert "estimated from the resting curve" in r["why"]


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
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=600, day_wh=600)
    for i in range(n):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, minutes, 52.0, 57.0,
                         gen="mep", solo=0, soc_start=40, soc_end=70)


def test_runs_that_reached_the_target_answer_it_from_the_minutes(conn, cfg, lm):
    runs_to_57(conn, cfg)
    scraped(conn, {52.0: (40, 900), 57.0: (95, 900)})
    r = lm.reach(None, 52.0, 57.0, 3.0, solo=False,
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
    r = lm.reach(None, 51.5, 57.0, 3.0, solo=False, now=now)
    assert r["basis"] == "charging curve (both generators paired, 3 runs)"
    assert "charging curve (both generators paired, 3 runs)" in r["why"]


def test_minutes_from_mid_run_use_the_runs_that_passed_through(conn, cfg, lm):
    """A run that began at 52.0 did pass 54.0, so it can time 54.0 to 57.0 -
    optimistically, by whatever charge was already in when it got there."""
    runs_to_57(conn, cfg, minutes=100)
    curve = lm.charge_curve(None, solo=False, now=ts_at(cfg, "2026-08-20", 12))
    minutes, n = curve.minutes_between(54.0, 57.0)
    assert n == 3 and 0 < minutes < 100


def test_a_target_no_run_has_reached_is_estimated_and_says_so(conn, cfg, lm):
    """Every run stopped at 56.0, so the charge curve cannot price 57.0. It is
    carried above its own top instead, and the basis admits it is an estimate.
    """
    this_morning(conn, cfg)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (95, 900)})
    now = ts_at(cfg, "2026-08-20", 12)
    r = lm.reach(None, 52.0, 57.0, 3.0, solo=False, now=now)
    assert r["basis"].startswith(
        "estimated, charging curve has no run to this voltage")
    assert r["hours"] is not None and r["hours"] > 0


def test_a_target_above_the_pack_is_never_priced_at_nothing(conn, cfg, lm):
    """2026-08-30, three times: 56.1 V "reachable in 0.0 h" for a Kubota that
    had never reached it. The shunt read 89% during a charge and the settled
    resting curve put 56.1 V at 88%, so the pack was already past a voltage
    it had never seen. Both ends come off one curve now."""
    this_morning(conn, cfg)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (88, 900)})
    now = ts_at(cfg, "2026-08-20", 12)
    r = lm.reach(None, 55.0, 56.5, 2.0, solo=False, now=now)
    assert r["hours"] is None or r["hours"] > 0
    assert "0.0 h" not in r["why"]


def test_the_charge_side_estimate_is_shorter_than_the_resting_one(conn, cfg, lm):
    """The whole point. The Pi5 stops on the charging voltage, not the settled
    one, so the resting curve prices every target as harder than it is: the
    same 52.0 to 57.0 is 1.7 h of observed run and over 7 h of resting
    arithmetic."""
    runs_to_57(conn, cfg)
    add_run(conn, cfg, "mep", "2026-08-15", 6, gross_w=8550.0, solo=1)
    scraped(conn, {52.0: (40, 900), 56.0: (85, 900), 57.0: (95, 900)})
    now = ts_at(cfg, "2026-08-20", 12)
    charging = lm.reach(None, 52.0, 57.0, 8.0, solo=False, now=now)
    resting = lm.reach("mep", 52.0, 57.0, 8.0, solo=True, now=now)
    assert charging["basis"].startswith("observed while charging")
    assert resting["basis"].startswith("estimated from the resting curve, "
                                       "0 charging runs")
    assert charging["hours"] < resting["hours"] / 3


def test_a_run_under_an_exceptional_load_now_shapes_the_curve_too(conn, cfg, lm):
    """No filter any more. The steam-bath run is kept, and its timings are
    read against the load it faced rather than thrown away."""
    this_morning(conn, cfg)
    add_charging_run(conn, cfg, "2026-08-14", 20, 100, 52.0, 55.0, gen="mep",
                     solo=0, load_w=7000.0)
    curve = lm.charge_curve(None, solo=False, now=ts_at(cfg, "2026-08-20", 12))
    assert curve.runs == 4, "the heavy run is evidence like any other"


def test_a_slow_run_under_a_heavy_load_is_rescaled_not_believed(conn, cfg, lm):
    """It took 100 minutes against 7 kW of house. Against 600 W the same
    gross would have done it far quicker, and that is what is quoted."""
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 100, 52.0, 56.0,
                         gen="mep", solo=1, load_w=7000.0, amps=150.0)
    curve = lm.charge_curve("mep", solo=True, now=ts_at(cfg, "2026-08-20", 12))
    heavy, _ = curve.minutes_between(52.0, 56.0, expected_load_w=7000)
    light, _ = curve.minutes_between(52.0, 56.0, expected_load_w=600)
    assert heavy > light, "a lighter house finishes the same run sooner"
    assert round(light) == round(heavy * (150.0 * 53.0) / (150.0 * 53.0 + 6400))


def test_two_loads_too_far_apart_do_not_speak_for_each_other(conn, cfg, lm):
    """A run that put 1.1 kW into the pack against a 5 kW house says almost
    nothing about the same engine against a 100 W one - the rescale would be
    nearly sixfold, which is further than one run can be stretched."""
    for i in range(3):
        add_charging_run(conn, cfg, f"2026-08-{10+i:02d}", 2, 100, 52.0, 56.0,
                         gen="mep", solo=1, load_w=5000.0, amps=20.0)
    curve = lm.charge_curve("mep", solo=True, now=ts_at(cfg, "2026-08-20", 12))
    assert curve.minutes_between(52.0, 56.0, expected_load_w=100) is None
    assert curve.minutes_between(52.0, 56.0, expected_load_w=4500) is not None


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
                                   solo=False,
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
    """A learned pack: 1,000 Wh a night hour, and 2,000 Wh to the volt.

    Five clean overnight discharges walking 56.0 down to 52.0 in half-volt
    steps at a kilowatt an hour, so the Wh-vs-V curve is flat and what it
    should answer is arithmetic. The resting SOC curve is still here because
    the top-up target reads a voltage off it - the deficit does not.
    """
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])   # night, no sun
    scraped(conn, {52.0: (40, 900), 54.0: (60, 900),
                   56.0: (80, 900), 57.0: (90, 900)})
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=1000, day_wh=1000)
    add_discharge_nights(conn, cfg)
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
    # The pack is at 54.0 and the curve is 2,000 Wh a volt above 52.0.
    assert d["available_wh"] == 4000
    assert d["available_source"] == "learned Wh-vs-V, 5 nights"
    assert d["deficit_wh"] == 5000


def test_what_the_pack_holds_no_longer_comes_from_the_shunt(cfg, deficit_pack,
                                                            conn):
    """The Battery Monitor says 60% of a 100 kWh pack, 20 points above the
    40% the curve puts at 52.0 - twenty thousand watt-hours. The house has
    only ever taken four thousand out between those two voltages."""
    now = ts_at(cfg, "2026-08-20", 21)
    d = deficit_pack.overnight_deficit(ts_at(cfg, "2026-08-21", 6), now=now)
    assert d["soc_now_display"] == 60
    assert d["available_wh"] == 4000
    assert d["available_wh"] != round(0.20 * d["capacity_wh"])


def test_the_deficit_names_the_tier_the_curve_came_from(cfg, deficit_pack):
    now = ts_at(cfg, "2026-08-20", 21)
    d = deficit_pack.overnight_deficit(ts_at(cfg, "2026-08-21", 6), now=now)
    assert d["available_tier"] == "last 14 nights"


def test_a_pack_with_room_to_spare_has_a_negative_deficit(cfg, deficit_pack):
    now = ts_at(cfg, "2026-08-20", 23)
    d = deficit_pack.overnight_deficit(ts_at(cfg, "2026-08-21", 2), now=now)
    assert d["deficit_wh"] < 0


def test_the_deficit_says_why_it_cannot_be_computed(conn, cfg, lm, monkeypatch):
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    now = ts_at(cfg, "2026-08-20", 21)
    d = lm.overnight_deficit(ts_at(cfg, "2026-08-21", 6), now=now)
    assert d["deficit_wh"] is None and "no battery sample" in d["reason"]


def test_the_deficit_says_when_the_curve_cannot_answer(conn, cfg, lm,
                                                        monkeypatch):
    """Nights that never went below 54.0 cannot price the volts under it."""
    monkeypatch.setattr(weather, "hourly", lambda *a, **k: [])
    build_load_history(conn, cfg, days=30, start="2026-08-01",
                       night_wh=1000, day_wh=1000)
    add_discharge_nights(conn, cfg, top_v=56.0, bottom_v=54.0)
    base = ts_at(cfg, "2026-08-20", 20)
    for i in range(20):
        add_sample(conn, cfg, base + i * 60, 55.0, 60, -1000, ah=1000)
    d = lm.overnight_deficit(ts_at(cfg, "2026-08-21", 6),
                             now=ts_at(cfg, "2026-08-20", 21))
    assert d["deficit_wh"] is None
    assert "learned Wh-vs-V curve cannot say" in d["reason"]
    assert "no night crossed 52.00-52.25 V" in d["reason"]


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
    t = deficit_pack.topup_target(9000, 15, 54.0, 100000, low=55.0, high=57.0)
    assert t["padded_wh"] == 10350 and t["target_soc"] == 70.3
    assert t["volts"] == 55.5
    assert t["basis"] == "resting curve"


def test_the_target_is_rounded_up_never_down(cfg, deficit_pack):
    """Too much is minutes of run time. Too little is not getting through."""
    t = deficit_pack.topup_target(100, 0, 54.0, 100000, low=52.0, high=57.0)
    assert t["uncapped_volts"] == 54.5, "60.1% is 54.005 V, which rounds up"


def test_the_target_is_clamped_at_both_ends(cfg, deficit_pack):
    assert deficit_pack.topup_target(100, 0, 54.0, 100000,
                                     low=55.0, high=57.0)["volts"] == 55.0
    assert deficit_pack.topup_target(60000, 0, 54.0, 100000,
                                     low=55.0, high=57.0)["volts"] == 57.0


def test_the_target_uses_the_charge_curve_when_there_is_one(conn, cfg,
                                                            deficit_pack):
    """The stop is a terminal voltage read while charging, so the charge-side
    curve is what turns the charge wanted into the voltage to stop at."""
    this_morning(conn, cfg)
    t = deficit_pack.topup_target(9000, 15, 54.0, 100000, low=55.0, high=57.0,
                                  solo=False)
    assert t["basis"].startswith("charging curve")


def test_no_curve_at_all_gives_no_target(conn, cfg, lm):
    assert lm.topup_target(9000, 15, 54.0, 100000, low=55.0, high=57.0) is None


# --- the energy-vs-voltage curve --------------------------------------------
#
# What the pack holds above a voltage, learned from what the house actually
# took out between two voltages overnight. It replaced state of charge times
# capacity, which was one shunt's percentage multiplied by a size derived
# from the same shunt.

@pytest.fixture
def curve_nights(conn, cfg):
    """Five nights walking 56.0 down to 52.0 at a kilowatt an hour."""
    add_discharge_nights(conn, cfg)
    return loadmodel.LoadModel(conn, cfg)


def when(cfg):
    return ts_at(cfg, "2026-08-20", 21)


def test_the_curve_is_watt_hours_to_the_volt(cfg, curve_nights):
    """Four volts and eight thousand watt-hours: two thousand to the volt."""
    r = curve_nights.energy_above(52.0, 56.0, now=when(cfg))
    assert r["wh"] == 8000 and r["nights"] == 5


def test_it_answers_between_any_two_voltages(cfg, curve_nights):
    now = when(cfg)
    assert curve_nights.energy_above(52.0, 53.0, now=now)["wh"] == 2000
    assert curve_nights.energy_above(52.0, 54.0, now=now)["wh"] == 4000
    assert curve_nights.energy_above(54.0, 56.0, now=now)["wh"] == 4000


def test_a_voltage_at_or_below_the_floor_holds_nothing(cfg, curve_nights):
    r = curve_nights.energy_above(52.0, 52.0, now=when(cfg))
    assert r["wh"] == 0


def test_a_stretch_no_night_crossed_is_a_gap_not_a_guess(conn, cfg):
    """Nights that stopped at 54.0 cannot price the volts underneath."""
    add_discharge_nights(conn, cfg, top_v=56.0, bottom_v=54.0)
    lm = loadmodel.LoadModel(conn, cfg)
    r = lm.energy_above(52.0, 55.0, now=when(cfg))
    assert r["wh"] is None
    assert "no night crossed 52.00-52.25 V" in r["reason"]
    # What it does cover, it answers.
    assert lm.energy_above(54.0, 56.0, now=when(cfg))["wh"] == 8000


def test_with_no_nights_at_all_it_points_at_the_backfill(conn, cfg):
    lm = loadmodel.LoadModel(conn, cfg)
    r = lm.energy_above(52.0, 55.0, now=when(cfg))
    assert r["wh"] is None and "--backfill" in r["reason"]


def test_two_nights_are_not_a_bin(conn, cfg):
    add_discharge_nights(conn, cfg, n=2)
    lm = loadmodel.LoadModel(conn, cfg)
    assert lm.energy_above(52.0, 56.0, now=when(cfg))["wh"] is None


def test_a_generator_hour_is_not_a_discharge(conn, cfg):
    """AC output is not house load while a generator is feeding the
    inverters, and the pack is being filled rather than emptied."""
    add_discharge_nights(conn, cfg)
    lm = loadmodel.LoadModel(conn, cfg)
    assert lm.energy_above(52.0, 56.0, now=when(cfg))["wh"] == 8000
    # The same nights, with the generator recorded as having produced.
    for i in range(5):
        night = (datetime(2026, 8, 10) + timedelta(days=i)).strftime("%Y-%m-%d")
        for h in (20, 21, 22, 23):
            history.put_hourly(conn, ts_at(cfg, night, h), "gen",
                               None, None, 5000, None, None, None, 60, "live")
    conn.commit()
    lm = loadmodel.LoadModel(conn, cfg)
    r = lm.energy_above(52.0, 56.0, now=when(cfg))
    assert r["wh"] is None, "the generator hours must not be in the curve"


def test_an_hour_with_sun_in_it_is_not_a_discharge(conn, cfg):
    add_discharge_nights(conn, cfg)
    for i in range(5):
        night = (datetime(2026, 8, 10) + timedelta(days=i)).strftime("%Y-%m-%d")
        for h in (20, 21, 22, 23):
            history.put_hourly(conn, ts_at(cfg, night, h), "solar",
                               None, None, 800, None, None, None, 60, "live")
    conn.commit()
    lm = loadmodel.LoadModel(conn, cfg)
    assert lm.energy_above(52.0, 56.0, now=when(cfg))["wh"] is None


def test_a_daylight_hour_is_not_part_of_a_night(conn, cfg):
    """Sunset to sunrise, computed for the site, and nothing outside it."""
    add_discharge_nights(conn, cfg, hours=(9, 10, 11, 12))
    lm = loadmodel.LoadModel(conn, cfg)
    assert lm._discharge_nights(when(cfg)) == {}


def test_an_hour_the_pack_rose_through_is_not_a_discharge(conn, cfg):
    for i in range(5):
        night = (datetime(2026, 8, 10) + timedelta(days=i)).strftime("%Y-%m-%d")
        for h in (20, 21, 22, 23):
            ts = ts_at(cfg, night, h)
            # max below min: nothing fell.
            history.put_hourly(conn, ts, "battery", 54.0, None, 0, 1000,
                               54.0, 54.0, 60, "live")
            history.put_hourly(conn, ts, "load", None, None, None, 1000,
                               None, None, 60, "live")
    conn.commit()
    lm = loadmodel.LoadModel(conn, cfg)
    assert lm._discharge_nights(when(cfg)) == {}


def test_hours_after_midnight_belong_to_the_night_that_began_yesterday(conn, cfg):
    add_discharge_nights(conn, cfg, n=3)
    lm = loadmodel.LoadModel(conn, cfg)
    nights = lm._discharge_nights(when(cfg))
    assert len(nights) == 3, "eight hours either side of midnight is one night"


def test_the_recent_tier_wins_over_older_evidence(conn, cfg):
    """The same tiers the overnight profile walks. A household changes, and
    a June that drew twice as much should not still be arguing with this
    fortnight."""
    add_discharge_nights(conn, cfg, first="2026-06-01", n=10, wh_per_hour=2000)
    add_discharge_nights(conn, cfg, first="2026-08-10", n=5, wh_per_hour=1000)
    lm = loadmodel.LoadModel(conn, cfg)
    r = lm.energy_above(52.0, 56.0, now=when(cfg))
    assert r["source"] == "last 14 nights" and r["wh"] == 8000


def test_a_tier_that_cannot_answer_gives_way_to_one_that_can(conn, cfg):
    """This fortnight never went below 54.0; the older nights did."""
    add_discharge_nights(conn, cfg, first="2026-06-01", n=10, wh_per_hour=2000)
    add_discharge_nights(conn, cfg, first="2026-08-10", n=5,
                         top_v=56.0, bottom_v=54.0)
    lm = loadmodel.LoadModel(conn, cfg)
    r = lm.energy_above(52.0, 55.0, now=when(cfg))
    assert r["source"] == "all history"
    assert r["wh"] == 12000, "three volts at 4,000 Wh each"


# --- the shunt against the curve --------------------------------------------

def test_the_shunt_claiming_more_than_the_curve_is_measurable(cfg, deficit_pack):
    """60% of the 90 kWh the shunt's own Ah imply is 20 points above the 40%
    the resting curve puts at 52.0: 18,000 Wh. The house has taken 4,000 out
    between those two voltages."""
    d = deficit_pack.soc_disagreement(60, 54.0, now=when(cfg))
    assert d["implied_wh"] == 18000 and d["learned_wh"] == 4000
    assert round(d["excess"], 2) == 3.5


def test_a_shunt_that_agrees_shows_no_excess(cfg, deficit_pack):
    """44.44% is 4.44 points above the floor, which on a 90 kWh pack is
    4,000 Wh - exactly what the curve says."""
    d = deficit_pack.soc_disagreement(44.44, 54.0, now=when(cfg))
    assert abs(d["excess"]) < 0.01


def test_no_disagreement_at_or_below_the_floor(cfg, deficit_pack):
    assert deficit_pack.soc_disagreement(40, 52.0, now=when(cfg)) is None


def test_no_disagreement_without_a_curve_to_disagree_with(conn, cfg, lm):
    assert lm.soc_disagreement(80, 55.0, now=when(cfg)) is None
