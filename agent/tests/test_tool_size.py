"""Tool results have to fit. llama-server has 16,384 tokens for the whole prompt
and the reply, and on 2026-11-05 get_gen_runtime(days=90) would have taken 20,000
of them alone.

Sizes are in characters here, with no tokenizer in the test. Each ratio is the
densest measured for that tool by Gemma's tokenizer, so a size under budget here
is under it there. See docs/tool_fixes_2026-11-06.md.
"""

import json
import logging
import random
import time

import pytest

import history
import tools

# Gemma's tokenizer on these results: runtime is mostly digits, the log mostly words.
RUNTIME_CHARS_PER_TOKEN = 1.9
ACTIONS_CHARS_PER_TOKEN = 2.5
RUNTIME_TOKENS = 3250
ACTIONS_TOKENS = 3300


def busy_year(conn, nights=365):
    """Both generators every night, every figure present, as long as a figure gets.

    The worst a year can be, more than twice what the record has ever held, and
    no field left NULL for the rows to look smaller than they will.
    """
    rng = random.Random(7)
    now = int(time.time())
    for night in range(nights):
        for gen, minutes in (("mep", 62.5), ("kubota", 58.5)):
            start = now - night * 86400 - 3600
            conn.execute(
                "INSERT OR IGNORE INTO gen_runs (gen, start_ts, stop_ts, duration_min, "
                "start_v, stop_v, rate_a, load_w, fuel_gal, gross_w, gross_attr_w, "
                "rate_v_per_h, solo, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gen, start + (gen == "mep"), start + 3600, minutes,
                 52.0 + rng.random(), 56.0 + rng.random(), 40.0 + rng.random() * 30,
                 1500 + rng.random() * 900, round(0.4 + rng.random(), 3),
                 3000 + rng.random() * 3000, 2500 + rng.random() * 2500,
                 3.0 + rng.random() * 3, 0,
                 "exercise" if night % 9 == 0 else "manual"))
    conn.commit()
    return now


@pytest.fixture
def year(conn):
    busy_year(conn)
    return conn


@pytest.mark.parametrize("days", [7, 30, 90, 365])
def test_runtime_stays_under_its_budget_for_a_busy_year(year, cfg, days):
    payload = tools.Tools(year, cfg).call("get_gen_runtime", {"days": days})
    assert len(payload) / RUNTIME_CHARS_PER_TOKEN < RUNTIME_TOKENS
    assert "error" not in json.loads(payload)


