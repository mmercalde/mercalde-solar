"""Agent logic that runs without a model or a network: the plan record,
the recommendation contract, answer grounding, and the anomaly triggers."""

import re
import sqlite3
from datetime import datetime, timedelta

import pytest

import agent as agentmod
import history
import tools as toolsmod
import policy
from stubs import StubModel


def base_facts(cfg, gate_open=False, model=None):
    now = int(datetime(2026, 8, 28, 16, 0,
                       tzinfo=history.tzinfo(cfg)).timestamp())
    f = {
        "now": now, "today": "2026-08-28",
        "data": {}, "config": {},
        "voltage": 55.8, "soc": 84, "load_w": 1100, "solar_w": 3000,
        "gen_running": False, "peak_today": 55.8,
        "weather": {}, "sunrise_ts": int(datetime(
            2026, 8, 29, 6, 31, tzinfo=history.tzinfo(cfg)).timestamp()),
        "forecast": {"learned": True, "hours": 12, "total_wh": 10800},
        "projection": {"reached": now + 43800, "at": "4:10 am", "hours": 12.2},
        "drawdown": {"wh": 10800, "month": 8, "nights": 12,
                     "source": "last 14 nights", "series": "battery"},
        "overhead": {"ratio": 1.18, "min": 1.09, "max": 1.34, "nights": 12,
                     "source": "last 14 nights"},
        "gate": {"open": gate_open},
        "soc_curve": {"points": 12, "soc_at_start_threshold": 41.0,
                      "start_threshold_v": 52.0, "volts_low": 51.8,
                      "volts_high": 55.4, "observations": 9000,
                      "scraped_observations": 9000},
        "tomorrow_cloud": 20,
        "est_solar": {"wh": 61000, "clear_day_wh": 68000},
        "summary_24h": {}, "intended": {},
        "thresholds": {"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
        "charge_rates": {"mep": {"v_per_h": 1.6}, "kubota": {"v_per_h": 1.0}},
        "run_window_h": {"mep": 2.0, "kubota": 2.0},
        "charge_rates": {},
        "baseline": {"mep_start": 52.0, "mep_stop": 56.0,
                     "kub_start": 52.0, "kub_stop": 56.0},
        "deficit": {"deficit_wh": 9000, "needed_wh": 32000,
                    "available_wh": 23000, "capacity_wh": 100000,
                    "available_source": "learned Wh-vs-V, 10 nights",
                    "soc_now_display": 63, "floor_v": 52.0,
                    "hours": 8.2, "source": "last 14 nights"},
    }
    f["voltage"] = 54.2
    f["soc"] = 63
    f["policy"] = policy.evaluate(cfg, f, model or StubModel())
    return f


# --- the plan record --------------------------------------------------------

def test_plan_record_matches_the_spec_shape(a, cfg):
    rec = a.plan_record(base_facts(cfg),
                        "Kubota solo, start 56.0 / stop 57.0; MEP 52.0 / 54.5",
                        "no (learning phase)")
    lines = rec.splitlines()
    assert len(lines) == 11
    assert lines[0] == "2026-08-28 4:00 pm  V 54.2  SOC 63%  load 1.1 kW"
    assert lines[1] == "peak today: 55.8 V  (threshold 57.0 -> solar shortfall)"
    assert lines[2] == ("overnight Wh out of the pack: 10,800 — from "
                        "last 14 nights (12 nights)")
    assert lines[3] == ("system overhead: 1.180x (pack out ÷ house in, "
                        "1.090-1.340 over 12 nights, last 14 nights)")
    assert lines[4] == "projected 52.0 V at: 4:10 am   sunrise 6:31 am"
    assert lines[5] == ("forecast tomorrow: 20% cloud, est. solar 61.0 kWh "
                        "(Aug clear-day 68.0)")
    assert lines[6] == ("POLICY 3 storm stop 57.0: no (tomorrow 20% daylight "
                        "cloud < 70%)")
    assert lines[7] == ("POLICY 3 pre-dawn stop 54.5: no (52 V projected 4:10 am, "
                        "2.4 h before sunrise 6:31 am (window 2.0 h))")
    assert lines[8].startswith("POLICY 4 top-up: FIRES (deficit 9,000 Wh to "
                               "sunrise above 52.0 V")
    assert "+15% is 10,350 Wh → stop 55.5" in lines[8]
    assert "raised to 56.4 to clear a start above 54.2 V" in lines[8]
    assert "MEP band (deficit ≤ 15,000 Wh)" in lines[8]
    assert lines[9].startswith("recommend: Kubota solo")
    assert lines[10] == "applied: no (learning phase)"


def test_the_plan_record_shows_a_rule_that_does_not_fire_and_why(a, cfg):
    f = base_facts(cfg)
    f["deficit"] = dict(f["deficit"], deficit_wh=-4000)
    f["policy"] = policy.evaluate(cfg, f, StubModel())
    lines = a.plan_record(f, "no change", "no (dry run)").splitlines()
    assert ("POLICY 4 top-up: no (the pack holds 4,000 Wh more than the night "
            "needs above 52.0 V, so nothing is short)") in lines


def test_peak_at_or_above_threshold_is_not_a_shortfall(a, cfg):
    f = base_facts(cfg)
    f["peak_today"] = 57.2
    assert "reached" in a.plan_record(f, "x", "y").splitlines()[1]


def test_plan_record_says_what_it_has_not_learned(a, cfg):
    f = base_facts(cfg)
    f["drawdown"] = None
    f["overhead"] = None
    f["projection"] = {"reached": None, "reason": "pack capacity not learned"}
    f["est_solar"] = None
    f["policy"] = policy.evaluate(cfg, f, StubModel())
    lines = a.plan_record(f, "no change", "no (learning phase)").splitlines()
    assert lines[2] == "overnight Wh out of the pack: not learned yet"
    assert lines[3] == "system overhead: not learned yet"
    assert "not projected (pack capacity not learned)" in lines[4]
    assert lines[5].endswith("not learned yet")


def test_a_projection_already_at_the_target_reads_now_not_a_question_mark(a, cfg):
    f = base_facts(cfg)
    f["projection"] = {"reached": f["now"], "at": "now", "hours": 0.0,
                       "reason": "already at or below target"}
    line = a.plan_record(f, "x", "y").splitlines()[4]
    assert line == "projected 52.0 V at: now   sunrise 6:31 am"
    assert "?" not in line


def test_a_projection_with_no_label_is_still_not_a_question_mark(a, cfg):
    f = base_facts(cfg)
    f["projection"] = {"reached": f["now"] + 600}
    assert "≤ 15 min" in a.plan_record(f, "x", "y").splitlines()[4]


def test_load_line_admits_when_a_generator_hides_the_load(a, cfg):
    f = base_facts(cfg)
    f["load_w"] = None
    assert a.plan_record(f, "x", "y").splitlines()[0].endswith("load gen running")


# --- the applied line is Python's, not the model's --------------------------

def test_applied_reports_the_learning_phase(a, cfg):
    assert a.applied_line(base_facts(cfg), {"applied": True}) == "no (learning phase)"


def test_applied_reports_a_dry_run(a, cfg):
    assert a.applied_line(base_facts(cfg, gate_open=True), None) == "no (dry run)"


def test_applied_reports_a_real_write(a, cfg):
    a.dry_run = False
    line = a.applied_line(base_facts(cfg, gate_open=True), {
        "applied": True, "now": {"mep_start": 52.0, "mep_stop": 54.5,
                                 "kub_start": 56.0, "kub_stop": 57.0}})
    assert line == "yes - MEP 52.0/54.5, Kubota 56.0/57.0"


def test_applied_reports_a_guard_refusal(a, cfg):
    a.dry_run = False
    line = a.applied_line(base_facts(cfg, gate_open=True),
                          {"applied": False, "reason": "unreachable target"})
    assert line == "no (unreachable target)"


def test_applied_reports_no_change(a, cfg):
    a.dry_run = False
    assert a.applied_line(base_facts(cfg, gate_open=True), None) == "no change"


# --- the recommendation contract -------------------------------------------

def test_recommendation_is_extracted():
    assert agentmod.Agent.extract_recommendation(
        "Thinking about it.\nrecommend: no change - pack is healthy\n") == \
        "no change - pack is healthy"


def test_recommendation_is_case_insensitive():
    assert agentmod.Agent.extract_recommendation(
        "Recommend: Kubota solo to 57.0") == "Kubota solo to 57.0"


def test_a_missing_recommend_line_keeps_the_models_words():
    out = agentmod.Agent.extract_recommendation("Everything looks fine tonight.")
    assert out == "Everything looks fine tonight."


def test_an_empty_answer_is_reported_as_such():
    assert agentmod.Agent.extract_recommendation("") == "no recommendation returned"
    assert agentmod.Agent.extract_recommendation(None) == "no recommendation returned"


# --- answer grounding -------------------------------------------------------

def test_an_ungrounded_answer_is_replaced_with_real_data(a, monkeypatch):
    """POLICY 9: a number no tool returned must never reach the owner."""
    monkeypatch.setattr(a, "run_model", lambda *args, **kw: ("Battery is 99.9 V.", None))
    monkeypatch.setattr(agentmod.history, "fetch_data", lambda *a, **k: {
        "batteryVoltage": 54.08, "battSocBM": 89,
        "mppt80PVPower": 100, "southArrayPVPower": 200, "westArrayPVPower": 300,
        "mep803aAction": history.GEN_STOPPED, "kubotaAction": history.GEN_STOPPED})
    out = a.answer("what is the voltage?", "en")
    assert "99.9" not in out
    assert "54.08 volts" in out and "89 percent" in out
    assert "No generator is running" in out


def test_the_fallback_speaks_spanish_when_asked_in_spanish(a, monkeypatch):
    monkeypatch.setattr(a, "run_model", lambda *args, **kw: ("99.9 V.", None))
    monkeypatch.setattr(agentmod.history, "fetch_data", lambda *a, **k: {
        "batteryVoltage": 54.08, "battSocBM": 89,
        "mppt80PVPower": 0, "southArrayPVPower": 0, "westArrayPVPower": 0,
        "mep803aAction": history.GEN_RUNNING, "kubotaAction": history.GEN_STOPPED})
    out = a.answer("cuanto voltaje?", "es")
    assert "voltios" in out and "54.08" in out and "MEP esta funcionando" in out


def test_plan_is_returned_verbatim_without_the_model(a, conn):
    history.record_plan(conn, "the exact plan text", {"x": 1})
    assert a.answer("plan") == "the exact plan text"
    assert a.answer("  PLAN  ") == "the exact plan text"


def test_plan_before_any_tick_says_so(a):
    assert "No plan" in a.answer("plan")


# --- the overnight report's reference prediction -----------------------------

def plan_at(a, cfg, day, hour, minute, reached_hour, reached_minute=0):
    """One recorded tick, projecting 52 V at the next such clock time."""
    tz = history.tzinfo(cfg)
    when = datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute,
                                                      tzinfo=tz)
    ts = int(when.timestamp())
    reached = when.replace(hour=reached_hour, minute=reached_minute)
    if reached <= when:
        reached += timedelta(days=1)
    reached = int(reached.timestamp())
    history.record_plan(a.conn, f"plan at {hour:02d}:{minute:02d}",
                        {"projection": {"reached": reached}}, ts=ts)
    return ts


