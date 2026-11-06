"""Who started a generator, as pi5/app.py records it. Loaded from the real file,
the way test_ags_reasons.py does: app.py wants Flask and a Modbus socket."""

import ast
import os
import re

import pytest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "pi5", "app.py")
SOURCE = open(APP).read()


@pytest.fixture(scope="module")
def ns():
    tree = ast.parse(SOURCE)
    want = {"_set_start_origin", "_clear_ended_origin", "GEN_START_TIMEOUT_S"}
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in want)
            or (isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in want for t in n.targets))]
    out = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), APP, "exec"), out)
    return out


@pytest.fixture
def state():
    return {"mep803a_start_origin": None, "mep803a_start_origin_at": None,
            "mep803a_origin_seen_running": False}


def started(ns, state, origin="auto", at=1000):
    ns["_set_start_origin"](state, "mep803a", origin, at)


def poll(ns, state, action, at):
    ns["_clear_ended_origin"](state, "mep803a", action, at)
    return state["mep803a_start_origin"]


def test_it_records_who_and_when(ns, state):
    started(ns, state)
    assert state["mep803a_start_origin"] == "auto"
    assert state["mep803a_start_origin_at"] == 1000


def test_pressing_start_during_an_automatic_run_leaves_it_automatic(ns, state):
    started(ns, state, "auto", 1000)
    started(ns, state, "manual", 2000)
    assert state["mep803a_start_origin"] == "auto"
    assert state["mep803a_start_origin_at"] == 1000


def test_it_survives_the_whole_run_and_clears_when_it_stops(ns, state):
    started(ns, state)
    for action, at in ((10, 1005), (1, 1020), (3, 1040), (9, 1060), (9, 4000), (9, 9000)):
        assert poll(ns, state, action, at) == "auto"
    assert poll(ns, state, 10, 9060) is None
    assert state["mep803a_start_origin_at"] is None


def test_a_poll_before_the_ags_reacts_does_not_clear_it(ns, state):
    started(ns, state)
    assert poll(ns, state, 10, 1001) == "auto"


@pytest.mark.parametrize("action", [None, 255])
def test_a_failed_read_clears_nothing(ns, state, action):
    started(ns, state)
    assert poll(ns, state, 9, 1060) == "auto"
    assert poll(ns, state, action, 2000) == "auto"
    assert poll(ns, state, 9, 2060) == "auto"


def test_a_start_that_never_ran_is_forgotten_after_the_timeout(ns, state):
    started(ns, state)
    timeout = ns["GEN_START_TIMEOUT_S"]
    assert poll(ns, state, 10, 1000 + timeout) == "auto"
    assert poll(ns, state, 10, 1000 + timeout + 1) is None


def test_a_long_start_sequence_is_not_a_failed_start(ns, state):
    started(ns, state)
    assert poll(ns, state, 3, 1000 + 10 * ns["GEN_START_TIMEOUT_S"]) == "auto"


def test_the_next_run_gets_its_own_origin(ns, state):
    started(ns, state, "auto", 1000)
    poll(ns, state, 9, 1060)
    poll(ns, state, 10, 5000)
    started(ns, state, "manual", 6000)
    assert state["mep803a_start_origin"] == "manual"
    assert poll(ns, state, 10, 6001) == "manual", "seen_running must not carry over"


def test_nothing_recorded_stays_nothing(ns, state):
    assert poll(ns, state, 9, 1000) is None
    assert state["mep803a_origin_seen_running"] is False


# --- the callers: every start says who it is ------------------------------------

def test_the_voltage_loop_starts_as_auto_and_setgen_as_manual():
    calls = re.findall(r"target=start_generator, args=\(([^)]*)\)", SOURCE)
    assert sorted(calls) == sorted(['"mep803a", "auto"', '"kubota", "auto"', 'gen, "manual"'])
    for gen in ("MEP-803A", "Kubota"):
        i = SOURCE.index(f'AUTO: Starting {gen}')
        assert '"auto"' in SOURCE[i:i + 200]


def test_both_generators_are_published_in_data():
    for key in ("mep803aStartOrigin", "kubotaStartOrigin",
                "mep803aStartOriginAt", "kubotaStartOriginAt"):
        assert f'"{key}": None' in SOURCE
    assert 'new_data[f"{key}StartOrigin"] = auto_gen_state' in SOURCE


def test_a_failed_write_records_no_origin():
    """It is set inside `if success:`, beside the state a real request sets."""
    for gen in ("mep803a", "kubota"):
        requested = f'auto_gen_state["{gen}_start_requested"] = time.time()\n'
        i = SOURCE.index(requested)
        assert f'_set_start_origin(auto_gen_state, "{gen}", origin, time.time())' \
            in SOURCE[i:i + len(requested) + 100]
