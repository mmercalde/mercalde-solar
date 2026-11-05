"""--exam --candidate: the agent's own answer(), on another model, touching nothing live.

No model is needed: LLM.chat is scripted. The "live" data directory is a temp
directory, and the tests check it is byte-for-byte what it was before.
"""

import json
import os
import sys
import time

import pytest

import agent as agentmod
import config
import eval_cases
import history
import llm as llmmod
import model_eval
import telegram
import tools as toolsmod
import weather

URL = "http://127.0.0.1:8082/v1/chat/completions"
LIVE_CONFIG = {"mep803a": {"startVoltage": 52.0, "stopVoltage": 57.0, "maxRuntime": 120,
                           "chargeRate": 100, "cooldown": 5},
               "kubota": {"startVoltage": 52.0, "stopVoltage": 57.0, "maxRuntime": 120,
                          "chargeRate": 70, "cooldown": 5}}
LIVE_DATA = {"batteryVoltage": 54.25, "battSocBM": 70, "mppt80PVPower": 100,
             "southArrayPVPower": 0, "westArrayPVPower": 0,
             "mep803aAction": 10, "kubotaAction": 10}


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def says(text):
    return {"role": "assistant", "content": text}


@pytest.fixture
def script(monkeypatch):
    """Replies for LLM.chat, in order, and a record of who was asked."""
    replies, seen = [], []

    def chat(self, messages, tools=None, **kwargs):
        seen.append((self.url, self.model))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return dict(reply)

    monkeypatch.setattr(llmmod.LLM, "chat", chat)
    return replies, seen


@pytest.fixture
def live(monkeypatch, tmp_path):
    """A stand-in for agent/data, with a plan, a guard state and an audit log."""
    data = tmp_path / "live"
    data.mkdir()
    db = str(data / "history.sqlite")
    conn = history.connect(db)
    history.record_plan(conn, "the plan as recorded", {"prompt": "p"}, ts=1000)
    conn.close()
    (data / "guard_state.json").write_text(json.dumps(
        {"intended": dict(toolsmod.thresholds_from_config(LIVE_CONFIG)), "last_write_ts": 0,
         "override_until": 0, "override_adopted": None,
         "owner_baseline": toolsmod.thresholds_from_config(LIVE_CONFIG),
         "raised_starts": {}, "lowered_stops": {}}))
    (data / "topup_state.json").write_text("{}")
    (data / "audit.log").write_text("the real audit log\n")
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "AUDIT_LOG", str(data / "audit.log"))
    monkeypatch.setattr(weather, "ARCHIVE_CACHE", None)
    monkeypatch.setattr(history, "fetch_data", lambda *a, **k: dict(LIVE_DATA))
    monkeypatch.setattr(history, "fetch_config", lambda *a, **k: json.loads(json.dumps(LIVE_CONFIG)))
    return data


@pytest.fixture
def snapshot(live, tmp_path):
    return model_eval.make_snapshot(str(tmp_path / "abexam"))


def tree(path):
    return {n: (path / n).read_bytes() for n in sorted(os.listdir(path))
            if not n.endswith(("-wal", "-shm"))}


def candidate():
    return model_eval.Candidate(URL, "gemma4")


def build(cfg, snapshot):
    directory = model_eval.prepare_sandbox(candidate(), snapshot)
    return directory, model_eval.build_exam_agent(candidate(), cfg, directory)


# --- the candidate's config ---------------------------------------------------

def test_the_agent_talks_to_the_candidate_and_the_cfg_is_not_touched(cfg, snapshot):
    before = json.dumps(cfg, sort_keys=True)
    directory = model_eval.prepare_sandbox(candidate(), snapshot)
    with model_eval.sandboxed(directory):
        a = model_eval.build_exam_agent(candidate(), cfg, directory)
    assert a.llm.url == URL and a.llm.model == "gemma4"
    assert a.cfg["llm_url"] == URL
    assert json.dumps(cfg, sort_keys=True) == before
    assert cfg["llm_url"] != URL


def test_the_question_is_put_to_the_candidates_endpoint(cfg, snapshot, script):
    replies, seen = script
    replies += [call("get_status"), says("The pack is at 54.25 V.")]
    model_eval.exam_candidate(candidate(), cfg, snapshot, only=["none"],
                              questions=[("voltage?", None)])
    assert seen == [(URL, "gemma4")] * 2


# --- dry run ------------------------------------------------------------------

def test_dry_run_is_enforced(cfg, snapshot):
    directory = model_eval.prepare_sandbox(candidate(), snapshot)
    with model_eval.sandboxed(directory):
        a = model_eval.build_exam_agent(candidate(), cfg, directory)
    assert a.dry_run is True
    assert a.tools().dry_run is True