@pytest.fixture
def cfg(cfg):
    """A 2.0 h pre-dawn window throughout, so the plan record's shape does not
    move when the site is retuned. test_system.py checks that the manifest's
    own value is what reaches config."""
    return dict(cfg, predawn_hours=2.0)


@pytest.fixture
def morning(cfg):
    """07:00, the hour the overnight report goes out."""
    return int(datetime(2026, 8, 28, 7, 0,
                        tzinfo=history.tzinfo(cfg)).timestamp())


def test_the_report_scores_the_evening_digests_projection(a, cfg, morning):
    """The 21:25 tick had the MEP still cooling; the 19:00 plan is the one the
    owner was shown."""
    plan_at(a, cfg, "2026-08-27", 19, 0, 3, 8)
    plan_at(a, cfg, "2026-08-27", 21, 25, 1, 34)
    plan_at(a, cfg, "2026-08-27", 23, 45, 4, 30)
    ts, p = a.reference_projection(morning)
    assert history.local(ts, cfg).strftime("%H:%M") == "19:00"
    assert history.local(p["reached"], cfg).strftime("%H:%M") == "03:08"


def test_a_later_tick_in_the_digest_hour_does_not_displace_it(a, cfg, morning):
    plan_at(a, cfg, "2026-08-27", 19, 0, 3, 8)
    plan_at(a, cfg, "2026-08-27", 19, 45, 2, 0)
    ts, _ = a.reference_projection(morning)
    assert history.local(ts, cfg).strftime("%H:%M") == "19:00"


