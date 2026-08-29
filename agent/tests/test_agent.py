"""Agent logic that runs without a model or a network: the plan record,
the recommendation contract, answer grounding, and the anomaly triggers."""

import sqlite3
from datetime import datetime

import pytest

import agent as agentmod
import history


@pytest.fixture
def a(cfg, conn, monkeypatch, tmp_path):
    # Agent resolves a connection per thread; hand every thread the same
    # in-memory one. `connect` is only used once, to create the schema, and
    # its result is closed, so it must not be the shared connection.
    monkeypatch.setattr(agentmod.history, "connect",
                        lambda *a, **k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(agentmod.history, "thread_connection", lambda *a, **k: conn)
    monkeypatch.setattr(agentmod.guardmod.Guard, "_save_state", lambda self: None)
    inst = agentmod.Agent(cfg, dry_run=True)
    inst.guard.state_path = str(tmp_path / "state.json")
    return inst


def base_facts(cfg, gate_open=False):
    now = int(datetime(2026, 8, 28, 16, 0,
                       tzinfo=history.tzinfo(cfg)).timestamp())
    return {
        "now": now, "today": "2026-08-28",
        "data": {}, "config": {},
        "voltage": 55.8, "soc": 84, "load_w": 1100, "solar_w": 3000,
        "gen_running": False, "peak_today": 55.8,
        "weather": {}, "sunrise_ts": int(datetime(
            2026, 8, 29, 6, 31, tzinfo=history.tzinfo(cfg)).timestamp()),
        "forecast": {"learned": True, "hours": 12, "total_wh": 10800},
        "projection": {"reached": now + 43800, "at": "04:10", "hours": 12.2},
        "drawdown": {"wh": 10800, "month": 8, "nights": 12},
        "gate": {"open": gate_open},
        "soc_curve": {"points": 12, "soc_at_start_threshold": 41.0,
                      "start_threshold_v": 52.0, "volts_low": 51.8,
                      "volts_high": 55.4, "observations": 9000,
                      "scraped_observations": 9000},
        "tomorrow_cloud": 20,
        "est_solar": {"wh": 61000, "clear_day_wh": 68000},
        "summary_24h": {}, "thresholds": {}, "intended": {},
    }


# --- the plan record --------------------------------------------------------

def test_plan_record_matches_the_spec_shape(a, cfg):
    rec = a.plan_record(base_facts(cfg),
                        "Kubota solo, start 56.0 / stop 57.0; MEP 52.0 / 54.5",
                        "no (learning phase)")
    lines = rec.splitlines()
    assert len(lines) == 7
    assert lines[0] == "2026-08-28 16:00  V 55.8  SOC 84%  load 1.1 kW"
    assert lines[1] == "peak today: 55.8 V  (threshold 57.0 -> solar shortfall)"
    assert lines[2] == "overnight Wh (profile, Aug weekday): 10,800"
    assert lines[3] == "projected 52.0 V at: 04:10   sunrise 06:31"
    assert lines[4] == ("forecast tomorrow: 20% cloud, est. solar 61.0 kWh "
                        "(Aug clear-day 68.0)")
    assert lines[5].startswith("recommend: Kubota solo")
    assert lines[6] == "applied: no (learning phase)"


def test_peak_at_or_above_threshold_is_not_a_shortfall(a, cfg):
    f = base_facts(cfg)
    f["peak_today"] = 57.2
    assert "reached" in a.plan_record(f, "x", "y").splitlines()[1]


def test_plan_record_says_what_it_has_not_learned(a, cfg):
    f = base_facts(cfg)
    f["drawdown"] = None
    f["projection"] = {"reached": None, "reason": "pack capacity not learned"}
    f["est_solar"] = None
    lines = a.plan_record(f, "no change", "no (learning phase)").splitlines()
    assert lines[2].endswith("not learned yet")
    assert "not projected (pack capacity not learned)" in lines[3]
    assert lines[4].endswith("not learned yet")


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
    """POLICY 7: a number no tool returned must never reach the owner."""
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


# --- anomaly triggers -------------------------------------------------------

def stub_data(**over):
    d = {"pollErrors": 0, "mepAgsOnline": True, "kubotaAgsOnline": True,
         "mppt80PVPower": 1000, "southArrayPVPower": 1000, "westArrayPVPower": 1000,
         "batteryVoltage": 54.0, "autoGenEnabled": True,
         "mep803aAction": history.GEN_STOPPED, "kubotaAction": history.GEN_STOPPED}
    d.update(over)
    return d


@pytest.fixture
def quiet(a, monkeypatch):
    monkeypatch.setattr(a, "on_anomaly", lambda key, msg: None)
    return a


def fire(a, monkeypatch, **over):
    monkeypatch.setattr(agentmod.history, "fetch_data", lambda *x, **k: stub_data(**over))
    return {k for k, _ in a.check_anomalies()}


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
    fire(quiet, monkeypatch, westArrayPVPower=1000)
    assert "west" not in quiet.array_low_since


def test_arrays_are_not_compared_at_night(quiet, monkeypatch):
    fire(quiet, monkeypatch, mppt80PVPower=10, southArrayPVPower=10,
         westArrayPVPower=0)
    assert quiet.array_low_since == {}


def test_each_anomaly_has_its_own_cooldown(quiet, monkeypatch):
    assert "ags_mepAgsOnline" in fire(quiet, monkeypatch, mepAgsOnline=False)
    assert fire(quiet, monkeypatch, mepAgsOnline=False) == set(), "still cooling down"
    assert "ags_kubotaAgsOnline" in fire(quiet, monkeypatch, kubotaAgsOnline=False)


def test_an_anomaly_refires_after_the_cooldown(quiet, monkeypatch):
    fire(quiet, monkeypatch, mepAgsOnline=False)
    quiet.anomaly_last["ags_mepAgsOnline"] -= agentmod.ANOMALY_COOLDOWN + 1
    assert "ags_mepAgsOnline" in fire(quiet, monkeypatch, mepAgsOnline=False)


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
    assert "SOC" not in a.tick_prompt(f).split("learned:")[0].split("FORECAST")[1]
