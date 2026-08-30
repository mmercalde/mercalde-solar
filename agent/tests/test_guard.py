"""Every guard rule from SPEC section 7, in both directions."""

import json
import os
from datetime import datetime, timedelta

import pytest

import config as cfgmod
import guard as guardmod
import history
import policy
import sun
from stubs import StubModel

# Captured before the autouse fixture below replaces it, so one test can put
# the real computation back.
REAL_SUN_TIMES = sun.times


def ts_at(cfg, day, hour, minute=0):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=tz).timestamp())


NOW_DAY, NOW_HOUR = "2026-08-20", 22


@pytest.fixture
def now(cfg):
    return ts_at(cfg, NOW_DAY, NOW_HOUR)


def make_status(cfg, now, voltage=54.0, soc=80, mep_running=False,
                kub_running=False, monitor_online=True, clock_skew=0,
                mep_start=52.0, mep_stop=54.5, kub_start=52.0, kub_stop=54.5,
                max_runtime=120):
    clock = history.local(now - clock_skew, cfg).strftime("%H:%M:%S")
    return {
        "data": {
            "batteryVoltage": voltage, "battSocBM": soc,
            "battMonitorOnline": monitor_online, "clockTime": clock,
            "lastUpdate": "12:34:56",   # uptime, deliberately not a timestamp
            "mep803aAction": history.GEN_RUNNING if mep_running else history.GEN_STOPPED,
            "kubotaAction": history.GEN_RUNNING if kub_running else history.GEN_STOPPED,
        },
        "config": {
            "mep803a": {"startVoltage": mep_start, "stopVoltage": mep_stop,
                        "maxRuntime": max_runtime, "chargeRate": 100, "cooldown": 5},
            "kubota": {"startVoltage": kub_start, "stopVoltage": kub_stop,
                       "maxRuntime": max_runtime, "chargeRate": 70, "cooldown": 5},
        },
    }


def open_the_gate(conn, cfg, now):
    """Satisfy rule 6: prior-year August history plus 8 consecutive live days."""
    for i in range(10):
        history.put_hourly(conn, ts_at(cfg, f"2025-08-{i+1:02d}", 12), "load",
                           None, None, None, 500, None, None, 60, "insightlocal")
    for d in range(1, 9):
        history.record_sample(conn, {
            "batteryVoltage": 54.0, "battSocBM": 80, "battPower": -1000,
            "battCurrent": -18.0, "battMonitorOnline": True,
            "mep803aAction": history.GEN_STOPPED,
            "kubotaAction": history.GEN_STOPPED,
            "acPower1": 500, "acPower2": 500,
            "mppt80PVPower": 0, "southArrayPVPower": 0, "westArrayPVPower": 0,
        }, ts=ts_at(cfg, f"2026-08-{d:02d}", 12))


def add_rate(conn, cfg, gen, amps=150.0, solo=1, n=3, kind="auto"):
    # Distinct start times: gen_runs is keyed on (gen, start_ts), and one gen
    # has both solo and paired history.
    hour = 2 + (0 if solo else 6)
    for i in range(n):
        start = ts_at(cfg, f"2026-08-{10+i:02d}", hour)
        conn.execute(
            "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, start_v, "
            "stop_v, rate_v_per_h, rate_a, load_w, solo, kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (gen, start, start + 3600, 60, 52.0, 53.5, 1.5, amps, 600.0,
             solo, kind))
    conn.commit()


def learn_the_pack(conn, cfg):
    """A 2000 Ah pack and the curve that says what a stop voltage costs.

    52.0 V is 60%, 54.0 V is 80%, 57.0 V is 95%, so the three volts from the
    everyday resting point to a full top-up are fifteen points of charge. At
    150 A a 2000 Ah pack gains 7.5 points an hour, which is exactly the two
    hours the Pi5 allows a run.
    """
    counts = {(history.soc_bin(v), soc): 900 for v, soc in
              ((52.0, 60), (54.0, 80), (55.0, 85), (56.0, 90), (57.0, 95))}
    history.record_soc_observations(conn, "2025-08-01", counts)
    base = ts_at(cfg, "2026-08-19", 2)
    for i in range(20):
        history.record_sample(conn, {
            "batteryVoltage": 54.0, "battSocBM": 50, "battPower": -1200,
            "battCurrent": -22.0, "battAhRemaining": 1000,
            "battMonitorOnline": True,
            "mep803aAction": history.GEN_STOPPED,
            "kubotaAction": history.GEN_STOPPED,
        }, ts=base + i * 60)


@pytest.fixture(autouse=True)
def sun(monkeypatch, cfg):
    """Sun times pinned to the hours the daylight tests talk about."""
    def times(_cfg, day=None, now=None):
        day = day or NOW_DAY
        return ts_at(cfg, day, 6, 22), ts_at(cfg, day, 19, 24)
    monkeypatch.setattr(guardmod.sunmod, "times", times)
    return times


