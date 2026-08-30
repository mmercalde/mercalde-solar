"""The replay harness's scoring. The model is not needed to test the marking."""

import json
from datetime import datetime

import pytest

import history
import model_eval
import policy
from stubs import StubModel


def ts_at(cfg, day, hour, minute=0):
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=history.tzinfo(cfg))
               .timestamp())


# --- numbers only from tool results -----------------------------------------

def test_a_number_from_the_prompt_is_sourced():
    assert model_eval.unsourced_numbers(
        "The pack is at 54.2 V.", ["battery 54.2 V, SOC 63%"]) == []


def test_a_number_from_a_tool_result_is_sourced():
    assert model_eval.unsourced_numbers(
        "Solar is 3412 W.", ["nothing here", '{"solar_w": 3412}']) == []


def test_an_invented_number_is_flagged():
    """The failure that put a made-up voltage in front of Alexa."""
    assert model_eval.unsourced_numbers(
        "The pack is at 99.9 V.", ["battery 54.2 V"]) == [99.9]


def test_a_thousands_separator_is_the_same_number():
    assert model_eval.unsourced_numbers(
        "deficit 9,000 Wh", ["the deficit is 9000 Wh"]) == []
    assert model_eval.unsourced_numbers(
        "deficit 9000 Wh", ["the deficit is 9,000 Wh"]) == []


def test_arithmetic_the_model_did_itself_is_flagged():
    """POLICY 8 forbids the conversion, so the harness marks it."""
    assert model_eval.unsourced_numbers("load 1.2 kW", ["load 1200 W"]) == [1.2]


def test_several_inventions_are_all_reported():
    out = model_eval.unsourced_numbers("57.4 V and 88% by 3 am", ["nothing"])
    assert out == [3.0, 57.4, 88.0]


# --- tool calls --------------------------------------------------------------

def test_a_good_call_is_not_counted_against_the_model():
    assert model_eval.invalid_calls([("get_status", {}, {"voltage": 54.2})]) == []


def test_an_unknown_tool_is_counted():
    bad = model_eval.invalid_calls(
        [("start_the_generator", {}, {"error": "no such tool: start_the_generator"})])
    assert len(bad) == 1 and "no such tool" in bad[0]


def test_arguments_that_do_not_bind_are_counted():
    bad = model_eval.invalid_calls(
        [("get_history", {"nope": 1}, {"error": "bad arguments for get_history: x"})])
    assert len(bad) == 1 and "bad arguments" in bad[0]


def test_a_tool_that_merely_failed_is_not_the_models_fault():
    """The dashboard being unreachable says nothing about the model."""
    assert model_eval.invalid_calls(
        [("get_status", {}, {"error": "get_status could not reach the dashboard"})]) == []


# --- narrated writes ---------------------------------------------------------

def test_the_message_that_was_sent_after_a_refusal_is_caught():
    """12:17 am: "Adjusted generator thresholds to 52.0/54.5" after a refusal."""
    out = model_eval.narration("recommend: no change - the guard refused",
                               ["Adjusted generator thresholds to 52.0/54.5."])
    assert len(out) == 1 and "Adjusted" in out[0]


def test_the_recommend_line_is_not_narration():
    """Describing what it asked for is the contract, not a claim it happened."""
    assert model_eval.narration(
        "recommend: MEP to 56.4 - the night is 9 kWh short", []) == []


def test_a_claim_in_the_final_answer_is_caught():
    out = model_eval.narration(
        "I have set the Kubota to 55.0.\nrecommend: Kubota to 55.0", [])
    assert len(out) == 1 and "final answer" in out[0]


def test_a_plain_observation_is_not_narration():
    assert model_eval.narration(
        "The pack is healthy and nothing needs to change.", ["All quiet."]) == []


# --- firing rules -------------------------------------------------------------

