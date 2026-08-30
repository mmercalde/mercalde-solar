"""The Q&A path: point-in-time readings, plan routing, and dated runs.

Each of these is a question the agent has already answered wrongly once.
"""

from datetime import datetime

import pytest

import agent as agentmod
import eval_cases
import history
import tools as toolsmod


def ts_at(cfg, day, hour, minute=0):
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=history.tzinfo(cfg))
               .timestamp())


@pytest.fixture
def night(conn, cfg):
    """A minute of samples either side of 2:47 am, as the sampler writes them."""
    base = ts_at(cfg, "2026-08-30", 2, 40)
    for i in range(20):
        history.record_sample(conn, {
            "batteryVoltage": round(53.80 - i * 0.02, 2), "battSocBM": 86,
            "battPower": -1400, "battCurrent": -26.0, "battMonitorOnline": True,
            "mep803aAction": history.GEN_STOPPED,
            "kubotaAction": history.GEN_STOPPED,
            "acPower1": 700, "acPower2": 700}, ts=base + i * 60)
    return base


# --- reading a moment ---------------------------------------------------------

def test_a_bare_clock_time_is_the_most_recent_one(cfg):
    now = ts_at(cfg, "2026-08-30", 13, 0)
    assert toolsmod.parse_when("2:47 am", cfg, now) == (
        ts_at(cfg, "2026-08-30", 2, 47), None)
    assert toolsmod.parse_when("11:30 pm", cfg, now) == (
        ts_at(cfg, "2026-08-29", 23, 30), None), "never a time in the future"


def test_a_dated_time_is_taken_as_given(cfg):
    now = ts_at(cfg, "2026-08-30", 13, 0)
    assert toolsmod.parse_when("2026-08-28 2:47 am", cfg, now)[0] == \
        ts_at(cfg, "2026-08-28", 2, 47)


def test_a_time_it_cannot_read_is_refused_not_guessed(cfg):
    when, why = toolsmod.parse_when("some time last night", cfg)
    assert when is None and "could not read" in why


def test_the_reading_at_a_moment_comes_from_the_nearest_sample(conn, cfg, night):
    t = toolsmod.Tools(conn, cfg)
    out = t.get_voltage_at("2:47 am")
    assert out["voltage"] == 53.66
    assert out["sample_at"] == "2026-08-30 2:47 am"
    assert out["seconds_from_asked"] == 0
    assert out["soc_pct"] == 86 and out["load_w"] == 1400
    assert out["generator_running"] is False


def test_a_moment_between_samples_takes_the_closer_one(conn, cfg, night):
    t = toolsmod.Tools(conn, cfg)
    assert t.get_voltage_at("2026-08-30 2:52 am")["sample_at"] == \
        "2026-08-30 2:52 am"


def test_a_moment_with_no_sample_near_it_is_refused(conn, cfg, night):
    t = toolsmod.Tools(conn, cfg)
    out = t.get_voltage_at("2026-08-30 9:15 am")
    assert "no sample within 5 minutes" in out["error"]
    assert out["nearest_sample"] == "2026-08-30 2:59 am"
    assert "voltage" not in out, "no approximation is offered"


def test_the_tool_is_offered_to_the_model():
    named = {s["function"]["name"] for s in toolsmod.SCHEMAS}
    assert "get_voltage_at" in named and "get_voltage_at" in toolsmod.READ_TOOLS
    schema = [s for s in toolsmod.SCHEMAS
              if s["function"]["name"] == "get_voltage_at"][0]
    assert "timestamp" in schema["function"]["parameters"]["required"]
    assert "minimum" in schema["function"]["description"], \
        "the schema warns off the window aggregates that caused the failure"


# --- the plan is the record ---------------------------------------------------

@pytest.mark.parametrize("q", [
    "plan", "/plan", "el plan", "the plan", "latest plan",
    "What is tonight's plan?", "whats the plan", "What's the plan?",
    "What is the plan for tonight?", "cual es el plan", "plan de esta noche",
])
def test_a_plan_question_is_recognised(q):
    assert agentmod.is_plan_question(q)