@pytest.fixture
def g(conn, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", str(tmp_path / "audit.log"))
    return guardmod.Guard(conn, cfg, state_path=str(tmp_path / "guard_state.json"))


@pytest.fixture
def ready(conn, cfg, g, now):
    """A guard whose gate is open, with a learned pack and charge rates."""
    open_the_gate(conn, cfg, now)
    learn_the_pack(conn, cfg)
    add_rate(conn, cfg, "mep", 150.0, solo=1)
    add_rate(conn, cfg, "mep", 150.0, solo=0)
    add_rate(conn, cfg, "kubota", 100.0, solo=1)
    add_rate(conn, cfg, "kubota", 100.0, solo=0)
    return g


# --- a permitted write ------------------------------------------------------

def test_a_sound_write_is_permitted(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "cloudy tomorrow",
                          now=now, status=make_status(cfg, now))
    assert ok, why


# --- the hard limits --------------------------------------------------------
#
# Absolute: not from config, not relaxable by a rule, not by the owner.

@pytest.mark.parametrize("start", [51.9, 51.0, 40.0, 0.0, -5.0])
def test_a_start_below_the_floor_is_refused(ready, cfg, now, start):
    ok, why = ready.check(start, 56.0, 52.0, 56.0, "x", now=now,
                          status=make_status(cfg, now))
    assert not ok and why.startswith("floor 52.0")
    assert "mep start" in why and "absolute" in why


@pytest.mark.parametrize("stop", [57.1, 58.0, 60.0, 99.0])
def test_a_stop_above_the_ceiling_is_refused(ready, cfg, now, stop):
    ok, why = ready.check(52.0, stop, 52.0, 56.0, "x", now=now,
                          status=make_status(cfg, now))
    assert not ok and why.startswith("ceiling 57.0")
    assert "mep stop" in why and "absolute" in why


def test_the_kubota_is_held_to_the_same_limits(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 51.5, 56.0, "x", now=now,
                          status=make_status(cfg, now))
    assert not ok and why.startswith("floor 52.0") and "kubota start" in why
    ok, why = ready.check(52.0, 56.0, 52.0, 57.5, "x", now=now,
                          status=make_status(cfg, now))
    assert not ok and why.startswith("ceiling 57.0") and "kubota stop" in why


def test_the_limits_themselves_are_permitted(ready, cfg, now):
    """The floor and the ceiling are inside, not outside."""
    ok, why = ready.check(guardmod.HARD_START_FLOOR, guardmod.HARD_STOP_CEILING,
                          guardmod.HARD_START_FLOOR, guardmod.HARD_STOP_CEILING,
                          "the edges", now=now, status=make_status(cfg, now))
    assert ok, why


def test_a_firing_policy_rule_cannot_widen_them(ready, cfg, now):
    ok, why = ready.check(51.5, 57.5, 52.0, 56.0, "POLICY 4 solo top-up",
                          now=now, status=make_status(cfg, now),
                          policy=a_firing_rule(cfg, now))
    assert not ok and why.startswith("floor 52.0")


def test_an_owner_baseline_cannot_widen_them(after_owner_edit, cfg, now):
    """Even returning to the owner's own values, if they are outside."""
    after_owner_edit.state["owner_baseline"] = {
        "mep_start": 51.0, "mep_stop": 58.0,
        "kub_start": 51.0, "kub_stop": 58.0}
    ok, why = after_owner_edit.check(51.0, 58.0, 51.0, 58.0, "the owner's own",
                                     now=now, status=owner_status(cfg, now))
    assert not ok and why.startswith("floor 52.0")


def test_a_loosened_config_cannot_widen_them(conn, cfg, tmp_path, monkeypatch,
                                             now):
    """config.json is data. These two numbers are not."""
    monkeypatch.setattr(cfgmod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", str(tmp_path / "audit.log"))
    loose = dict(cfg, start_voltage_min=40.0, stop_voltage_max=60.0)
    g = guardmod.Guard(conn, loose, state_path=str(tmp_path / "s.json"))
    open_the_gate(conn, loose, now)
    learn_the_pack(conn, loose)
    add_rate(conn, loose, "mep", 150.0, solo=1)
    ok, why = g.check(50.0, 59.0, 52.0, 56.0, "x", now=now,
                      status=make_status(loose, now))
    assert not ok and why.startswith("floor 52.0")


def test_the_limits_are_checked_before_anything_else(g, cfg, now):
    """No learning gate, no dashboard, no rules - and still refused, with the
    limit as the reason rather than whatever would have refused it anyway."""
    ok, why = g.check(51.0, 58.0, 51.0, 58.0, "x", now=now,
                      status=make_status(cfg, now))
    assert not ok and why.startswith("floor 52.0")
    assert "learning phase" not in why


def test_hard_limits_needs_no_state_at_all():
    ok, why = guardmod.Guard.hard_limits(
        {"mep_start": 52.0, "mep_stop": 57.0,
         "kub_start": 52.0, "kub_stop": 57.0})
    assert ok and why == "within the hard limits"


def test_a_refused_limit_is_audited(ready, conn, cfg, now, tmp_path):
    ready.check(51.0, 56.0, 52.0, 56.0, "x", now=now, status=make_status(cfg, now))
    row = conn.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason"].startswith("floor 52.0")
    assert "floor 52.0" in (tmp_path / "audit.log").read_text()


def test_the_heartbeat_will_not_re_send_values_outside_the_limits(ready, now):
    """An owner edit to 51.0 in the dashboard is adopted as observed, and
    never written back by the hourly re-send."""
    ready.state["intended"] = {"mep_start": 51.0, "mep_stop": 56.0,
                               "kub_start": 51.0, "kub_stop": 56.0}
    send, values, why = ready.heartbeat(now=now)
    assert not send and values is None
    assert why == "heartbeat withheld: floor 52.0: mep start 51.0 is below it, " \
                  "and the floor is absolute"


def test_the_heartbeat_sends_values_inside_the_limits(ready, now):
    ready.state["intended"] = {"mep_start": 52.0, "mep_stop": 57.0,
                               "kub_start": 52.0, "kub_stop": 57.0}
    send, values, _ = ready.heartbeat(now=now)
    assert send and values["mep_stop"] == 57.0


# --- rule 1: bounds ---------------------------------------------------------

# Config may narrow the permitted range but never widen it, so rule 1 only
# ever fires strictly inside the hard limits: a start under 52.0 or a stop
# over 57.0 is refused by the floor and the ceiling before rule 1 is reached.

def test_start_below_the_configured_minimum_is_refused(conn, cfg, tmp_path,
                                                       monkeypatch, now):
    monkeypatch.setattr(cfgmod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", str(tmp_path / "audit.log"))
    tight = dict(cfg, start_voltage_min=53.0)
    g = guardmod.Guard(conn, tight, state_path=str(tmp_path / "s.json"))
    open_the_gate(conn, tight, now)
    learn_the_pack(conn, tight)
    ok, why = g.check(52.5, 54.5, 53.0, 54.5, "x",
                      now=now, status=make_status(tight, now))
    assert not ok and "outside the permitted" in why and "mep start" in why


def test_start_above_the_ceiling_is_refused(ready, cfg, now):
    ok, why = ready.check(52.0, 54.5, 56.5, 57.0, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "kubota start" in why


def test_stop_below_the_configured_minimum_is_refused(ready, cfg, now):
    ok, why = ready.check(52.0, 54.0, 52.0, 54.5, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "mep stop" in why and "outside the permitted" in why


def test_stop_must_clear_start_by_two_volts(ready, cfg, now):
    ok, why = ready.check(53.0, 54.5, 52.0, 54.5, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "at least 2.0 V above" in why


def test_exactly_two_volts_apart_is_accepted(ready, cfg, now):
    ok, why = ready.check(52.5, 54.5, 52.0, 54.5, "x",
                          now=now, status=make_status(cfg, now))
    assert ok, why


def test_the_bounds_are_inclusive(ready, cfg, now):
    ok, why = ready.check(cfg["start_voltage_min"], cfg["stop_voltage_max"],
                          cfg["start_voltage_min"], cfg["stop_voltage_max"],
                          "edges", now=now, status=make_status(cfg, now))
    assert ok, why


# --- rule 2: no-op ----------------------------------------------------------

def test_writing_the_live_values_is_refused(ready, cfg, now):
    ok, why = ready.check(52.0, 54.5, 52.0, 54.5, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "already the live thresholds" in why


def test_changing_one_value_is_not_a_no_op(ready, cfg, now):
    ok, why = ready.check(52.0, 54.5, 52.0, 55.0, "x",
                          now=now, status=make_status(cfg, now))
    assert ok, why


# --- rule 3: running generator ---------------------------------------------

def test_lowering_a_running_generators_stop_is_refused(ready, cfg, now):
    st = make_status(cfg, now, mep_running=True, mep_stop=56.0)
    ok, why = ready.check(52.0, 55.0, 52.0, 54.5, "x", now=now, status=st)
    assert not ok and "cannot be lowered" in why and "mep is running" in why


def test_raising_a_running_generators_stop_is_allowed(ready, cfg, now):
    st = make_status(cfg, now, mep_running=True, mep_stop=54.5)
    ok, why = ready.check(52.0, 56.0, 52.0, 54.5, "x", now=now, status=st)
    assert ok, why


def test_a_stopped_generators_stop_may_be_lowered(ready, cfg, now):
    st = make_status(cfg, now, mep_running=False, mep_stop=56.0)
    ok, why = ready.check(52.0, 54.5, 52.0, 54.5, "x", now=now, status=st)
    assert ok, why


def test_a_running_generators_start_is_irrelevant(ready, cfg, now):
    """Mid-run the start threshold does nothing, so changing it is fine."""
    st = make_status(cfg, now, mep_running=True, mep_stop=54.5, voltage=53.0)
    ok, why = ready.check(52.5, 56.0, 52.0, 54.5, "x", now=now, status=st)
    assert ok, why


# --- rule 4: reachability ---------------------------------------------------

def test_an_unreachable_target_is_refused(ready, cfg, now):
    """The Kubota puts 100 A into a 2000 Ah pack, which is 5 points an hour.
    60% to 95% is thirty-five of them: seven hours, not two."""
    st = make_status(cfg, now, voltage=52.0, soc=60)
    ok, why = ready.check(52.0, 54.5, 55.0, 57.0, "solo top-up", now=now, status=st)
    assert not ok and "kubota cannot lift the pack from 52.0 V to 57.0 V" in why
    assert "57.0 needs 7.0 h" in why and "run window is 2.0 h" in why
    assert "100 A into the pack (5.0% SOC/h)" in why


def test_a_reachable_target_is_permitted(ready, cfg, now):
    """The MEP's 150 A is 7.5 points an hour; 80% to 95% is exactly two."""
    st = make_status(cfg, now, voltage=54.0, soc=80)
    ok, why = ready.check(55.0, 57.0, 52.0, 54.5, "solo top-up", now=now, status=st)
    assert ok, why


def test_volts_per_hour_is_not_what_reachability_is_judged_on(ready, conn, cfg,
                                                              now):
    """The 20:09 MEP run: 150 A into the pack, but the terminal voltage barely
    moved under a 7 kW load. The rate is the current, not the voltage."""
    conn.execute("UPDATE gen_runs SET rate_v_per_h=0.864 WHERE gen='mep'")
    conn.commit()
    st = make_status(cfg, now, voltage=54.0, soc=80)
    ok, why = ready.check(55.0, 57.0, 52.0, 54.5, "solo top-up", now=now, status=st)
    assert ok, why


def test_a_run_taken_under_an_exceptional_load_is_not_a_rate(ready, conn, cfg,
                                                             now):
    """With every MEP run measured through a steam bath there is no MEP rate
    left, and an unproven target is refused rather than guessed at."""
    for d in range(1, 9):
        history.put_hourly(conn, ts_at(cfg, f"2026-08-{d:02d}", 12), "load",
                           None, None, None, 600, None, None, 60, "live")
    for h in range(24):
        for d in range(1, 15):
            history.put_hourly(conn, ts_at(cfg, f"2026-08-{d:02d}", h), "load",
                               None, None, None, 600, None, None, 60, "live")
    conn.execute("UPDATE gen_runs SET load_w=7000 WHERE gen='mep'")
    conn.commit()
    st = make_status(cfg, now, voltage=54.0, soc=80)
    ok, why = ready.check(55.0, 57.0, 52.0, 54.5, "solo top-up", now=now, status=st)
    assert not ok and "no observed charge rate for mep" in why


def test_reachability_only_applies_to_generators_that_will_fire(ready, cfg, now):
    """Both starts below current voltage: neither fires, so nothing to check."""
    st = make_status(cfg, now, voltage=56.0)
    ok, why = ready.check(52.0, 57.0, 52.0, 57.0, "x", now=now, status=st)
    assert ok, why


def test_no_observed_rate_refuses_and_points_at_the_defaults(conn, cfg, g, now):
    open_the_gate(conn, cfg, now)      # gate open, but no runs recorded
    learn_the_pack(conn, cfg)
    st = make_status(cfg, now, voltage=52.5, soc=65)
    ok, why = g.check(55.0, 57.0, 52.0, 54.5, "solo top-up", now=now, status=st)
    assert not ok
    assert "no observed charge rate" in why
    # The message must quote the configured defaults, whatever they are.
    assert str(cfg["default_start"]) in why and str(cfg["default_stop"]) in why


def test_the_ags_cap_binds_when_it_is_tighter_than_the_pi5(ready, cfg, now):
    """A 10 h Pi5 maxRuntime still cannot beat the 3 h AGS limit."""
    st = make_status(cfg, now, voltage=52.0, soc=60, max_runtime=600)
    ok, why = ready.check(52.0, 54.5, 55.0, 57.0, "x", now=now, status=st)
    assert not ok and "7.0 h" in why and "run window is 3.0 h" in why
    assert "resting curve" in why, "and it says which curve said so"


def test_solo_and_paired_rates_are_chosen_correctly(conn, cfg, g, now):
    """Only the solo rate exists; a paired firing must still find a rate."""
    open_the_gate(conn, cfg, now)
    learn_the_pack(conn, cfg)
    add_rate(conn, cfg, "mep", 300.0, solo=1)
    add_rate(conn, cfg, "kubota", 300.0, solo=1)
    st = make_status(cfg, now, voltage=54.0, soc=80)
    ok, why = g.check(55.0, 57.0, 55.0, 57.0, "both fire", now=now, status=st)
    assert ok, why


# --- rule 5: rate limit -----------------------------------------------------

def test_a_second_write_within_the_hour_is_refused(ready, cfg, now):
    ready.note_write({"mep_start": 52.0, "mep_stop": 54.5,
                      "kub_start": 52.0, "kub_stop": 54.5}, now=now - 1800)
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "30 minutes ago" in why


def test_a_write_after_the_hour_is_permitted(ready, cfg, now):
    ready.note_write({"mep_start": 52.0, "mep_stop": 54.5,
                      "kub_start": 52.0, "kub_stop": 54.5}, now=now - 3700)
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x",
                          now=now, status=make_status(cfg, now))
    assert ok, why


def test_the_first_write_is_not_rate_limited(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x",
                          now=now, status=make_status(cfg, now))
    assert ok, why


# --- rule 6: learning gate --------------------------------------------------

def test_no_history_at_all_refuses(g, cfg, now):
    ok, why = g.check(52.0, 56.0, 52.0, 56.0, "x",
                      now=now, status=make_status(cfg, now))
    assert not ok and why.startswith("learning phase")
    assert "prior year" in why and "consecutive days" in why


def test_prior_year_history_alone_is_not_enough(conn, cfg, g, now):
    for i in range(10):
        history.put_hourly(conn, ts_at(cfg, f"2025-08-{i+1:02d}", 12), "load",
                           None, None, None, 500, None, None, 60, "insightlocal")
    ok, why = g.check(52.0, 56.0, 52.0, 56.0, "x",
                      now=now, status=make_status(cfg, now))
    assert not ok and "consecutive days" in why and "prior year" not in why


def test_live_days_alone_are_not_enough(conn, cfg, g, now):
    for d in range(1, 9):
        history.record_sample(conn, {
            "batteryVoltage": 54.0, "battSocBM": 80, "battMonitorOnline": True,
            "mep803aAction": history.GEN_STOPPED, "kubotaAction": history.GEN_STOPPED,
        }, ts=ts_at(cfg, f"2026-08-{d:02d}", 12))
    ok, why = g.check(52.0, 56.0, 52.0, 56.0, "x",
                      now=now, status=make_status(cfg, now))
    assert not ok and "prior year" in why and "consecutive days" not in why


# --- rule 7: stale data -----------------------------------------------------

def test_stale_dashboard_data_refuses(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x", now=now,
                          status=make_status(cfg, now, clock_skew=600))
    assert not ok and "old" in why


def test_data_just_inside_the_window_is_accepted(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x", now=now,
                          status=make_status(cfg, now, clock_skew=290))
    assert ok, why


def test_offline_battery_monitor_refuses(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x", now=now,
                          status=make_status(cfg, now, monitor_online=False))
    assert not ok and "battery monitor is offline" in why


def test_uptime_is_not_mistaken_for_a_timestamp(ready, cfg, now):
    """lastUpdate is uptime (pi5/app.py:868); it must not drive staleness."""
    st = make_status(cfg, now)
    st["data"]["lastUpdate"] = "99:99:99"
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x", now=now, status=st)
    assert ok, why


def test_a_poll_just_before_midnight_is_not_seen_as_stale(ready, cfg):
    """clockTime carries no date, so 23:59 read at 00:01 must not look 24 h old."""
    midnight = ts_at(cfg, "2026-08-21", 0, 1)
    st = make_status(cfg, midnight, clock_skew=120)
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x", now=midnight, status=st)
    assert ok, why


def test_missing_clocktime_refuses(ready, cfg, now):
    st = make_status(cfg, now)
    st["data"]["clockTime"] = "--:--:--"
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x", now=now, status=st)
    assert not ok and "clockTime" in why


# --- rule 8: owner override -------------------------------------------------

def test_an_owner_edit_is_adopted_and_stands_the_agent_down(ready, cfg, now):
    ready.note_write({"mep_start": 52.0, "mep_stop": 56.0,
                      "kub_start": 52.0, "kub_stop": 56.0}, now=now - 7200)
    st = make_status(cfg, now, mep_start=53.0, mep_stop=55.0)   # owner's values
    ok, why = ready.check(52.0, 57.0, 52.0, 57.0, "x", now=now, status=st)
    assert not ok and "owner changed the thresholds" in why
    assert ready.intended()["mep_start"] == 53.0, "owner's values adopted"
    assert ready.state["override_until"] == now + guardmod.OWNER_OVERRIDE_SECONDS


def test_the_stand_down_lasts_six_hours(ready, cfg, now):
    ready.state["override_until"] = now + 3600
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "standing down" in why and "60 minutes left" in why


def test_writes_resume_once_the_stand_down_expires(ready, cfg, now):
    ready.state["override_until"] = now - 1
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x",
                          now=now, status=make_status(cfg, now))
    assert ok, why


def test_the_agents_own_write_is_not_an_owner_edit(ready, cfg, now):
    """note_write records what /config reported back, so a Pi5 clamp is not
    later mistaken for the owner editing by hand."""
    ready.note_write({"mep_start": 52.0, "mep_stop": 56.0,
                      "kub_start": 52.0, "kub_stop": 56.0}, now=now - 7200)
    st = make_status(cfg, now, mep_stop=56.0, kub_stop=56.0)
    ok, why = ready.check(52.0, 57.0, 52.0, 57.0, "x", now=now, status=st)
    assert ok, why


# --- rule 8: the owner's values are the baseline ----------------------------

OWNER = {"mep_start": 52.0, "mep_stop": 55.0,
         "kub_start": 52.0, "kub_stop": 55.0}


@pytest.fixture
def after_owner_edit(ready, now):
    """The owner set 55.0 stops by hand; the 6 h stand-down has just expired."""
    ready.state.update(intended=dict(OWNER), owner_baseline=dict(OWNER),
                       override_until=now - 60, last_write_ts=now - 8 * 3600)
    return ready


def owner_status(cfg, now):
    return make_status(cfg, now, mep_stop=55.0, kub_stop=55.0)


def a_firing_rule(cfg, now):
    """A real evaluation in which POLICY 4 fires and nothing else does."""
    return policy.evaluate(cfg, {
        "voltage": 54.0, "soc": 80.0, "peak_today": 55.0,
        "sunrise_ts": now + 8 * 3600,
        "projection": {"reached": now + 4 * 3600}, "tomorrow_cloud": 20,
        "thresholds": dict(OWNER),
        "run_window_h": {"mep": 2.0, "kubota": 2.0}}, StubModel())


def test_an_owner_edit_is_remembered_as_the_baseline(ready, cfg, now):
    ready.note_write({"mep_start": 52.0, "mep_stop": 56.0,
                      "kub_start": 52.0, "kub_stop": 56.0}, now=now - 7200)
    ready.check(52.0, 57.0, 52.0, 57.0, "x", now=now,
                status=make_status(cfg, now, mep_stop=55.0, kub_stop=55.0))
    assert ready.owner_baseline() == OWNER
    assert ready.baseline() == OWNER, "not the config defaults"


def test_no_owner_edit_means_the_baseline_is_config(ready, cfg):
    assert ready.owner_baseline() is None
    assert ready.baseline() == {"mep_start": cfg["default_start"],
                                "mep_stop": cfg["default_stop"],
                                "kub_start": cfg["default_start"],
                                "kub_stop": cfg["default_stop"]}


def test_the_stand_down_expiring_does_not_repeal_the_owners_change(after_owner_edit,
                                                                   cfg, now):
    """04:10 on the first live night, exactly: the stand-down ended and the
    agent wrote the owner's 55.0 stops back to 56.0 with no rule firing."""
    ok, why = after_owner_edit.check(52.0, 56.0, 52.0, 56.0, "returned to default",
                                     now=now, status=owner_status(cfg, now))
    assert not ok
    assert "not a reason on its own" in why
    assert "MEP 52.0/55.0" in why, "it must say what the values to return to are"


def test_moving_off_the_baseline_needs_a_firing_rule(after_owner_edit, cfg, now):
    ok, why = after_owner_edit.check(52.0, 56.0, 52.0, 56.0, "seems better",
                                     now=now, status=owner_status(cfg, now))
    assert not ok
    assert "the owner set MEP 52.0/55.0, Kubota 52.0/55.0 by hand" in why
    assert "none fires" in why


def test_a_firing_rule_may_move_off_the_baseline(after_owner_edit, cfg, now):
    ok, why = after_owner_edit.check(
        55.0, 57.0, 52.0, 56.0, "POLICY 4 solo top-up", now=now,
        status=owner_status(cfg, now), policy=a_firing_rule(cfg, now))
    assert ok, why


def test_the_return_is_to_the_owners_values_not_the_config_defaults(after_owner_edit,
                                                                    cfg, now):
    """The rule-driven change is over; POLICY 6's "default" is the owner's."""
    st = make_status(cfg, now, mep_start=55.0, mep_stop=57.0, kub_stop=56.0)
    after_owner_edit.state["intended"] = {"mep_start": 55.0, "mep_stop": 57.0,
                                          "kub_start": 52.0, "kub_stop": 56.0}
    ok, why = after_owner_edit.check(52.0, 55.0, 52.0, 55.0,
                                     "the top-up is done, back to default",
                                     now=now, status=st)
    assert ok, why


def test_restore_default_is_never_a_reason_on_its_own(ready, cfg, now):
    """With no owner edit at all, it still may not name a destination as a
    cause when that destination is not the values to return to."""
    ok, why = ready.check(52.0, 54.5, 52.0, 54.5, "restoring the defaults",
                          now=now, status=make_status(cfg, now, mep_stop=56.0,
                                                      kub_stop=56.0))
    assert not ok and "not a reason on its own" in why


def test_a_genuine_return_to_the_config_defaults_is_allowed(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "the storm has passed; "
                          "returning to the defaults", now=now,
                          status=make_status(cfg, now, mep_stop=57.0, kub_stop=57.0))
    assert ok, why


def test_a_firing_rule_outranks_the_restore_default_wording(after_owner_edit, cfg,
                                                            now):
    ok, why = after_owner_edit.check(
        55.0, 57.0, 52.0, 56.0, "POLICY 4 top-up; the other returns to default",
        now=now, status=owner_status(cfg, now), policy=a_firing_rule(cfg, now))
    assert ok, why


def test_the_owner_baseline_survives_a_restart(conn, cfg, ready, now, tmp_path,
                                               monkeypatch):
    monkeypatch.undo()       # let this guard actually persist its state
    g = guardmod.Guard(conn, cfg, state_path=str(tmp_path / "state2.json"))
    g.state.update(owner_baseline=dict(OWNER))
    g._save_state()
    reborn = guardmod.Guard(conn, cfg, state_path=str(tmp_path / "state2.json"))
    assert reborn.baseline() == OWNER


# --- rule 10: the daylight hold ---------------------------------------------

def midday(cfg):
    return ts_at(cfg, NOW_DAY, 9, 36)


def test_a_start_raise_is_refused_while_the_sun_is_up(ready, cfg):
    """The 9:36 am event: POLICY 4 fired and the MEP started. Whatever rule
    asks, no generator is raised into a producing day."""
    now = midday(cfg)
    ok, why = ready.check(55.0, 57.0, 52.0, 56.0, "solo top-up", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80))
    assert not ok and why.startswith("daylight hold")
    assert "9:36 am" in why and "6:22 am" in why and "7:24 pm" in why
    assert "would raise mep start above the baseline" in why


def test_a_firing_rule_does_not_get_past_the_daylight_hold(ready, cfg):
    """It is not a policy question. The rule may fire and still be refused."""
    now = midday(cfg)
    ok, why = ready.check(55.0, 57.0, 52.0, 56.0, "POLICY 4 solo top-up", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80),
                          policy=a_firing_rule(cfg, now))
    assert not ok and why.startswith("daylight hold")


def test_both_generators_raised_are_both_named(ready, cfg):
    now = midday(cfg)
    ok, why = ready.check(55.0, 57.0, 55.0, 57.0, "both", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80))
    assert not ok and "mep and kubota start above the baseline" in why


def test_a_stop_raise_is_allowed_in_daylight(ready, cfg):
    """Raising a stop starts nothing, so a storm forecast is acted on at once."""
    now = midday(cfg)
    ok, why = ready.check(52.0, 57.0, 52.0, 57.0, "storm tomorrow", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80))
    assert ok, why


def test_lowering_a_start_is_allowed_in_daylight(ready, cfg):
    now = midday(cfg)
    st = make_status(cfg, now, voltage=54.0, soc=80, mep_start=54.0)
    ready.state["intended"] = {"mep_start": 54.0, "mep_stop": 54.5,
                               "kub_start": 52.0, "kub_stop": 54.5}
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "back to default", now=now,
                          status=st)
    assert ok, why