def test_without_a_digest_tick_it_takes_the_last_before_midnight(a, cfg, morning):
    plan_at(a, cfg, "2026-08-27", 20, 30, 1, 34)
    plan_at(a, cfg, "2026-08-27", 23, 45, 4, 30)
    plan_at(a, cfg, "2026-08-28", 2, 0, 5, 0)
    ts, p = a.reference_projection(morning)
    assert history.local(ts, cfg).strftime("%H:%M") == "23:45"
    assert history.local(p["reached"], cfg).strftime("%H:%M") == "04:30"


def test_plans_without_a_projection_are_not_candidates(a, cfg, morning):
    ts = int(datetime(2026, 8, 27, 19, 0,
                      tzinfo=history.tzinfo(cfg)).timestamp())
    history.record_plan(a.conn, "no projection",
                        {"projection": {"reached": None, "reason": "x"}}, ts=ts)
    plan_at(a, cfg, "2026-08-27", 23, 45, 4, 30)
    ts, _ = a.reference_projection(morning)
    assert history.local(ts, cfg).strftime("%H:%M") == "23:45"


def test_a_night_with_no_projection_at_all_says_so(a, cfg, morning):
    assert a.reference_projection(morning) == (None, None)


def test_only_after_midnight_projections_are_better_than_none(a, cfg, morning):
    plan_at(a, cfg, "2026-08-28", 2, 0, 5, 0)
    plan_at(a, cfg, "2026-08-28", 4, 0, 5, 30)
    ts, _ = a.reference_projection(morning)
    assert history.local(ts, cfg).strftime("%H:%M") == "04:00"


def test_the_report_names_the_plan_it_scored(a, cfg, morning, monkeypatch, capsys):
    plan_at(a, cfg, "2026-08-27", 19, 0, 3, 8)
    f = base_facts(cfg)
    f["now"] = morning
    monkeypatch.setattr(a, "gather", lambda *x, **k: f)
    text = a.digest(evening=False)
    assert "predicted 52.0 V at 3:08 am  (from the 7:00 pm plan)" in text


def test_a_crossing_past_sunrise_is_not_printed_as_a_bare_time(
        a, cfg, morning, monkeypatch, capsys):
    """The 7:00 pm plan projected 9:10 pm the following evening. Printed as
    "at 9:10 pm" it read as a crossing the owner slept through."""
    tz = history.tzinfo(cfg)
    when = datetime(2026, 8, 27, 19, 0, tzinfo=tz)
    reached = datetime(2026, 8, 28, 21, 10, tzinfo=tz)
    history.record_plan(a.conn, "plan at 19:00",
                        {"projection": {"reached": int(reached.timestamp())}},
                        ts=int(when.timestamp()))
    f = base_facts(cfg)
    f["now"] = morning
    monkeypatch.setattr(a, "gather", lambda *x, **k: f)
    text = a.digest(evening=False)
    assert ("predicted 52.0 V not before sunrise "
            "(next crossing 9:10 pm Aug 28)  (from the 7:00 pm plan)") in text
    assert "at 9:10 pm" not in text