@pytest.fixture
def fired_tick(cfg):
    night = {
        "now": ts_at(cfg, "2026-08-27", 22), "voltage": 54.2, "soc": 63.0,
        "peak_today": 55.0, "sunrise_ts": ts_at(cfg, "2026-08-28", 6, 21),
        "sunset_ts": ts_at(cfg, "2026-08-27", 19, 24), "remaining_solar_wh": 0,
        "projection": {"reached": ts_at(cfg, "2026-08-28", 3, 8)},
        "tomorrow_cloud": 20,
        "deficit": {"deficit_wh": 9000, "needed_wh": 32000, "available_wh": 23000,
                    "capacity_wh": 100000, "floor_v": 52.0},
        "thresholds": {"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
        "baseline": {"mep_start": 52.0, "mep_stop": 56.0,
                     "kub_start": 52.0, "kub_stop": 56.0},
        "run_window_h": {"mep": 2.0, "kubota": 2.0}}
    rules = policy.evaluate(cfg, night, StubModel())
    assert policy.firing(rules), "the fixture must have a rule that fires"
    return model_eval.Tick(night["now"], "the prompt as it was sent", "",
                           {"policy": rules, "thresholds": night["thresholds"]})


class Recorder:
    """Stands in for EvalTools when only the scoring is under test."""

    def __init__(self, calls=None, sent=None, proposed=None):
        self.calls = calls or []
        self.sent = sent or []
        self.proposed = proposed or []


def test_no_change_past_a_firing_rule_is_a_miss(fired_tick):
    row = model_eval.score_tick(fired_tick, "recommend: no change - looks fine",
                                Recorder())
    assert row["fired"] == 1 and len(row["missed"]) == 1
    assert row["missed"][0].startswith("POLICY 4")
    assert not model_eval.clean(row)


def test_an_overrule_is_not_a_miss(fired_tick):
    row = model_eval.score_tick(
        fired_tick,
        "overrule POLICY 4: the Kubota is mid-cooldown.\nrecommend: no change",
        Recorder())
    assert row["missed"] == [] and model_eval.clean(row)


def test_proposing_what_the_rule_asked_for_is_not_a_miss(fired_tick):
    proposal = policy.firing(fired_tick.policy)[0]["proposal"]
    row = model_eval.score_tick(fired_tick, "recommend: MEP to 56.4",
                                Recorder(proposed=[dict(proposal, reason="x")]))
    assert row["missed"] == []


# --- the table ----------------------------------------------------------------

def test_the_table_has_a_row_for_each_model(cfg, fired_tick):
    good = model_eval.score_tick(fired_tick, "overrule POLICY 4: x\nrecommend: no change",
                                 Recorder())
    bad = model_eval.score_tick(fired_tick, "recommend: no change", Recorder())
    out = model_eval.table({"incumbent": [good, good], "candidate": [bad, good]})
    lines = out.splitlines()
    assert lines[0].startswith("model")
    assert any(l.startswith("incumbent") and "100%" in l for l in lines)
    assert any(l.startswith("candidate") and " 50%" in l for l in lines)


def test_a_model_that_never_answered_says_so(cfg):
    assert "no ticks replayed" in model_eval.table({"candidate": []})


def test_the_faults_name_the_tick_and_the_fault(cfg, fired_tick):
    bad = model_eval.score_tick(fired_tick, "recommend: no change", Recorder())
    out = model_eval.faults({"candidate": [bad]}, cfg)
    assert "candidate" in out and "rule missed    POLICY 4" in out
    assert history.stamp(fired_tick.ts, cfg) in out


def test_a_clean_run_reports_no_faults(cfg, fired_tick):
    good = model_eval.score_tick(fired_tick, "overrule POLICY 4: x\nrecommend: no change",
                                 Recorder())
    assert model_eval.faults({"candidate": [good]}, cfg) == ""


# --- loading the ticks --------------------------------------------------------

def test_only_ticks_that_kept_their_prompt_are_replayed(conn, cfg):
    history.record_plan(conn, "with", {"prompt": "the prompt", "policy": []}, ts=2000)
    history.record_plan(conn, "without", {"policy": []}, ts=3000)
    ticks = model_eval.load_ticks(conn, 10)
    assert [t.ts for t in ticks] == [2000]


def test_the_newest_ticks_are_taken_and_replayed_oldest_first(conn, cfg):
    for i in range(5):
        history.record_plan(conn, "x", {"prompt": f"p{i}"}, ts=1000 + i)
    ticks = model_eval.load_ticks(conn, 3)
    assert [t.ts for t in ticks] == [1002, 1003, 1004]


def test_a_pruned_prompt_drops_out_of_the_replay_pool(conn, cfg):
    old, new = 1000, 1000 + 30 * 86400
    history.record_plan(conn, "old", {"prompt": "p", "answer": "a"}, ts=old)
    history.record_plan(conn, "new", {"prompt": "p", "answer": "a"}, ts=new)
    assert history.purge_plan_prompts(conn, cfg, days=14, now=new) == 1
    assert [t.ts for t in model_eval.load_ticks(conn, 10)] == [new]
    kept = json.loads(conn.execute("SELECT data FROM plans WHERE ts=?",
                                   (old,)).fetchone()["data"])
    assert "prompt" not in kept and conn.execute(
        "SELECT text FROM plans WHERE ts=?", (old,)).fetchone()["text"] == "old"


# --- candidates ----------------------------------------------------------------

def test_a_candidate_can_name_its_own_model():
    url, name = model_eval.parse_candidate(
        "http://127.0.0.1:8082/v1/chat/completions=qwen3-14b")
    assert url.endswith("8082/v1/chat/completions") and name == "qwen3-14b"


def test_a_candidate_without_a_name_reuses_the_configured_one():
    url, name = model_eval.parse_candidate("http://127.0.0.1:8082/v1/chat/completions")
    assert name is None


def test_a_candidate_points_the_client_at_its_own_endpoint(cfg):
    c = model_eval.Candidate("http://127.0.0.1:8082/v1/chat/completions", "qwen3-14b")
    client = c.llm(cfg, timeout=30)
    assert client.url.endswith("8082/v1/chat/completions")
    assert client.model == "qwen3-14b"
    assert cfg["llm_url"] != client.url, "the live config is not touched"


def test_a_rule_number_is_a_citation_not_a_figure():
    """"overrule POLICY 4" is naming a rule, not asserting the number four."""
    assert model_eval.unsourced_numbers(
        "overrule POLICY 4: the pack is fine", ["nothing numeric here"]) == []
    assert model_eval.unsourced_numbers(
        "POLICY 3 says so, and the pack is at 57.4 V", ["nothing"]) == [57.4]


def test_a_call_that_names_no_tool_is_still_recorded(conn, cfg):
    """Tools.call drops it before self.calls; the harness must not."""
    t = model_eval.EvalTools(conn, cfg, 1000, {})
    t.call("start_the_generator", {})
    assert len(t.calls) == 1
    assert model_eval.invalid_calls(t.calls)[0].startswith("start_the_generator")


def test_the_replay_writes_nothing_and_sends_nothing(conn, cfg):
    t = model_eval.EvalTools(conn, cfg, 1000, {})
    out = json.loads(t.call("set_gen_thresholds",
                            {"mep_start": 55.0, "mep_stop": 57.0,
                             "kub_start": 52.0, "kub_stop": 56.0, "reason": "x"}))
    assert out["applied"] is False and out["replay"] is True
    assert t.proposed[0]["mep_start"] == 55.0
    sent = json.loads(t.call("send_telegram", {"text": "hello"}))
    assert sent["sent"] is False and t.sent == ["hello"]