def test_the_same_write_is_permitted_after_sunset(ready, cfg):
    now = ts_at(cfg, NOW_DAY, 19, 25)
    ok, why = ready.check(55.0, 57.0, 52.0, 56.0, "solo top-up", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80))
    assert ok, why


def test_the_same_write_is_permitted_before_sunrise(ready, cfg):
    now = ts_at(cfg, NOW_DAY, 5, 0)
    ok, why = ready.check(55.0, 57.0, 52.0, 56.0, "solo top-up", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80))
    assert ok, why


def test_the_hold_is_measured_against_the_owners_baseline(after_owner_edit, cfg,
                                                          now):
    """The owner set 52.0 starts; a write to those starts raises nothing."""
    day = midday(cfg)
    ok, why = after_owner_edit.check(52.0, 56.0, 52.0, 56.0, "matching the owner",
                                     now=day,
                                     status=owner_status(cfg, day))
    assert "daylight hold" not in (why or "")


def test_the_hold_does_not_depend_on_anything_being_reachable(ready, cfg,
                                                              monkeypatch):
    """The real computation, with every outbound request made to fail. Sun
    times come from the site's coordinates, so the hold still applies."""
    import requests
    monkeypatch.setattr(guardmod.sunmod, "times", REAL_SUN_TIMES)
    for name in ("get", "post"):
        monkeypatch.setattr(requests, name, lambda *a, **k: (_ for _ in ()).throw(
            requests.ConnectionError("no route")))
    now = ts_at(cfg, NOW_DAY, 12)          # local noon, unambiguously daylight
    ok, why = ready.check(55.0, 57.0, 52.0, 56.0, "solo top-up", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80))
    assert not ok and why.startswith("daylight hold")