def test_runtime_at_the_row_budget_is_under_its_budget_too(conn, cfg):
    busy_year(conn, nights=tools.RUN_ROW_BUDGET // 2)
    payload = tools.Tools(conn, cfg).call("get_gen_runtime", {"days": 365})
    assert len(json.loads(payload)["runs"]) == tools.RUN_ROW_BUDGET
    assert len(payload) / RUNTIME_CHARS_PER_TOKEN < RUNTIME_TOKENS


def test_a_short_span_keeps_every_run_and_says_nothing_of_summaries(conn, cfg):
    busy_year(conn, nights=3)
    out = tools.Tools(conn, cfg).get_gen_runtime(7)
    assert len(out["runs"]) == 6
    assert "runs_summarised" not in out and "run_periods" not in out
    assert "fuel_mtd_gal" in out["fuel"]["mep"]


def test_past_the_budget_the_totals_cover_everything_and_it_says_so(year, cfg):
    out = tools.Tools(year, cfg).get_gen_runtime(365)
    assert len(out["runs"]) == tools.RECENT_RUNS_KEPT
    assert f"of {2 * 365}" in out["runs_summarised"]
    assert "month" in out["runs_summarised"]
    assert out["totals"]["mep"]["runs"] == 365
    assert out["fuel_window"]["total"]["runs"] == 2 * 365
    assert out["fuel_window"]["mep"]["hours"] == round(365 * 62.5 / 60, 1)
    assert out["charge_rates"] == tools.Tools(year, cfg).model.charge_rates()
    assert out["fuel"]["summary"]


def test_the_runs_kept_are_the_latest_in_order(year, cfg):
    rows = year.execute("SELECT start_ts FROM gen_runs ORDER BY start_ts DESC "
                        f"LIMIT {tools.RECENT_RUNS_KEPT}").fetchall()
    out = tools.Tools(year, cfg).get_gen_runtime(365)
    assert [r["start"] for r in out["runs"]] == \
        [history.stamp(r["start_ts"], cfg) for r in reversed(rows)]


@pytest.mark.parametrize("days,monthly", [(90, False), (91, True), (365, True)])
def test_period_totals_add_up_to_the_whole(year, cfg, days, monthly):
    out = tools.Tools(year, cfg).get_gen_runtime(days)
    periods = out["run_periods"]
    labels = [p["period"] for p in periods]
    assert all(len(p) == (7 if monthly else 10) for p in labels)
    assert labels == sorted(labels)
    for gen in history.GENS:
        hours = sum(p[f"{gen}_hours"] for p in periods)
        assert hours == pytest.approx(out["fuel_window"][gen]["hours"], abs=0.1 * len(periods))
    assert sum(p["gal"] for p in periods) == pytest.approx(
        out["fuel_window"]["total"]["fuel_gal"], abs=0.1 * len(periods))


def audit(conn, count=60):
    """The longest entries the audit log has: a storm-stop reason on every one."""
    why = ("POLICY 4 top-up fires: deficit of 6,508 Wh to sunrise above 52.0 V. Both "
           "generators are set to charge up to 57.0 V to meet the night's demand.")
    for i in range(count):
        history.record_action(
            conn, "set_gen_thresholds",
            {"mep_start": 53.8, "mep_stop": 57.0, "kub_start": 53.8, "kub_stop": 57.0,
             "reason": why}, 0,
            "standing down after an owner threshold change; 194 minutes left",
            53.62, 67.0, "refused", ts=1000 + i)


def test_recent_actions_at_its_cap_is_under_its_budget(conn, cfg):
    audit(conn)
    payload = tools.Tools(conn, cfg).call("get_recent_actions", {"n": 20})
    assert json.loads(payload)["count"] == 20
    assert len(payload) / ACTIONS_CHARS_PER_TOKEN < ACTIONS_TOKENS


def test_recent_actions_keeps_what_an_answer_needs(conn, cfg):
    history.record_action(conn, "set_gen_thresholds",
                          {"mep_start": 52.0, "mep_stop": 57.0, "kub_start": 52.0,
                           "kub_stop": 57.0, "reason": "POLICY 3: storm stop"},
                          1, "permitted", 54.3, 83.0, "allowed", ts=5000)
    entry = tools.Tools(conn, cfg).get_recent_actions(1)["actions"][0]
    assert entry["at"] == history.stamp(5000, cfg)
    assert entry["tool"] == "set_gen_thresholds"
    assert entry["result"] == "allowed"
    assert entry["reason"] == "permitted"
    assert entry["proposed_because"] == "POLICY 3: storm stop"
    assert entry["values"] == {"mep_start": 52.0, "mep_stop": 57.0,
                               "kub_start": 52.0, "kub_stop": 57.0}
    assert not {"args", "voltage", "soc", "allowed"} & set(entry)


def test_a_policy_miss_files_its_values_under_proposal(conn, cfg):
    history.record_action(conn, "policy_miss",
                          {"detail": "77% cloud", "name": "storm stop 57.0", "rule": 3,
                           "proposal": {"kub_start": 52.0, "kub_stop": 57.0,
                                        "mep_start": 52.0, "mep_stop": 57.0},
                           "recommend": "no recommendation returned"},
                          0, "POLICY 3 storm stop 57.0 fired", None, None, "missed", ts=5000)
    entry = tools.Tools(conn, cfg).get_recent_actions(1)["actions"][0]
    assert entry["values"]["mep_stop"] == 57.0
    assert entry["reason"] == "POLICY 3 storm stop 57.0 fired"
    assert "proposed_because" not in entry


def test_an_entry_with_arguments_that_are_not_json_still_reads(conn, cfg):
    conn.execute("INSERT INTO actions (ts, tool, args, allowed, reason, result) "
                 "VALUES (5000, 'x', 'not json', 0, 'why', 'refused')")
    conn.commit()
    assert tools.Tools(conn, cfg).get_recent_actions(1)["actions"][0]["reason"] == "why"


def test_the_schema_says_the_cap(conn, cfg):
    schema = next(s["function"] for s in tools.SCHEMAS
                  if s["function"]["name"] == "get_recent_actions")
    assert "1 to 20" in schema["parameters"]["properties"]["n"]["description"]
    audit(conn, count=30)
    assert tools.Tools(conn, cfg).get_recent_actions(500)["count"] == tools.RECENT_ACTIONS_MAX


# --- the safety net ----------------------------------------------------------

class Oversized(tools.Tools):
    size = tools.TOOL_RESULT_MAX_CHARS

    def get_weather(self):
        return {"data": "x" * self.size}


def test_the_safety_net_stops_an_oversized_result(conn, cfg, caplog):
    t = Oversized(conn, cfg)
    with caplog.at_level(logging.WARNING, logger=tools.__name__):
        payload = t.call("get_weather", {})
    assert len(payload) < 500
    out = json.loads(payload)
    assert out["error"].startswith(f"result too large ({Oversized.size + 12} chars)")
    assert "shorter span or fewer entries" in out["error"]
    assert out["hint"]
    assert any("not sent to the model" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_what_the_model_never_saw_cannot_source_a_figure(conn, cfg):
    """self.calls is what a figure in the reply is checked against."""
    t = Oversized(conn, cfg)
    t.call("get_weather", {})
    assert "data" not in t.calls[-1][2] and "error" in t.calls[-1][2]


def test_a_result_at_the_limit_goes_through(conn, cfg):
    t = Oversized(conn, cfg)
    t.size = tools.TOOL_RESULT_MAX_CHARS - 12         # {"data": "..."} is 12 more
    assert len(t.call("get_weather", {})) == tools.TOOL_RESULT_MAX_CHARS
    assert "error" not in t.calls[-1][2]


def test_the_usual_warning_still_comes_first(conn, cfg, caplog):
    t = Oversized(conn, cfg)
    t.size = tools.TOOL_RESULT_WARN_CHARS + 1
    with caplog.at_level(logging.WARNING, logger=tools.__name__):
        t.call("get_weather", {})
    assert any("a slow model" in r.getMessage() for r in caplog.records)
    assert not any("not sent" in r.getMessage() for r in caplog.records)