@pytest.mark.parametrize("q", [
    "What should the plan be if it storms?", "how is the plan going to work",
    "planning permission", "what is the plan for the generator overhaul",
    "why did the plan change", "",
])
def test_a_question_merely_containing_plan_is_not(q):
    assert not agentmod.is_plan_question(q)


def test_the_plan_question_returns_the_record_with_no_model_call(a, conn,
                                                                 monkeypatch):
    """Both Telegram inbound and POST /ask arrive at answer(), so one route
    covers each."""
    history.record_plan(conn, "the exact plan record\nline two", {})
    monkeypatch.setattr(a, "run_model", lambda *x, **k: (_ for _ in ()).throw(
        AssertionError("the model must not be called for a plan question")))
    for q in ("plan", "What is tonight's plan?", "el plan"):
        assert a.answer(q) == "the exact plan record\nline two"


def test_before_any_tick_the_plan_question_says_so(a, monkeypatch):
    monkeypatch.setattr(a, "run_model", lambda *x, **k: (_ for _ in ()).throw(
        AssertionError("no model call")))
    assert "No plan" in a.answer("what is tonight's plan?")


# --- runs carry the day they were on ------------------------------------------

def test_a_run_is_labelled_with_its_day(conn, cfg, monkeypatch):
    now = ts_at(cfg, "2026-08-30", 13, 0)
    monkeypatch.setattr(toolsmod.time, "time", lambda: now)
    for day, hour in (("2026-08-29", 5), ("2026-08-30", 8), ("2026-08-27", 3)):
        start = ts_at(cfg, day, hour)
        conn.execute("INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, "
                     "solo, kind) VALUES ('kubota', ?, ?, 60, 1, 'auto')",
                     (start, start + 3600))
    conn.commit()
    out = toolsmod.Tools(conn, cfg).get_gen_runtime(7)
    days = {r["start"]: r["day"] for r in out["runs"]}
    assert days["2026-08-30 8:00 am"] == "today"
    assert days["2026-08-29 5:00 am"] == "yesterday"
    assert days["2026-08-27 3:00 am"] == "2026-08-27"
    assert out["today"] == "2026-08-30" and out["yesterday"] == "2026-08-29"


def test_a_generator_that_did_not_run_is_named(conn, cfg, monkeypatch):
    now = ts_at(cfg, "2026-08-30", 13, 0)
    monkeypatch.setattr(toolsmod.time, "time", lambda: now)
    start = ts_at(cfg, "2026-08-30", 8)
    conn.execute("INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, "
                 "solo, kind) VALUES ('kubota', ?, ?, 60, 1, 'auto')",
                 (start, start + 3600))
    conn.commit()
    out = toolsmod.Tools(conn, cfg).get_gen_runtime(7)
    assert out["generators_with_no_runs"] == ["mep"]
    assert "do not answer as though the assumption were true" in out["note"]


# --- the graders --------------------------------------------------------------

def test_blaming_a_low_battery_fails_the_start_case():
    truth = {"today": "2026-08-30", "yesterday": "2026-08-29",
             "starts_today": 2, "starts_yesterday": 0,
             "started_above_default": [52.35, 52.6]}
    ok, why = eval_cases.grade_gen_starts(
        "The Kubota started because the battery voltage dropped below its "
        "start threshold.", truth)
    assert not ok and "blames a low battery" in why


def test_naming_the_raised_threshold_passes():
    truth = {"today": "2026-08-30", "yesterday": "2026-08-29",
             "starts_today": 2, "starts_yesterday": 0,
             "started_above_default": [52.35, 52.6]}
    ok, why = eval_cases.grade_gen_starts(
        "A stale start threshold of 53.3 V was in force, so the Pi5 started "
        "the Kubota at 52.35 V.", truth)
    assert ok and "raised start threshold" in why


def test_correcting_the_day_also_passes():
    truth = {"today": "2026-08-30", "yesterday": "2026-08-29",
             "starts_today": 2, "starts_yesterday": 0,
             "started_above_default": []}
    ok, why = eval_cases.grade_gen_starts(
        "It did not start twice yesterday; both starts were today.", truth)
    assert ok and "corrects the day" in why