def test_the_real_computation_puts_midnight_outside_daylight(ready, cfg,
                                                             monkeypatch):
    monkeypatch.setattr(guardmod.sunmod, "times", REAL_SUN_TIMES)
    now = ts_at(cfg, NOW_DAY, 2)
    ok, why = ready.check(55.0, 57.0, 52.0, 56.0, "solo top-up", now=now,
                          status=make_status(cfg, now, voltage=54.0, soc=80))
    assert ok, why


def test_a_daylight_refusal_is_audited(ready, conn, cfg, tmp_path):
    now = midday(cfg)
    ready.check(55.0, 57.0, 52.0, 56.0, "solo top-up", now=now,
                status=make_status(cfg, now, voltage=54.0, soc=80))
    row = conn.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason"].startswith("daylight hold")
    assert "daylight hold" in (tmp_path / "audit.log").read_text()


# --- rule 9: audit ----------------------------------------------------------

def test_a_refusal_is_audited(conn, g, cfg, now, tmp_path):
    g.check(52.0, 54.5, 52.0, 54.5, "bad", now=now, status=make_status(cfg, now))
    row = conn.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["tool"] == "set_gen_thresholds"
    assert row["voltage"] == 54.0 and row["soc"] == 80
    assert json.loads(row["args"])["mep_start"] == 52.0
    assert "learning phase" in row["reason"]