def test_a_write_and_a_telegram_from_the_model_go_nowhere(cfg, snapshot, script, live, monkeypatch):
    """The questions never ask for a write. A model may offer one regardless."""
    def forbidden(*a, **k):
        raise AssertionError("a dry run reached the outside world")
    monkeypatch.setattr(toolsmod, "apply_thresholds", forbidden)
    monkeypatch.setattr(telegram, "send", forbidden)
    replies, _ = script
    replies += [call("set_gen_thresholds", mep_start=54.0, mep_stop=57.0,
                     kub_start=54.0, kub_stop=57.0, reason="unprompted"),
                call("send_telegram", text="hello"), says("Done.")]
    before = tree(live)
    result = model_eval.exam_candidate(candidate(), cfg, snapshot, only=["none"],
                                       questions=[("how is the pack?", None)])
    row = result["questions"][0]
    assert [c["name"] for c in row["tool_calls"]] == ["set_gen_thresholds", "send_telegram"]
    assert model_eval.exam_summary(result)["write_attempts"] == 1
    assert tree(live) == before, "nothing live moved: database, guard state, audit log"


def test_the_guard_and_top_up_state_files_are_the_sandbox_copies(cfg, snapshot, live):
    """Guard.check can reach adopt_owner, which saves its state in a dry run too."""
    before = tree(live)
    directory = model_eval.prepare_sandbox(candidate(), snapshot)
    with model_eval.sandboxed(directory):
        a = model_eval.build_exam_agent(candidate(), cfg, directory)
        assert a.guard.state_path == os.path.join(directory, "guard_state.json")
        assert a.topup.path == os.path.join(directory, "topup_state.json")
        assert a.guard.owner_baseline() is not None, "the state was copied, not lost"
        a.guard.adopt_owner(dict(toolsmod.thresholds_from_config(LIVE_CONFIG), mep_start=53.0),
                            2000, ["mep"], "test")
        a.topup.save()
        a.guard._audit_line(2000, "a line")
    assert tree(live) == before


def test_the_sandbox_is_undone_afterwards(live):
    before = (config.DATA_DIR, config.DB_PATH, config.AUDIT_LOG, weather.ARCHIVE_CACHE)
    with pytest.raises(ValueError):
        with model_eval.sandboxed("/tmp/nowhere"):
            assert config.AUDIT_LOG == "/tmp/nowhere/audit.log"
            raise ValueError
    assert (config.DATA_DIR, config.DB_PATH, config.AUDIT_LOG, weather.ARCHIVE_CACHE) == before


# --- the snapshot -------------------------------------------------------------

def test_the_answer_and_the_truth_come_from_the_snapshot(cfg, snapshot, live, script):
    """The live database moves on; the exam must not see it."""
    conn = history.connect(config.DB_PATH)
    history.record_plan(conn, "a newer plan on the live database", {"prompt": "p"}, ts=2000)
    conn.close()
    result = model_eval.exam_candidate(candidate(), cfg, snapshot, only=["tonight_plan"])
    row = result["cases"][0]
    assert row["reply"] == "the plan as recorded"
    assert row["truth"]["text"] == "the plan as recorded"
    assert row["passed"] is True


def test_the_snapshot_is_taken_once_unless_asked(live, tmp_path):
    directory = str(tmp_path / "abexam")
    path = model_eval.make_snapshot(directory)
    conn = history.connect(config.DB_PATH)
    history.record_plan(conn, "newer", {}, ts=2000)
    conn.close()

    def latest():
        c = history.readonly_connection(path)
        try:
            return history.latest_plan(c)["text"]
        finally:
            c.close()

    assert model_eval.make_snapshot(directory) == path and latest() == "the plan as recorded"
    model_eval.make_snapshot(directory, fresh=True)
    assert latest() == "newer"


def test_one_models_proposal_does_not_reach_the_next(cfg, snapshot, script):
    """It would appear in the next model's plan context and get_recent_actions."""
    def actions(path):
        c = history.readonly_connection(path)
        try:
            return len(history.recent_actions(c, limit=10))
        finally:
            c.close()

    replies, _ = script
    replies += [call("set_gen_thresholds", mep_start=54.0, mep_stop=57.0,
                     kub_start=54.0, kub_stop=57.0, reason="x"), says("Done.")]
    first = model_eval.exam_candidate(candidate(), cfg, snapshot, only=["none"],
                                      questions=[("q", None)])
    assert actions(os.path.join(first["sandbox"], "history.sqlite")) == 1
    assert actions(snapshot) == 0
    other = model_eval.Candidate(URL, "qwen3")
    second = model_eval.prepare_sandbox(other, snapshot)
    assert actions(os.path.join(second, "history.sqlite")) == 0