def test_the_runtime_grader_catches_an_invented_figure():
    truth = {"mep": {"runs": 0, "minutes": 0.0},
             "kubota": {"runs": 3, "minutes": 126.0}}
    ok, why = eval_cases.grade_runtime(
        "The Kubota ran 140 minutes and the MEP did not run.", truth)
    assert not ok and "not in gen_runs" in why


def test_the_runtime_grader_wants_the_idle_generator_named():
    truth = {"mep": {"runs": 0, "minutes": 0.0},
             "kubota": {"runs": 3, "minutes": 126.0}}
    ok, why = eval_cases.grade_runtime("The Kubota ran 126 minutes.", truth)
    assert not ok and "not mentioned" in why
    ok, why = eval_cases.grade_runtime(
        "The Kubota ran 126 minutes over 3 runs; the MEP did not run.", truth)
    assert ok, why


def test_the_plan_grader_wants_the_record_itself():
    truth = {"text": "line one\nrecommend: no change\napplied: no change"}
    assert eval_cases.grade_plan(truth["text"], truth)[0]
    ok, why = eval_cases.grade_plan(
        "Tonight the battery stays between 52 and 56 volts.", truth)
    assert not ok and "not the record" in why


def test_the_point_reading_grader_catches_a_window_aggregate():
    truth = {"asked_for": "2026-08-30 2:47 am", "voltage": 53.66,
             "sample_at": "2026-08-30 2:47 am"}
    ok, why = eval_cases.grade_voltage_at(
        "The battery voltage at 2:47 am was 52.26 V.", truth)
    assert not ok and "the sample at 2026-08-30 2:47 am reads 53.66 V" in why


def test_the_point_reading_grader_accepts_the_real_reading():
    truth = {"asked_for": "2026-08-30 2:47 am", "voltage": 53.66,
             "sample_at": "2026-08-30 2:47 am"}
    assert eval_cases.grade_voltage_at("It was 53.66 V at 2:47 am.", truth)[0]


def test_a_refusal_passes_only_when_there_is_nothing_to_report():
    absent = {"asked_for": "x", "voltage": None, "sample_at": None}
    assert eval_cases.grade_voltage_at(
        "There is no sample within five minutes of that time.", absent)[0]
    present = {"asked_for": "x", "voltage": 53.66, "sample_at": "2:47 am"}
    ok, why = eval_cases.grade_voltage_at("I don't have that reading.", present)
    assert not ok and "declined, but the sample" in why


# --- the question is asked in the owner's words, not a format ---------------

@pytest.mark.parametrize("phrase,expect_day", [
    ("2:47 am", "2026-08-30"),
    ("2:47 a.m.", "2026-08-30"),
    ("last night 2:47 am", "2026-08-30"),
    ("2:47 am last night", "2026-08-30"),
    ("at 2:47 am last night", "2026-08-30"),
    ("2:47 am (last night)", "2026-08-30"),
    ("exactly 2:47 am", "2026-08-30"),
    ("this morning 2:47 am", "2026-08-30"),
    ("yesterday 2:47 am", "2026-08-29"),
    ("yesterday morning 2:47 am", "2026-08-29"),
    ("the day before yesterday 2:47 am", "2026-08-28"),
    ("2026-08-29 2:47 AM", "2026-08-29"),
    ("2026-08-30T02:47:00-07:00", "2026-08-30"),
])
def test_the_time_is_read_however_the_question_put_it(cfg, phrase, expect_day):
    """The model relays the owner's own words. A parser that took only a bare
    clock time turned an answerable question into an apology about formats."""
    now = ts_at(cfg, "2026-08-30", 13, 30)
    when, why = toolsmod.parse_when(phrase, cfg, now)
    assert when is not None, why
    assert history.stamp(when, cfg) == f"{expect_day} 2:47 am"


def test_a_phrase_with_no_clock_time_is_still_refused(cfg):
    now = ts_at(cfg, "2026-08-30", 13, 30)
    for phrase in ("half past two", "last night", "some time before dawn"):
        when, why = toolsmod.parse_when(phrase, cfg, now)
        assert when is None and why