def test_a_permitted_write_is_audited(conn, ready, cfg, now, tmp_path):
    ready.check(52.0, 56.0, 52.0, 56.0, "cloudy", now=now,
                status=make_status(cfg, now))
    row = conn.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["allowed"] == 1 and row["result"] == "allowed"


def test_the_audit_log_file_is_written(ready, cfg, now, tmp_path):
    ready.check(52.0, 56.0, 52.0, 56.0, "cloudy", now=now,
                status=make_status(cfg, now))
    text = (tmp_path / "audit.log").read_text()
    assert "allowed" in text and "V=54.0" in text and "SOC=80" in text


def test_a_policy_miss_reaches_both_the_table_and_the_log(conn, ready, cfg, now,
                                                          tmp_path):
    """A rule fired and nothing was attempted; that is not a refusal, but the
    audit log is where the question "why did it do that" gets answered."""
    rule = {"rule": 4, "name": "solo top-up", "detail": "peak 55.0 < 57.0",
            "proposal": {"mep_start": 55.0, "mep_stop": 57.0,
                         "kub_start": 52.0, "kub_stop": 56.0}}
    assert ready.record_policy_miss([rule], "no change - pack is healthy",
                                    54.2, 71, now=now) == 1
    row = conn.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["tool"] == "policy_miss" and row["allowed"] == 0
    assert row["voltage"] == 54.2 and row["soc"] == 71
    text = (tmp_path / "audit.log").read_text()
    assert "policy_miss" in text and "solo top-up" in text
    assert "no change - pack is healthy" in text


