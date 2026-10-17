"""Paying for the prompt cache before a person is waiting on it.

The first model call after a restart rebuilds the cache, about 50 s on the
KAMRUI's integrated GPU. A question that landed in that window cost 80 to
100 s and the dashboard gave up at 90, so the owner was told the agent was
not answering about an answer that had arrived.
"""

import threading

import pytest

import agent as agentmod
import prompts
import tools as toolsmod
from llm import LLMError


@pytest.fixture
def calls(a, monkeypatch):
    """Records every chat the warm-up makes."""
    seen = []

    def chat(messages, tools=None, **kw):
        seen.append({"messages": messages, "tools": tools, **kw})
        return {"role": "assistant", "content": "OK"}

    monkeypatch.setattr(a.llm, "chat", chat)
    return seen


def test_the_warm_up_sends_one_request(a, calls):
    assert a.warm_prompt_cache() is not None
    assert len(calls) == 1, "one throwaway question, not a loop"


def test_it_uses_the_prompt_the_ask_path_uses(a, calls):
    """A cache is a prefix. A prefix that differs anywhere is a cache that
    misses, so the system text has to be the one a real question sends."""
    a.warm_prompt_cache()
    system = calls[0]["messages"][0]
    assert system["role"] == "system"
    # The whole system message is static now - the time and plan ride in
    # the user turn - so it must match a real question's byte for byte.
    assert system["content"] == prompts.ask_prompt()
    assert "NOW\n" not in system["content"]
    assert "MISSION" in system["content"] and "POLICY" in system["content"]


def test_it_carries_the_same_tool_schemas(a, calls):
    """The chat template folds the tool definitions into the prompt, so a
    warm-up without them would cache a prefix that diverges the moment a real
    turn adds them."""
    a.warm_prompt_cache()
    assert calls[0]["tools"] is toolsmod.SCHEMAS


def test_it_asks_for_almost_no_output(a, calls):
    """It is sent for the prefix it carries, not for the reply."""
    a.warm_prompt_cache()
    assert calls[0]["max_tokens"] <= 8
    assert calls[0]["temperature"] == 0.0
    assert len(calls[0]["messages"]) == 2
    assert calls[0]["messages"][1]["role"] == "user"


def test_it_logs_how_long_the_cache_took(a, calls, caplog):
    with caplog.at_level("INFO", logger="agent"):
        a.warm_prompt_cache()
    line = [r.getMessage() for r in caplog.records
            if "prompt cache warmed" in r.getMessage()]
    assert len(line) == 1 and " s" in line[0]


# --- and it must not stop the agent starting -------------------------------------

def test_an_unreachable_server_is_survived_quietly(a, monkeypatch, caplog):
    """The agent starts, samples, plans and refuses to answer without the
    model. A warm-up that cannot connect must not change that."""
    tries = []

    def down(*args, **kw):
        tries.append(1)
        raise LLMError("llama-server unreachable at http://127.0.0.1:8080")

    monkeypatch.setattr(a.llm, "chat", down)
    monkeypatch.setattr(agentmod, "WARMUP_RETRY_SECONDS", 0)
    with caplog.at_level("INFO", logger="agent"):
        assert a.warm_prompt_cache() is None, "no exception, no timing"
    assert len(tries) == agentmod.WARMUP_ATTEMPTS, "tried, then gave up"
    assert any("prompt cache not warmed" in r.getMessage()
               for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_a_server_that_comes_up_late_is_caught(a, monkeypatch):
    """llama-server may still be loading its weights when the agent starts."""
    tries = []

    def slow(*args, **kw):
        tries.append(1)
        if len(tries) < 2:
            raise LLMError("still loading")
        return {"role": "assistant", "content": "OK"}

    monkeypatch.setattr(a.llm, "chat", slow)
    monkeypatch.setattr(agentmod, "WARMUP_RETRY_SECONDS", 0)
    assert a.warm_prompt_cache() is not None
    assert len(tries) == 2


def test_any_other_failure_is_swallowed_too(a, monkeypatch, caplog):
    monkeypatch.setattr(a.llm, "chat",
                        lambda *ar, **k: (_ for _ in ()).throw(ValueError("x")))
    assert a.warm_prompt_cache() is None


def test_a_shutdown_stops_it_waiting(a, monkeypatch):
    """The waits are on stop_event, so a restart during a retry is not held
    up by one."""
    monkeypatch.setattr(a.llm, "chat",
                        lambda *ar, **k: (_ for _ in ()).throw(LLMError("no")))
    monkeypatch.setattr(agentmod, "WARMUP_RETRY_SECONDS", 30)
    a.stop_event.set()
    assert a.warm_prompt_cache() is None


def test_startup_runs_it_once_and_does_not_wait_on_it(a, monkeypatch):
    """Started as a daemon thread: the model may be absent, and startup has
    a dashboard to answer and a tick to run."""
    started = []
    real_thread = threading.Thread

    def spy(target=None, name=None, **kw):
        if name == "warmup":
            started.append(target)
        return real_thread(target=lambda: None, name=name, **kw)

    monkeypatch.setattr(agentmod.threading, "Thread", spy)
    monkeypatch.setattr(agentmod.ask_server, "serve", lambda *ar, **k: None)
    monkeypatch.setattr(a, "tick", lambda *ar, **k: None)

    class Sched:
        def add_job(self, *ar, **k): pass
        def start(self): pass
        def shutdown(self, **k): pass

    monkeypatch.setattr(agentmod, "BackgroundScheduler", lambda **k: Sched())
    a.stop_event.set()
    a.run()
    assert started == [a.warm_prompt_cache], "once, in its own thread"


# --- and the cache has to reach most of the prompt to be worth warming ---------

def test_nothing_changes_in_the_system_prompt_between_questions():
    """The server caches by prefix, and the chat template appends the tool
    schemas to the system message. So the system message has to be the same
    bytes every time, or the tools fall after the break and are re-prefilled
    on every question (2,845 tokens, 75 s, measured 2026-10-16)."""
    first = prompts.ask_prompt("en", now_text="2026-09-02 7:00 pm")
    later = prompts.ask_prompt("es", now_text="2026-09-02 8:00 pm", context="x")
    assert first == later == prompts.ask_prompt()
    assert "NOW\nIt is" not in first and "Answer in English." not in first


def test_the_clock_line_rides_in_the_user_turn():
    u = prompts.ask_user("voltage?", "en", now_text="2026-08-30 1:26 pm")
    assert u.startswith("NOW\nIt is 2026-08-30 1:26 pm.")
    assert "Answer in English." in u
    assert u.rstrip().endswith("QUESTION\nvoltage?")
    assert "NOW" not in prompts.ask_user("voltage?", "en")


def test_a_spanish_question_still_gets_its_instruction():
    u = prompts.ask_user("voltaje?", "es", now_text="2026-08-30 1:26 pm")
    assert "Responde en espanol." in u and "It is 2026-08-30" in u


def test_the_plan_context_rides_in_the_user_turn_before_the_question():
    u = prompts.ask_user("q", now_text="t", context="PLAN FACTS")
    assert u.index("NOW") < u.index("PLAN FACTS") < u.index("QUESTION\nq")