def test_a_crossing_just_before_sunrise_still_prints_its_time(
        a, cfg, morning, monkeypatch, capsys):
    tz = history.tzinfo(cfg)
    when = datetime(2026, 8, 27, 19, 0, tzinfo=tz)
    reached = datetime(2026, 8, 28, 5, 55, tzinfo=tz)
    history.record_plan(a.conn, "plan at 19:00",
                        {"projection": {"reached": int(reached.timestamp())}},
                        ts=int(when.timestamp()))
    f = base_facts(cfg)
    f["now"] = morning
    monkeypatch.setattr(a, "gather", lambda *x, **k: f)
    text = a.digest(evening=False)
    assert "predicted 52.0 V at 5:55 am  (from the 7:00 pm plan)" in text


# --- what the Pi5 watchdog is told to reset to ------------------------------

def test_plan_json_offers_the_config_defaults_by_default(a, cfg):
    payload = a.latest_plan_json()
    assert payload["defaults"] == {"start": cfg["default_start"],
                                   "stop": cfg["default_stop"]}
    assert payload["owner_baseline"] is None


def test_plan_json_offers_the_owners_values_once_they_have_set_them(a, cfg):
    a.guard.state["owner_baseline"] = {"mep_start": 52.0, "mep_stop": 55.0,
                                       "kub_start": 52.0, "kub_stop": 55.0}
    payload = a.latest_plan_json()
    assert payload["defaults"] == {"start": 52.0, "stop": 55.0}
    assert payload["baseline"]["mep_stop"] == 55.0


def test_a_baseline_the_watchdog_cannot_express_is_not_forced_into_one_pair(a, cfg,
                                                                            caplog):
    """It applies one start/stop pair to both generators."""
    a.guard.state["owner_baseline"] = {"mep_start": 52.0, "mep_stop": 55.0,
                                       "kub_start": 53.0, "kub_stop": 56.0}
    payload = a.latest_plan_json()
    assert payload["defaults"] == {"start": cfg["default_start"],
                                   "stop": cfg["default_stop"]}
    assert payload["baseline"]["kub_start"] == 53.0, "still reported in full"
    assert "differs per generator" in caplog.text


# --- anomaly triggers -------------------------------------------------------

def stub_data(**over):
    d = {"pollErrors": 0, "mepAgsOnline": True, "kubotaAgsOnline": True,
         "mppt80PVPower": 2000, "southArrayPVPower": 2000, "westArrayPVPower": 2000,
         "batteryVoltage": 54.0, "autoGenEnabled": True,
         "mep803aAction": history.GEN_STOPPED, "kubotaAction": history.GEN_STOPPED}
    d.update(over)
    return d


@pytest.fixture
def quiet(a, monkeypatch):
    monkeypatch.setattr(a, "on_anomaly", lambda key, msg: None)
    # The shunt check needs a learned pack; the tests that want it stub the
    # model's answer directly, and the rest must not fire it by accident.
    monkeypatch.setattr(a.model, "soc_disagreement",
                        lambda *args, **kw: None)
    return a


def fire(a, monkeypatch, **over):
    monkeypatch.setattr(agentmod.history, "fetch_data", lambda *x, **k: stub_data(**over))
    return {k for k, _ in a.check_anomalies()}


# --- the shunt against the curve --------------------------------------------

def disagreeing(a, monkeypatch, excess):
    monkeypatch.setattr(a.model, "soc_disagreement", lambda *args, **kw: {
        "implied_wh": 24800, "learned_wh": 18900, "excess": excess,
        "nights": 12, "source": "last 60 days", "floor_v": 52.0,
        "voltage": 55.4, "soc_pct": 92})


def test_a_shunt_claiming_a_quarter_more_than_the_curve_is_reported(quiet,
                                                                    monkeypatch):
    """Nothing decides on the Battery Monitor any more, which is exactly why
    a drifted shunt would otherwise never be noticed."""
    disagreeing(quiet, monkeypatch, 0.31)
    assert "soc_drift" in fire(quiet, monkeypatch)


def test_a_shunt_within_a_quarter_is_left_alone(quiet, monkeypatch):
    disagreeing(quiet, monkeypatch, 0.24)
    assert "soc_drift" not in fire(quiet, monkeypatch)


def test_the_shunt_is_not_judged_while_a_generator_runs(quiet, monkeypatch):
    """Both the voltage and the shunt read high under charge, and the curve
    is a discharge curve."""
    disagreeing(quiet, monkeypatch, 0.9)
    assert "soc_drift" not in fire(quiet, monkeypatch,
                                   kubotaAction=history.GEN_RUNNING)


def test_the_shunt_is_not_judged_while_the_monitor_is_offline(quiet, monkeypatch):
    disagreeing(quiet, monkeypatch, 0.9)
    assert "soc_drift" not in fire(quiet, monkeypatch, battMonitorOnline=False)


def test_the_owner_hears_about_the_shunt_once_a_day(quiet, monkeypatch):
    """A shunt does not drift back by itself, so saying it again before
    tomorrow adds nothing."""
    disagreeing(quiet, monkeypatch, 0.31)
    assert "soc_drift" in fire(quiet, monkeypatch)
    assert "soc_drift" not in fire(quiet, monkeypatch)
    quiet.anomaly_last["soc_drift"] -= agentmod.ANOMALY_COOLDOWNS["soc_drift"]
    assert "soc_drift" in fire(quiet, monkeypatch)


def test_the_message_gives_both_figures_and_says_nothing_moved(quiet, monkeypatch):
    disagreeing(quiet, monkeypatch, 0.31)
    key, message = quiet.soc_drift(stub_data())
    assert key == "soc_drift"
    assert "24,800 Wh above 52.0 V" in message
    assert "18,900 Wh (last 60 days, 12 nights)" in message
    assert "31% more" in message
    assert "No decision uses SOC" in message