def test_an_unreachable_dashboard_is_refused_and_audited(conn, ready, cfg, now,
                                                         monkeypatch):
    monkeypatch.setattr(history, "fetch_data",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "x", now=now)
    assert not ok and "cannot read the dashboard" in why
    assert conn.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"] == 1


# --- heartbeat (SPEC section 9) --------------------------------------------

def test_the_heartbeat_is_withheld_during_the_learning_phase(g, now):
    send, values, why = g.heartbeat(now=now)
    assert not send and values is None and "learning phase" in why


def test_the_heartbeat_sends_the_defaults_before_any_write(ready, cfg, now):
    send, values, why = ready.heartbeat(now=now)
    assert send
    assert values == {"mep_start": cfg["default_start"], "mep_stop": cfg["default_stop"],
                      "kub_start": cfg["default_start"], "kub_stop": cfg["default_stop"]}


def test_the_heartbeat_sends_the_last_intended_values(ready, cfg, now):
    ready.note_write({"mep_start": 55.0, "mep_stop": 57.0,
                      "kub_start": 52.0, "kub_stop": 54.5}, now=now - 3700)
    send, values, _ = ready.heartbeat(now=now)
    assert send and values["mep_start"] == 55.0


def test_the_heartbeat_ignores_the_no_op_rule(ready, cfg, now):
    """A real write of these values would be refused as a no-op; the
    heartbeat exists precisely to re-send what is already in force."""
    ready.note_write({"mep_start": 52.0, "mep_stop": 54.5,
                      "kub_start": 52.0, "kub_stop": 54.5}, now=now - 3700)
    send, values, _ = ready.heartbeat(now=now)
    assert send and values["mep_start"] == 52.0