def test_the_owners_phrasing_reaches_the_reading(conn, cfg, night):
    t = toolsmod.Tools(conn, cfg)
    assert t.get_voltage_at("2:47 am last night")["voltage"] == 53.66


def test_the_ask_prompt_says_what_day_it_is():
    """Without it the model invented a date three years out and the reading
    it wanted was never looked up."""
    import prompts
    p = prompts.ask_prompt("en", now_text="2026-08-30 1:26 pm")
    assert "It is 2026-08-30 1:26 pm." in p
    assert "Never supply a date of" in p
    assert "NOW" not in prompts.ask_prompt("en"), "omitted when not supplied"


@pytest.mark.parametrize("phrase", ["8-30 2:47 am", "08-30 02:47", "8/30 2:47 am"])
def test_a_month_and_day_without_a_year_resolves_to_the_recent_one(cfg, phrase):
    """The model reached for this shape and the parser turned it away, so it
    fell back to inventing a full date and read the wrong night."""
    now = ts_at(cfg, "2026-08-30", 13, 30)
    when, why = toolsmod.parse_when(phrase, cfg, now)
    assert when is not None, why
    assert history.stamp(when, cfg) == "2026-08-30 2:47 am"


def test_a_month_and_day_still_to_come_is_taken_as_last_year(cfg):
    now = ts_at(cfg, "2026-08-30", 13, 30)
    when, _ = toolsmod.parse_when("12-25 2:47 am", cfg, now)
    assert history.stamp(when, cfg) == "2025-12-25 2:47 am"


# --- the grader must not mistake a clock time for a figure -------------------

def test_a_clock_time_is_not_one_of_the_numbers_stated():
    """Grading the live 8B, "2:47 am" put 47 into the figures and the grader
    reported the model as saying "47.0 V" when it had said 52.84."""
    assert eval_cases._numbers(
        "The battery voltage at exactly 2:47 am last night was 52.84 V.") == [52.84]


@pytest.mark.parametrize("text,expect", [
    ("It was 53.66 V at 2:47 am.", [53.66]),
    ("At 2:53 a.m. it was 53.10 V.", [53.10]),
    ("At 14:07:30 the pack read 54.2 V.", [54.2]),
    ("On 2026-08-30 at 08:28 the pack read 52.35 V.", [52.35]),
    ("The run began at 11:35 pm and lasted 122 minutes.", [122.0]),
])
def test_times_and_dates_are_stripped_before_the_figures(text, expect):
    assert eval_cases._numbers(text) == expect


def test_the_grader_now_names_the_number_the_model_actually_said():
    truth = {"asked_for": "2026-08-30 2:47 am", "voltage": 53.66,
             "sample_at": "2026-08-30 2:47 am"}
    ok, why = eval_cases.grade_voltage_at(
        "The battery voltage at exactly 2:47 am last night was 52.84 V.", truth)
    assert not ok
    assert "stated 52.84 V" in why, "not 47.0, which is half the clock"


def test_a_time_can_no_longer_pass_an_answer_by_accident():
    """2:53 would have read as 53.0 and matched a true 53.0 V within tolerance,
    passing an answer that never gave a voltage at all."""
    truth = {"asked_for": "2026-08-30 2:53 am", "voltage": 53.0,
             "sample_at": "2026-08-30 2:53 am"}
    ok, why = eval_cases.grade_voltage_at(
        "I checked the log around 2:53 am.", truth)
    assert not ok and "no voltage given" in why


def test_a_correct_answer_still_passes_with_the_time_in_it():
    truth = {"asked_for": "2026-08-30 2:47 am", "voltage": 53.66,
             "sample_at": "2026-08-30 2:47 am"}
    assert eval_cases.grade_voltage_at(
        "At exactly 2:47 am last night the pack read 53.66 V.", truth)[0]


def test_the_runtime_grader_is_not_confused_by_run_times_either():
    truth = {"mep": {"runs": 0, "minutes": 0.0},
             "kubota": {"runs": 3, "minutes": 126.0}}
    ok, why = eval_cases.grade_runtime(
        "The Kubota ran 126 minutes over 3 runs, starting at 11:35 pm; "
        "the MEP did not run.", truth)
    assert ok, why