def test_poll_error_jump_fires(quiet, monkeypatch):
    assert fire(quiet, monkeypatch, pollErrors=0) == set()
    assert "poll_errors" in fire(quiet, monkeypatch, pollErrors=12)


def test_a_small_poll_error_rise_does_not_fire(quiet, monkeypatch):
    fire(quiet, monkeypatch, pollErrors=0)
    assert fire(quiet, monkeypatch, pollErrors=9) == set()


def test_ags_offline_fires(quiet, monkeypatch):
    assert "ags_mepAgsOnline" in fire(quiet, monkeypatch, mepAgsOnline=False)


def test_critical_voltage_fires_regardless_of_generators(quiet, monkeypatch):
    assert "v_critical" in fire(quiet, monkeypatch, batteryVoltage=50.5,
                                autoGenEnabled=True)


def test_low_voltage_fires_only_with_autogen_off_and_no_generator(quiet, monkeypatch):
    assert fire(quiet, monkeypatch, batteryVoltage=52.2) == set()
    assert "v_low_no_autogen" in fire(quiet, monkeypatch, batteryVoltage=52.2,
                                      autoGenEnabled=False)


def test_a_running_generator_suppresses_the_low_voltage_trigger(quiet, monkeypatch):
    assert fire(quiet, monkeypatch, batteryVoltage=52.2, autoGenEnabled=False,
                mep803aAction=history.GEN_RUNNING) == set()


def test_array_imbalance_needs_thirty_minutes(quiet, monkeypatch):
    assert fire(quiet, monkeypatch, westArrayPVPower=10) == set(), "not yet 30 min"
    quiet.array_low_since["west"] -= agentmod.ARRAY_IMBALANCE_SECONDS + 1
    assert "array_west" in fire(quiet, monkeypatch, westArrayPVPower=10)


def test_array_imbalance_resets_when_output_recovers(quiet, monkeypatch):
    fire(quiet, monkeypatch, westArrayPVPower=10)
    assert "west" in quiet.array_low_since
    fire(quiet, monkeypatch, westArrayPVPower=2000)
    assert "west" not in quiet.array_low_since


def test_arrays_are_not_compared_at_night(quiet, monkeypatch):
    fire(quiet, monkeypatch, mppt80PVPower=10, southArrayPVPower=10,
         westArrayPVPower=0)
    assert quiet.array_low_since == {}


def test_dawn_is_not_a_weak_array(quiet, monkeypatch):
    """07:31 on the first live day: mppt80 0 W against an average of 188 W.
    One group faces the sun minutes before the others; that is not a fault."""
    fire(quiet, monkeypatch, mppt80PVPower=0, southArrayPVPower=188,
         westArrayPVPower=188)
    assert quiet.array_low_since == {}, "the others are not making real power yet"


def test_the_others_must_be_over_the_floor_before_it_counts(quiet, monkeypatch):
    fire(quiet, monkeypatch, mppt80PVPower=0, southArrayPVPower=1000,
         westArrayPVPower=1000)
    assert quiet.array_low_since == {}, "an average of exactly 1000 is not over it"
    fire(quiet, monkeypatch, mppt80PVPower=0, southArrayPVPower=1100,
         westArrayPVPower=1100)
    assert "mppt80" in quiet.array_low_since


def test_a_weak_array_sends_the_model_at_the_pv_side(a, monkeypatch):
    asked = []
    monkeypatch.setattr(a, "answer", lambda q, *x, **k: asked.append(q) or "")
    a.on_anomaly("array_mppt80", "mppt80 array has produced under 30%.")
    assert "PV side" in asked[0]
    assert "MPPT" in asked[0] and "breaker" in asked[0]
    assert "do not reach for get_ac_diag" in asked[0]


def test_other_anomalies_get_no_pv_hint(a, monkeypatch):
    asked = []
    monkeypatch.setattr(a, "answer", lambda q, *x, **k: asked.append(q) or "")
    a.on_anomaly("ags_mepAgsOnline", "MEP AGS has gone offline.")
    assert "PV side" not in asked[0]


def test_each_anomaly_has_its_own_cooldown(quiet, monkeypatch):
    assert "ags_mepAgsOnline" in fire(quiet, monkeypatch, mepAgsOnline=False)
    assert fire(quiet, monkeypatch, mepAgsOnline=False) == set(), "still cooling down"
    assert "ags_kubotaAgsOnline" in fire(quiet, monkeypatch, kubotaAgsOnline=False)


def test_an_anomaly_refires_after_the_cooldown(quiet, monkeypatch):
    fire(quiet, monkeypatch, mepAgsOnline=False)
    quiet.anomaly_last["ags_mepAgsOnline"] -= agentmod.ANOMALY_COOLDOWN + 1
    assert "ags_mepAgsOnline" in fire(quiet, monkeypatch, mepAgsOnline=False)


# --- the POLICY evaluation reaches the model and is scored ------------------