def test_past_midnight_the_snapshot_says_so(cfg, snapshot):
    taken = int(os.path.getmtime(snapshot))
    assert model_eval.snapshot_warning(snapshot, cfg, now=taken) is None
    assert "last night" in model_eval.snapshot_warning(snapshot, cfg, now=taken + 2 * 86400)


# --- what is recorded per answer ----------------------------------------------

@pytest.fixture
def asked(cfg, snapshot, script):
    replies, _ = script

    def ask(*script_replies, question="how is the pack?", lang=None):
        replies[:] = list(script_replies)
        result = model_eval.exam_candidate(candidate(), cfg, snapshot, only=["none"],
                                           questions=[(question, lang)])
        return result["questions"][0]
    return ask


def test_a_grounded_answer(asked):
    row = asked(call("get_status"), says("The pack is at 54.25 V."))
    assert row["answered_by"] == "model" and row["unsourced"] == []
    assert [c["name"] for c in row["tool_calls"]] == ["get_status"]
    assert not row["ungrounded_retry"] and not row["status_fallback"]
    assert row["wall_s"] >= 0


def test_each_answer_records_when_it_was_asked(asked):
    before = int(time.time())
    row = asked(call("get_status"), says("The pack is at 54.25 V."))
    assert before <= row["asked_at"] <= row["ended_at"] <= time.time()


def test_an_invented_number_is_reported(asked):
    assert asked(call("get_status"), says("The pack is at 99.9 V."))["unsourced"] == [99.9]


def test_the_question_is_a_source_for_its_own_numbers(asked):
    row = asked(call("get_status"), says("At 3:15 there is nothing."),
                question="what was it at 3:15?")
    assert row["unsourced"] == []


def test_the_retry_is_seen(asked):
    row = asked(says("I think 54 V."), call("get_status"), says("It is 54.25 V."))
    assert row["ungrounded_retry"] and not row["status_fallback"]
    assert row["answered_by"] == "model"


def test_the_status_fallback_is_seen_and_its_numbers_are_not_the_models(asked):
    row = asked(says("I think 54 V."), says("Still 54 V."))
    assert row["ungrounded_retry"] and row["status_fallback"]
    assert row["answered_by"] == "python_status"
    assert row["unsourced"] == [] and "54.25" in row["reply"]


def test_a_model_that_is_down_is_not_scored_as_a_wrong_answer(asked):
    row = asked(llmmod.LLMError("connection refused"))
    assert row["answered_by"] == "model_error" and row["reply"]


def test_a_spanish_decimal_comma_is_one_number(asked):
    row = asked(call("get_status"), says("La batería está a 54,25 voltios."),
                question="¿voltaje?", lang="es")
    assert row["unsourced"] == [] and row["lang"] == "es"


def test_an_invented_spanish_figure_is_still_reported(asked):
    row = asked(call("get_status"), says("La batería está a 99,9 voltios."),
                question="¿voltaje?", lang="es")
    assert row["unsourced"] == [99.9]


def test_english_commas_stay_thousands_separators(asked):
    row = asked(call("get_status"), says("About 1,200 watts."))
    assert row["unsourced"] == [1200.0]


# --- repeats and the Python-answered case -------------------------------------

def test_repeat_asks_each_case_that_many_times(cfg, snapshot, script):
    replies, _ = script
    replies += [call("get_status"), says("I do not have that reading."),
                call("get_status"), says("Pack at 54.25.")]
    result = model_eval.exam_candidate(candidate(), cfg, snapshot, repeat=2,
                                       only=["voltage_at_247"])
    assert [r["attempt"] for r in result["cases"]] == [1, 2]
    assert model_eval.exam_summary(result)["graded"] == 2


def test_the_plan_case_is_counted_as_python_not_the_model(cfg, snapshot, script):
    _, seen = script
    result = model_eval.exam_candidate(candidate(), cfg, snapshot, repeat=3,
                                       only=["tonight_plan"])
    assert seen == [], "the model was never asked"
    assert {r["answered_by"] for r in result["cases"]} == {"python_plan"}
    s = model_eval.exam_summary(result)
    assert s["graded"] == 0 and s["python_answered"] == 3
    assert "answered by Python" in model_eval.exam_candidate_report([result])


def test_the_built_in_plan_case_really_is_python_answered():
    plan = [c for c in eval_cases.CASES if c["id"] == "tonight_plan"][0]
    assert agentmod.is_plan_question(plan["question"])
    others = [c for c in eval_cases.CASES if c["id"] != "tonight_plan"]
    assert not any(agentmod.is_plan_question(c["question"]) for c in others)


