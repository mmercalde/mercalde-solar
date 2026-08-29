"""Every guard rule from SPEC section 7, in both directions."""

import json
import os
from datetime import datetime, timedelta

import pytest

import config as cfgmod
import guard as guardmod
import history


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


def add_rate(conn, cfg, gen, v_per_h=1.5, solo=1, n=3, kind="auto"):
    # Distinct start times: gen_runs is keyed on (gen, start_ts), and one gen
    # has both solo and paired history.
    hour = 2 + (0 if solo else 6)
    for i in range(n):
        start = ts_at(cfg, f"2026-08-{10+i:02d}", hour)
        conn.execute(
            "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, start_v, "
            "stop_v, rate_v_per_h, rate_a, solo, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (gen, start, start + 3600, 60, 52.0, 52.0 + v_per_h, v_per_h, 90.0,
             solo, kind))
    conn.commit()


@pytest.fixture
def g(conn, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cfgmod, "AUDIT_LOG", str(tmp_path / "audit.log"))
    return guardmod.Guard(conn, cfg, state_path=str(tmp_path / "guard_state.json"))


@pytest.fixture
def ready(conn, cfg, g, now):
    """A guard whose gate is open and which has observed charge rates."""
    open_the_gate(conn, cfg, now)
    add_rate(conn, cfg, "mep", 1.5, solo=1)
    add_rate(conn, cfg, "mep", 1.5, solo=0)
    add_rate(conn, cfg, "kubota", 1.0, solo=1)
    add_rate(conn, cfg, "kubota", 1.0, solo=0)
    return g


# --- a permitted write ------------------------------------------------------

def test_a_sound_write_is_permitted(ready, cfg, now):
    ok, why = ready.check(52.0, 56.0, 52.0, 56.0, "cloudy tomorrow",
                          now=now, status=make_status(cfg, now))
    assert ok, why


# --- rule 1: bounds ---------------------------------------------------------

def test_start_below_the_floor_is_refused(ready, cfg, now):
    ok, why = ready.check(51.0, 54.5, 52.0, 54.5, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "outside the permitted" in why and "mep start" in why


def test_start_above_the_ceiling_is_refused(ready, cfg, now):
    ok, why = ready.check(52.0, 54.5, 56.5, 57.0, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "kubota start" in why


def test_stop_outside_bounds_is_refused(ready, cfg, now):
    ok, why = ready.check(52.0, 58.0, 52.0, 54.5, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "mep stop" in why
    ok, why = ready.check(52.0, 54.0, 52.0, 54.5, "x",
                          now=now, status=make_status(cfg, now))
    assert not ok and "mep stop" in why


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
    """Kubota at 1.0 V/h cannot lift 52.0 -> 57.0 inside a 2 h window."""
    st = make_status(cfg, now, voltage=52.0)
    ok, why = ready.check(52.0, 54.5, 55.0, 57.0, "solo top-up", now=now, status=st)
    assert not ok and "run window" in why and "kubota" in why


def test_a_reachable_target_is_permitted(ready, cfg, now):
    """MEP at 1.5 V/h needs 2.0 h for 54.0 -> 57.0, inside the 2 h Pi5 window."""
    st = make_status(cfg, now, voltage=54.0)
    ok, why = ready.check(55.0, 57.0, 52.0, 54.5, "solo top-up", now=now, status=st)
    assert ok, why


def test_reachability_only_applies_to_generators_that_will_fire(ready, cfg, now):
    """Both starts below current voltage: neither fires, so nothing to check."""
    st = make_status(cfg, now, voltage=56.0)
    ok, why = ready.check(52.0, 57.0, 52.0, 57.0, "x", now=now, status=st)
    assert ok, why


def test_no_observed_rate_refuses_and_points_at_the_defaults(conn, cfg, g, now):
    open_the_gate(conn, cfg, now)      # gate open, but no runs recorded
    st = make_status(cfg, now, voltage=52.5)
    ok, why = g.check(55.0, 57.0, 52.0, 54.5, "solo top-up", now=now, status=st)
    assert not ok
    assert "no observed charge rate" in why
    # The message must quote the configured defaults, whatever they are.
    assert str(cfg["default_start"]) in why and str(cfg["default_stop"]) in why


def test_the_ags_cap_binds_when_it_is_tighter_than_the_pi5(ready, cfg, now):
    """A 10 h Pi5 maxRuntime still cannot beat the 3 h AGS limit."""
    st = make_status(cfg, now, voltage=52.0, max_runtime=600)
    ok, why = ready.check(52.0, 54.5, 55.0, 57.0, "x", now=now, status=st)
    assert not ok and "5.0 h" in why and "3.0 h" in why


def test_solo_and_paired_rates_are_chosen_correctly(conn, cfg, g, now):
    """Only the solo rate exists; a paired firing must still find a rate."""
    open_the_gate(conn, cfg, now)
    add_rate(conn, cfg, "mep", 3.0, solo=1)
    add_rate(conn, cfg, "kubota", 3.0, solo=1)
    st = make_status(cfg, now, voltage=54.0)
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


# --- rule 9: audit ----------------------------------------------------------

def test_a_refusal_is_audited(conn, g, cfg, now, tmp_path):
    g.check(51.0, 54.5, 52.0, 54.5, "bad", now=now, status=make_status(cfg, now))
    row = conn.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["tool"] == "set_gen_thresholds"
    assert row["voltage"] == 54.0 and row["soc"] == 80
    assert json.loads(row["args"])["mep_start"] == 51.0
    assert "learning phase" in row["reason"] or "outside" in row["reason"]


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
                      "kub_start": 52.0, "kub_stop": 54.5}, now=now - 600)
    send, values, _ = ready.heartbeat(now=now)
    assert send and values["mep_start"] == 55.0


def test_the_heartbeat_ignores_the_rate_limit_and_the_no_op_rule(ready, cfg, now):
    """Both would refuse a real write here; the heartbeat still goes out."""
    ready.note_write({"mep_start": 52.0, "mep_stop": 54.5,
                      "kub_start": 52.0, "kub_stop": 54.5}, now=now - 60)
    send, values, _ = ready.heartbeat(now=now)
    assert send and values["mep_start"] == 52.0


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