def prompt_facts(cfg, **over):
    f = base_facts(cfg)
    f.update(data={}, config={"mep803a": {"maxRuntime": 120},
                              "kubota": {"maxRuntime": 120}},
             intended={"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
             summary_24h={"min_v": 52.4, "max_v": 55.1, "avg_v": 53.8,
                          "solar_wh": 31000, "load_wh": 32000,
                          "gen_minutes": {"mep": 0, "kubota": 0}},
             weather={"tomorrow": {"max_temp_c": 26.0}})
    f.update(over)
    f["policy"] = policy.evaluate(cfg, f, StubModel())
    return f


def test_the_tick_prompt_carries_the_computed_rules(a, cfg):
    prompt = a.tick_prompt(prompt_facts(cfg))
    assert "POLICY EVALUATION" in prompt
    assert "POLICY 4 top-up: FIRES" in prompt
    assert "the arithmetic is already done, do not redo it" in prompt


def test_the_tick_prompt_demands_an_answer_to_a_firing_rule(a, cfg):
    prompt = a.tick_prompt(prompt_facts(cfg))
    assert 'overrule POLICY <n>: <reason>' in prompt
    assert "policy miss" in prompt


def test_the_tick_prompt_says_which_values_the_rule_asks_for(a, cfg):
    """"Set the thresholds it calls for" is not actionable without them."""
    prompt = a.tick_prompt(prompt_facts(cfg))
    assert ("POLICY 4 top-up FIRES → set MEP 54.4/56.4, Kubota 52.0/56.0"
            in prompt)


def test_the_tick_prompt_says_so_when_nothing_fires(a, cfg):
    f = prompt_facts(cfg)
    f["deficit"] = dict(f["deficit"], deficit_wh=-4000)
    f["policy"] = policy.evaluate(cfg, f, StubModel())
    prompt = a.tick_prompt(f)
    assert "No rule fires." in prompt
    assert "FIRES" not in prompt


def test_a_firing_rule_answered_with_no_change_is_audited_as_a_miss(a, cfg, conn):
    f = prompt_facts(cfg)
    missed = policy.misses(f["policy"], "recommend: no change - looks fine", None)
    a.guard.record_policy_miss(missed, "no change - looks fine",
                               f["voltage"], f["soc"], now=f["now"])
    row = conn.execute("SELECT * FROM actions ORDER BY ts DESC LIMIT 1").fetchone()
    assert row["tool"] == "policy_miss" and row["result"] == "missed"
    assert row["allowed"] == 0 and row["voltage"] == 54.2
    assert "POLICY 4 top-up fired" in row["reason"]
    assert "the model said: no change - looks fine" in row["reason"]


def test_an_overruled_rule_is_not_audited_as_a_miss(a, cfg, conn):
    f = prompt_facts(cfg)
    text = ("overrule POLICY 4: the Kubota is mid-cooldown.\n"
            "recommend: no change - waiting out the cooldown")
    assert policy.misses(f["policy"], text, None) == []


def test_the_prompt_names_the_owners_baseline_when_there_is_one(a, cfg):
    f = prompt_facts(cfg, owner_baseline={"mep_start": 52.0, "mep_stop": 55.0,
                                          "kub_start": 52.0, "kub_stop": 55.0})
    prompt = a.tick_prompt(f)
    assert "The owner set MEP 52.0/55.0, Kubota 52.0/55.0 by hand" in prompt
    assert "not the config defaults" in prompt


def test_the_prompt_says_nothing_about_a_baseline_that_does_not_exist(a, cfg):
    assert "by hand" not in a.tick_prompt(prompt_facts(cfg))


# --- the learned voltage/SOC curve reaches the model ------------------------

def test_the_tick_prompt_states_soc_at_the_start_threshold(a, cfg):
    f = base_facts(cfg)
    f.update(data={}, config={"mep803a": {"maxRuntime": 120},
                              "kubota": {"maxRuntime": 120}},
             thresholds={"mep_start": 52.0, "mep_stop": 56.0,
                         "kub_start": 52.0, "kub_stop": 56.0},
             intended={"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
             summary_24h={"min_v": 52.4, "max_v": 55.1, "avg_v": 53.8,
                          "solar_wh": 31000, "load_wh": 32000,
                          "gen_minutes": {"mep": 0, "kubota": 0}},
             weather={"tomorrow": {"max_temp_c": 26.0}})
    prompt = a.tick_prompt(f)
    assert "52.0 V is about 41.0% SOC" in prompt


def test_the_tick_prompt_omits_the_line_when_the_curve_is_unlearned(a, cfg):
    f = base_facts(cfg)
    f["soc_curve"] = {"points": 0, "soc_at_start_threshold": None,
                      "start_threshold_v": 52.0}
    f.update(data={}, config={"mep803a": {"maxRuntime": 120},
                              "kubota": {"maxRuntime": 120}},
             thresholds={"mep_start": 52.0, "mep_stop": 56.0,
                         "kub_start": 52.0, "kub_stop": 56.0},
             intended={"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
             summary_24h={"min_v": 52.4, "max_v": 55.1, "avg_v": 53.8,
                          "solar_wh": 31000, "load_wh": 32000,
                          "gen_minutes": {"mep": 0, "kubota": 0}},
             weather={"tomorrow": {"max_temp_c": 26.0}})
    forecast = a.tick_prompt(f).split("POLICY EVALUATION")[0].split("FORECAST")[1]
    assert "SOC" not in forecast.split("learned:")[0]


# --- the owner reads a 12-hour clock ----------------------------------------

def test_every_time_in_a_plan_record_is_twelve_hour(a, cfg):
    """No 24-hour time may reach the owner. Logs and the database keep theirs."""
    f = base_facts(cfg)
    rec = a.plan_record(f, "no change", "no (dry run)")
    assert "4:00 pm" in rec and "4:10 am" in rec and "6:31 am" in rec
    assert not re.search(r"\b(1[3-9]|2[0-3]):[0-5]\d\b", rec), rec
    for t in re.findall(r"\b\d{1,2}:[0-5]\d\b", rec):
        assert 1 <= int(t.split(":")[0]) <= 12, t


def test_every_time_in_the_tick_prompt_is_twelve_hour(a, cfg):
    """The model quotes these back, so it must never have to convert one."""
    prompt = a.tick_prompt(prompt_facts(cfg))
    assert not re.search(r"\b(1[3-9]|2[0-3]):[0-5]\d\b", prompt), prompt


def test_the_overnight_report_is_twelve_hour(a, cfg, morning, monkeypatch):
    plan_at(a, cfg, "2026-08-27", 19, 0, 3, 8)
    history.record_sample(a.conn, {"batteryVoltage": 52.1, "battSocBM": 40},
                          ts=morning - 3600)
    f = base_facts(cfg)
    f["now"] = morning
    monkeypatch.setattr(a, "gather", lambda *x, **k: f)
    text = a.digest(evening=False)
    assert "3:08 am" in text and "6:00 am" in text
    assert not re.search(r"\b(1[3-9]|2[0-3]):[0-5]\d\b", text), text


# --- a raised start comes back on its own ------------------------------------

def running_facts(a, cfg, gen="kubota", start=53.3, running=True, v=53.1):
    live = {"mep803a": {"startVoltage": 52.0, "stopVoltage": 56.0,
                        "maxRuntime": 120},
            "kubota": {"startVoltage": 52.0, "stopVoltage": 56.0,
                       "maxRuntime": 120}}
    key = "kubota" if gen == "kubota" else "mep803a"
    live[key]["startVoltage"] = start
    action = history.GEN_RUNNING if running else history.GEN_STOPPED
    f = base_facts(cfg, gate_open=True)
    f.update(voltage=v, config=live,
             data={"mep803aAction": (action if gen == "mep" else history.GEN_STOPPED),
                   "kubotaAction": (action if gen == "kubota" else history.GEN_STOPPED)},
             thresholds=toolsmod.thresholds_from_config(live))
    a.dry_run = False
    a.guard.state["raised_starts"] = {gen: {"since": f["now"] - 3600,
                                            "baseline": 52.0, "start": start}}
    return f


@pytest.fixture
def wrote(monkeypatch):
    """Captures the one write the return makes."""
    calls = []
    monkeypatch.setattr(agentmod.toolsmod, "apply_thresholds",
                        lambda cfg, *v, **k: calls.append(v) or {
                            "mep803a": {"startVoltage": v[0], "stopVoltage": v[1]},
                            "kubota": {"startVoltage": v[2], "stopVoltage": v[3]}})
    monkeypatch.setattr(agentmod.telegram, "send", lambda *a, **k: True)
    return calls


def test_the_start_returns_as_soon_as_the_generator_is_running(a, cfg, wrote):
    """The Pi5 ignores start changes mid-run, so there is no window at all."""
    f = running_facts(a, cfg)
    monkey = a.guard.check
    a.guard.check = lambda **kw: (True, "permitted")
    assert a.return_raised_starts(f) == ["kubota"]
    a.guard.check = monkey
    assert wrote == [(52.0, 56.0, 52.0, 56.0)]
    assert a.guard.raised_starts() == {}


def test_it_waits_while_the_generator_has_not_started(a, cfg, wrote):
    f = running_facts(a, cfg, running=False)
    assert a.return_raised_starts(f) == []
    assert wrote == []
    assert "kubota" in a.guard.raised_starts(), "still outstanding"


def test_a_missed_tick_is_caught_once_the_run_has_ended(a, cfg, wrote, conn):
    f = running_facts(a, cfg, running=False)
    since = a.guard.raised_starts()["kubota"]["since"]
    conn.execute("INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, "
                 "kind) VALUES ('kubota', ?, ?, 30, 'agent')",
                 (since + 60, since + 60 + 1800))
    conn.commit()
    a.guard.check = lambda **kw: (True, "permitted")
    assert a.return_raised_starts(f) == ["kubota"]
    assert wrote == [(52.0, 56.0, 52.0, 56.0)]


def test_only_the_raised_generators_start_moves(a, cfg, wrote):
    f = running_facts(a, cfg, gen="mep", start=54.4)
    f["config"]["mep803a"]["stopVoltage"] = 56.4
    f["thresholds"] = toolsmod.thresholds_from_config(f["config"])
    a.guard.check = lambda **kw: (True, "permitted")
    a.return_raised_starts(f)
    assert wrote == [(52.0, 56.4, 52.0, 56.0)], "the stop it charges to stays"


def test_a_start_already_back_is_just_forgotten(a, cfg, wrote):
    f = running_facts(a, cfg, start=52.0)
    assert a.return_raised_starts(f) == []
    assert wrote == [] and a.guard.raised_starts() == {}


def test_a_dry_run_returns_nothing(a, cfg, wrote):
    f = running_facts(a, cfg)
    a.dry_run = True
    assert a.return_raised_starts(f) == []
    assert wrote == []


def test_a_guard_refusal_leaves_it_outstanding(a, cfg, wrote, caplog):
    f = running_facts(a, cfg)
    a.guard.check = lambda **kw: (False, "dashboard data is 900s old")
    assert a.return_raised_starts(f) == []
    assert wrote == []
    assert "kubota" in a.guard.raised_starts(), "to be tried again next tick"
    assert "could not return the raised start" in caplog.text


def test_the_write_is_recorded_as_a_raise_when_it_is_one(a, cfg):
    a.guard.note_write({"mep_start": 54.4, "mep_stop": 56.4,
                        "kub_start": 52.0, "kub_stop": 56.0}, now=1000)
    assert set(a.guard.raised_starts()) == {"mep"}
    a.guard.note_write({"mep_start": 52.0, "mep_stop": 56.4,
                        "kub_start": 52.0, "kub_stop": 56.0}, now=2000)
    assert a.guard.raised_starts() == {}


def test_an_owner_edit_cancels_an_outstanding_raise(a, cfg, conn, monkeypatch):
    """Their values are the baseline now; nothing of the agent's is raised."""
    monkeypatch.setattr(agentmod.guardmod.Guard, "_save_state", lambda self: None)
    monkeypatch.setattr(a.guard.model, "learning_status",
                        lambda **k: {"open": True})
    a.guard.state["raised_starts"] = {"kubota": {"since": 0, "baseline": 52.0,
                                                 "start": 53.3}}
    a.guard.state["intended"] = {"mep_start": 52.0, "mep_stop": 56.0,
                                 "kub_start": 53.3, "kub_stop": 56.0}
    live = {"mep803a": {"startVoltage": 52.0, "stopVoltage": 55.0,
                        "maxRuntime": 120},
            "kubota": {"startVoltage": 52.0, "stopVoltage": 55.0,
                       "maxRuntime": 120}}
    a.guard._evaluate({"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
                      {"battMonitorOnline": True, "clockTime": "22:00:00",
                       "batteryVoltage": 54.0}, live, 54.0,
                      int(datetime(2026, 8, 28, 22, tzinfo=history.tzinfo(cfg))
                          .timestamp()))
    assert a.guard.raised_starts() == {}


# --- a restart does not re-assert what a dead process meant ------------------
#
# 4:01 am freeze, 8:26 am reboot. The state file still said Kubota 53.3/57.0,
# the hourly heartbeat re-asserted it over the owner's 52/56 at 8:27 and 9:27,
# and the Kubota started twice in full sun. The heartbeat reached /config
# without check(), so neither the daylight hold nor the audit log saw it.

STALE = {"mep_start": 52.0, "mep_stop": 56.0,
         "kub_start": 53.3, "kub_stop": 57.0}
OWNERS = {"mep803a": {"startVoltage": 52.0, "stopVoltage": 56.0,
                      "maxRuntime": 120},
          "kubota": {"startVoltage": 52.0, "stopVoltage": 56.0,
                     "maxRuntime": 120}}


@pytest.fixture
def rebooted(a, conn, cfg, monkeypatch):
    """A fresh process whose state file remembers the run that froze."""
    writes = []
    monkeypatch.setattr(agentmod.toolsmod, "apply_thresholds",
                        lambda *v, **k: writes.append((v, k)) or OWNERS)
    monkeypatch.setattr(agentmod.history, "fetch_config", lambda *x, **k: OWNERS)
    monkeypatch.setattr(agentmod.history, "fetch_data", lambda *x, **k: {
        "batteryVoltage": 55.4, "battSocBM": 88, "battMonitorOnline": True,
        "clockTime": "08:27:00", "mep803aAction": history.GEN_STOPPED,
        "kubotaAction": history.GEN_STOPPED, "acPower1": 700, "acPower2": 700,
        "mppt80PVPower": 4000, "southArrayPVPower": 4000,
        "westArrayPVPower": 4000})
    monkeypatch.setattr(agentmod.weather, "summary", lambda *x, **k: {})
    a.dry_run = False
    a.guard.state.update(intended=dict(STALE), owner_baseline=None,
                         raised_starts={"kubota": {"since": 0, "baseline": 52.0,
                                                   "start": 53.3}})
    return a, writes


def test_a_restart_with_stale_intent_writes_nothing(rebooted, cfg):
    """The whole of the incident, in one assertion."""
    a, writes = rebooted
    a.gather()
    assert writes == []


def test_the_live_values_become_the_baseline_on_the_first_gather(rebooted, cfg):
    a, _ = rebooted
    a.gather()
    assert a.guard.baseline() == {"mep_start": 52.0, "mep_stop": 56.0,
                                  "kub_start": 52.0, "kub_stop": 56.0}
    assert a.guard.owner_baseline()["kub_stop"] == 56.0


def test_the_stale_intent_is_discarded_not_re_asserted(rebooted):
    a, _ = rebooted
    a.gather()
    assert a.guard.intended()["kub_start"] == 52.0
    assert a.guard.intended()["kub_stop"] == 56.0


def test_a_raise_recorded_before_the_freeze_is_forgotten(rebooted):
    """It was this process's predecessor that raised it, and the pack has
    moved on. Nothing outstanding survives a restart."""
    a, _ = rebooted
    a.gather()
    assert a.guard.raised_starts() == {}


def test_the_baseline_is_adopted_only_once(rebooted):
    a, _ = rebooted
    a.gather()
    a.guard.state["owner_baseline"] = {"mep_start": 53.0, "mep_stop": 55.0,
                                       "kub_start": 53.0, "kub_stop": 55.0}
    a.gather()
    assert a.guard.baseline()["mep_start"] == 53.0, "not re-adopted every tick"


def test_the_agent_has_no_heartbeat_method_left(a):
    assert not hasattr(a, "heartbeat")