def test_pause_only_falls_between_questions(cfg, snapshot, script, monkeypatch):
    slept = []
    monkeypatch.setattr(model_eval.time, "sleep", slept.append)
    replies, _ = script
    replies += [call("get_status"), says("a"), call("get_status"), says("b")]
    model_eval.exam_candidate(candidate(), cfg, snapshot, only=["none"], pause=30,
                              questions=[("one", None), ("two", None)])
    assert slept == [30]


# --- the question file --------------------------------------------------------

def test_questions_parse_with_language_and_comments():
    assert model_eval.parse_questions(
        ["# a note\n", "\n", "How is it?\n", "es: ¿Cómo está?\n", "ES:Hola\n"]
    ) == [("How is it?", None), ("¿Cómo está?", "es"), ("Hola", "es")]


def test_the_shipped_question_file():
    path = os.path.join(os.path.dirname(config.__file__), "docs", "exam_questions_2026-11.txt")
    with open(path, encoding="utf-8") as f:
        questions = model_eval.parse_questions(f)
    assert 12 <= len(questions) <= 18
    assert sum(lang == "es" for _, lang in questions) >= 5
    assert not any(agentmod.is_plan_question(q) for q, _ in questions), \
        "a Python-answered question tells us nothing about the model"


# --- the default is unchanged -------------------------------------------------

def run_main(monkeypatch, cfg, argv):
    monkeypatch.setattr(sys, "argv", ["model_eval.py"] + argv)
    monkeypatch.setattr(config, "load", lambda *a, **k: cfg)
    return model_eval.main()


def test_plain_exam_still_goes_to_the_running_agent(cfg, live, monkeypatch, capsys):
    posted = []

    def fake_exam(url, cfg_, conn, timeout=90, only=None):
        posted.append(url)
        return [{"id": "x", "question": "q", "about": "a", "reply": "r",
                 "pass": True, "why": "w", "truth": {}}]

    def forbidden(*a, **k):
        raise AssertionError("plain --exam must not take the candidate path")

    monkeypatch.setattr(model_eval, "exam", fake_exam)
    monkeypatch.setattr(model_eval, "make_snapshot", forbidden)
    monkeypatch.setattr(model_eval, "run_candidate_exam", forbidden)
    assert run_main(monkeypatch, cfg, ["--exam"]) == 0
    assert len(posted) == 1 and posted[0].endswith("/ask")
    assert "1/1 passed" in capsys.readouterr().out


def test_a_candidate_routes_to_the_new_path(cfg, live, monkeypatch):
    called = []

    def fake(args, cfg_):
        called.append(args.candidate)
        return 0

    def forbidden(*a, **k):
        raise AssertionError("the running agent is not to be asked")

    monkeypatch.setattr(model_eval, "run_candidate_exam", fake)
    monkeypatch.setattr(model_eval, "exam", forbidden)
    assert run_main(monkeypatch, cfg, ["--exam", "--candidate", f"{URL}=gemma4"]) == 0
    assert called == [[f"{URL}=gemma4"]]


@pytest.mark.parametrize("argv", [["--exam", "--repeat", "3"],
                                  ["--exam", "--questions", "f"],
                                  ["--exam", "--pause", "30"],
                                  ["--exam", "--fresh-snapshot"],
                                  ["--repeat", "3", "--candidate", URL],
                                  ["--exam", "--candidate", URL, "--repeat", "0"]])
def test_the_new_flags_are_refused_where_they_would_mean_nothing(cfg, monkeypatch, argv):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, cfg, argv)


def test_json_output_end_to_end(cfg, live, tmp_path, monkeypatch, script, capsys):
    monkeypatch.setattr(model_eval, "SNAPSHOT_DIR", str(tmp_path / "abexam"))
    real = model_eval.make_snapshot
    monkeypatch.setattr(model_eval, "make_snapshot",
                        lambda fresh=False: real(str(tmp_path / "abexam"), fresh=fresh))
    qfile = tmp_path / "q.txt"
    qfile.write_text("es: ¿Voltaje?\n", encoding="utf-8")
    replies, _ = script
    replies += [call("get_status"), says("54,25 voltios.")]
    assert run_main(monkeypatch, cfg, ["--exam", "--case", "tonight_plan",
                                       "--candidate", f"{URL}=gemma4",
                                       "--questions", str(qfile), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["summary"]["python_answered"] == 1
    assert out[0]["questions"][0]["lang"] == "es"
    assert out[0]["questions"][0]["reply"] == "54,25 voltios."