def test_the_heartbeat_is_hourly_not_per_tick(ready, cfg, now):
    """At the 15-minute tick this wrote "Config updated" to the Pi5 event log
    96 times a day. The watchdog's window is six hours."""
    ready.note_heartbeat({"mep_start": 52.0, "mep_stop": 56.0,
                          "kub_start": 52.0, "kub_stop": 56.0}, now=now - 900)
    send, values, why = ready.heartbeat(now=now)
    assert not send and values is None
    assert "15 minutes ago" in why and "hourly" in why
    send, _, _ = ready.heartbeat(now=now + 2701)
    assert send, "an hour after the last one it goes out again"


def test_a_real_write_also_defers_the_heartbeat(ready, cfg, now):
    """The thresholds have just been sent; re-sending them adds nothing."""
    ready.note_write({"mep_start": 55.0, "mep_stop": 57.0,
                      "kub_start": 52.0, "kub_stop": 56.0}, now=now - 600)
    send, _, why = ready.heartbeat(now=now)
    assert not send and "10 minutes ago" in why


def test_the_heartbeat_is_not_the_rate_limit_clock(ready, cfg, now):
    """The bug this hid: with the heartbeat setting last_write_ts every tick,
    rule 5 refused every model write for as long as the agent was alive."""
    ready.note_heartbeat({"mep_start": 52.0, "mep_stop": 56.0,
                          "kub_start": 52.0, "kub_stop": 56.0}, now=now - 60)
    assert ready.state["last_write_ts"] == 0
    ok, why = ready.check(52.0, 57.0, 52.0, 57.0, "storm tomorrow",
                          now=now, status=make_status(cfg, now, mep_stop=56.0,
                                                      kub_stop=56.0))
    assert ok, why


def test_a_heartbeat_still_keeps_the_intended_values_current(ready, now):
    """It reads back what /config reported, so a Pi5 clamp is not later
    mistaken for the owner editing by hand."""
    ready.note_heartbeat({"mep_start": 52.0, "mep_stop": 56.0,
                          "kub_start": 52.0, "kub_stop": 56.0}, now=now)
    assert ready.intended()["mep_stop"] == 56.0


def test_the_heartbeat_stops_while_standing_down_for_the_owner(ready, now):
    ready.state["override_until"] = now + 3600
    send, values, why = ready.heartbeat(now=now)
    assert not send and "standing down" in why


# --- state persistence ------------------------------------------------------

def test_intended_thresholds_survive_a_restart(conn, cfg, ready, now, tmp_path):
    ready.note_write({"mep_start": 55.0, "mep_stop": 57.0,
                      "kub_start": 52.0, "kub_stop": 54.5}, now=now)
    reborn = guardmod.Guard(conn, cfg, state_path=str(tmp_path / "guard_state.json"))
    assert reborn.intended()["mep_start"] == 55.0
    assert reborn.state["last_write_ts"] == now
