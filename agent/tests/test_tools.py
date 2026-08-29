"""Tool behaviour, above all the shape of the one write the agent may make."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

import tools


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


LIVE_CONFIG = {"config": {
    "mep803a": {"startVoltage": 55.5, "stopVoltage": 57.0, "chargeRate": 100,
                "maxRuntime": 120, "cooldown": 5},
    "kubota": {"startVoltage": 52.0, "stopVoltage": 54.5, "chargeRate": 70,
               "maxRuntime": 120, "cooldown": 5},
    "autoGenEnabled": True}}


class RecordingSession:
    """Captures the request instead of making it."""

    def __init__(self, payload=LIVE_CONFIG):
        self.payload = payload
        self.url = None
        self.params = None
        self.headers = None
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        self.url = url
        self.params = params
        self.headers = headers
        return FakeResponse(self.payload)


@pytest.fixture
def session():
    return RecordingSession()


# --- the write --------------------------------------------------------------

def test_write_hits_the_config_endpoint(cfg, session):
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5, session=session)
    assert session.calls == 1
    assert session.url == cfg["dashboard_url"] + "/config"
    assert urlparse(session.url).path == "/config"


def test_write_sends_the_four_threshold_params(cfg, session):
    tools.apply_thresholds(cfg, 52.0, 54.5, 55.5, 57.0, session=session)
    assert session.params["mep.startVoltage"] == "52.0"
    assert session.params["mep.stopVoltage"] == "54.5"
    assert session.params["kub.startVoltage"] == "55.5"
    assert session.params["kub.stopVoltage"] == "57.0"


def test_write_sends_nothing_but_the_thresholds_and_the_marker(cfg, session):
    """SPEC section 4: never chargeRate, maxRuntime or cooldown. The single
    extra parameter is the section 9 access-log marker, which app.py ignores."""
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5, session=session)
    assert set(session.params) == set(tools.THRESHOLD_PARAMS) | {"src"}
    assert session.params["src"] == "agent"


def test_write_never_sends_a_forbidden_parameter(cfg, session):
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5, session=session)
    for bad in tools.FORBIDDEN_PARAMS:
        assert bad not in session.params
    for bad in ("chargeRate", "maxRuntime", "cooldown", "autoGenEnabled"):
        assert not any(bad in k for k in session.params)


def test_write_identifies_itself_for_the_watchdog(cfg, session):
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5, session=session)
    assert session.headers["X-Agent"] == "solar-agent"


def test_write_formats_one_decimal(cfg, session):
    tools.apply_thresholds(cfg, 52, 54.53, 52.0, 57, session=session)
    assert session.params["mep.startVoltage"] == "52.0"
    assert session.params["mep.stopVoltage"] == "54.5"
    assert session.params["kub.stopVoltage"] == "57.0"


def test_write_returns_the_live_config(cfg, session):
    live = tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5, session=session)
    assert tools.thresholds_from_config(live) == {
        "mep_start": 55.5, "mep_stop": 57.0, "kub_start": 52.0, "kub_stop": 54.5}


def test_the_request_line_carries_only_expected_parameters(cfg, session):
    """What the Pi5 access log will actually show."""
    tools.apply_thresholds(cfg, 52.0, 54.5, 55.5, 57.0, session=session)
    import requests
    query = requests.models.PreparedRequest()
    query.prepare_url(session.url, session.params)
    parsed = parse_qs(urlparse(query.url).query)
    assert set(parsed) == {"mep.startVoltage", "mep.stopVoltage",
                           "kub.startVoltage", "kub.stopVoltage", "src"}


# --- the guard boundary -----------------------------------------------------

class StubGuard:
    def __init__(self, allowed=True, reason="ok"):
        self.allowed, self.reason = allowed, reason
        self.checked = None
        self.noted = None

    def check(self, **kwargs):
        self.checked = kwargs
        return self.allowed, self.reason

    def note_write(self, applied):
        self.noted = applied


def test_write_tool_refuses_without_a_guard(conn, cfg):
    t = tools.Tools(conn, cfg, guard=None)
    out = t.set_gen_thresholds(52.0, 54.5, 52.0, 54.5, "test")
    assert out["applied"] is False and "guard" in out["reason"]


def test_write_tool_reports_a_guard_refusal_to_the_model(conn, cfg):
    guard = StubGuard(allowed=False, reason="learning phase: no writes yet")
    t = tools.Tools(conn, cfg, guard=guard)
    out = t.set_gen_thresholds(52.0, 54.5, 52.0, 54.5, "test")
    assert out["applied"] is False
    assert out["refused_by"] == "guard"
    assert out["reason"] == "learning phase: no writes yet"
    assert guard.checked["mep_start"] == 52.0


def test_dry_run_never_writes(conn, cfg):
    guard = StubGuard(allowed=True)
    t = tools.Tools(conn, cfg, guard=guard, dry_run=True)
    out = t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert out["applied"] is False and out["dry_run"] is True
    assert out["would_set"]["mep_start"] == 55.5
    assert guard.noted is None, "a dry run must not record a write"


def test_dry_run_does_not_send_telegram(conn, cfg):
    t = tools.Tools(conn, cfg, dry_run=True)
    out = t.send_telegram("hello")
    assert out["sent"] is False and out["dry_run"] is True


def test_write_tool_notes_the_applied_values(conn, cfg, monkeypatch):
    guard = StubGuard(allowed=True)
    monkeypatch.setattr(tools, "apply_thresholds",
                        lambda *a, **k: LIVE_CONFIG["config"])
    monkeypatch.setattr(tools.telegram, "send", lambda *a, **k: True)
    t = tools.Tools(conn, cfg, guard=guard)
    out = t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert out["applied"] is True
    assert guard.noted == {"mep_start": 55.5, "mep_stop": 57.0,
                           "kub_start": 52.0, "kub_stop": 54.5}


# --- every executed write tells the owner -----------------------------------

@pytest.fixture
def sent(monkeypatch):
    """Captures what would go to Telegram."""
    out = []
    monkeypatch.setattr(tools, "apply_thresholds",
                        lambda *a, **k: LIVE_CONFIG["config"])
    monkeypatch.setattr(tools.telegram, "send",
                        lambda cfg, text, **k: out.append(text) or True)
    return out


def test_an_executed_write_sends_the_values_and_the_reason(conn, cfg, sent):
    """The 04:10 write sent nothing; the model was trusted to do it."""
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=True))
    out = t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "POLICY 4 solo top-up")
    assert out["notified"] is True
    assert len(sent) == 1
    assert "MEP 55.5 / 57.0, Kubota 52.0 / 54.5" in sent[0]
    assert "POLICY 4 solo top-up" in sent[0]


# --- the message says who did it, and what happens next ---------------------

BEFORE = {"mep_start": 52.0, "mep_stop": 56.0,
          "kub_start": 52.0, "kub_stop": 56.0}
AFTER = {"mep_start": 55.0, "mep_stop": 57.0,
         "kub_start": 52.0, "kub_stop": 56.0}


def test_the_message_names_the_agent_the_change_and_the_effect():
    """Four numbers alone read like the Pi5's own low-voltage auto-start."""
    m = tools.write_message(AFTER, "solo top-up", before=BEFORE, voltage=54.2,
                            default_start=52.0)
    assert "Agent raised MEP start 52.0 → 55.0" in m
    assert "solo top-up" in m
    assert "MEP will start now" in m
    assert "not the Pi5's 52.0 V auto-start" in m
    assert "Now MEP 55.0 / 57.0, Kubota 52.0 / 56.0; pack 54.2 V" in m


def test_a_start_above_the_pack_says_the_generator_runs_now():
    _, effects = tools.describe_write(BEFORE, AFTER, voltage=54.2)
    assert effects == ["MEP will start now"]


def test_a_start_below_the_pack_says_when_it_will_run():
    _, effects = tools.describe_write(BEFORE, AFTER, voltage=56.4)
    assert effects == ["MEP will start when the pack falls to 55.0"]


def test_a_stop_only_change_promises_no_generator():
    """Raising a stop does not start anything, so nothing is claimed."""
    after = dict(BEFORE, mep_stop=57.0, kub_stop=57.0)
    changes, effects = tools.describe_write(BEFORE, after, voltage=54.2)
    assert effects == []
    assert changes == ["raised MEP stop 56.0 → 57.0", "raised Kubota stop 56.0 → 57.0"]


def test_a_lowered_threshold_says_lowered():
    after = dict(BEFORE, mep_stop=54.5)
    changes, _ = tools.describe_write(BEFORE, after, voltage=54.2)
    assert changes == ["lowered MEP stop 56.0 → 54.5"]


def test_an_unchanged_value_is_not_reported_as_a_change():
    changes, _ = tools.describe_write(BEFORE, dict(BEFORE, mep_start=55.0), 54.2)
    assert changes == ["raised MEP start 52.0 → 55.0"]


def test_the_write_reads_the_before_values_off_the_guard(conn, cfg, sent):
    """The guard already fetched them to judge the write; no second call."""
    guard = StubGuard(allowed=True)
    guard.last_seen = {"thresholds": {"mep_start": 55.5, "mep_stop": 57.0,
                                      "kub_start": 52.0, "kub_stop": 54.5},
                       "voltage": 54.2}
    t = tools.Tools(conn, cfg, guard=guard)
    t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert "Agent set the thresholds" in sent[0], "nothing moved"
    assert "pack 54.2 V" in sent[0]


def test_the_message_quotes_what_the_dashboard_read_back(conn, cfg, sent):
    """A Pi5 clamp must reach the owner as the number that is in force."""
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=True))
    t.set_gen_thresholds(56.0, 58.0, 52.0, 54.5, "clamped somewhere")
    assert "MEP 55.5 / 57.0" in sent[0], "the /config response, not the request"


def test_a_refused_write_tells_nobody(conn, cfg, sent):
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=False, reason="rate limit"))
    t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert sent == []


def test_a_dry_run_tells_nobody(conn, cfg, sent):
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=True), dry_run=True)
    t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert sent == []


def test_a_telegram_failure_does_not_undo_the_write(conn, cfg, monkeypatch):
    monkeypatch.setattr(tools, "apply_thresholds",
                        lambda *a, **k: LIVE_CONFIG["config"])
    monkeypatch.setattr(tools.telegram, "send", lambda *a, **k: False)
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=True))
    out = t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert out["applied"] is True and out["notified"] is False


# --- dispatch ---------------------------------------------------------------

def test_schemas_cover_every_tool_and_nothing_else():
    named = {s["function"]["name"] for s in tools.SCHEMAS}
    assert named == tools.READ_TOOLS | tools.WRITE_TOOLS
    assert len(named) == 8


def test_every_schema_is_a_valid_openai_function():
    for s in tools.SCHEMAS:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["description"] and fn["parameters"]["type"] == "object"
        for req in fn["parameters"].get("required", []):
            assert req in fn["parameters"]["properties"]


def test_unknown_tool_is_reported_not_raised(conn, cfg):
    t = tools.Tools(conn, cfg)
    assert "no such tool" in json.loads(t.call("drop_tables", {}))["error"]


def test_private_methods_are_not_callable_as_tools(conn, cfg):
    t = tools.Tools(conn, cfg)
    assert "no such tool" in json.loads(t.call("call", {}))["error"]
    assert "no such tool" in json.loads(t.call("apply_thresholds", {}))["error"]


def test_bad_arguments_come_back_as_an_error(conn, cfg):
    t = tools.Tools(conn, cfg)
    assert "bad arguments" in json.loads(t.call("get_history", {"nope": 1}))["error"]


def test_call_returns_json_and_records_the_call(conn, cfg):
    t = tools.Tools(conn, cfg)
    out = json.loads(t.call("get_load_forecast", {"hours": 6}))
    assert out["hours"] == 6
    assert t.calls[0][0] == "get_load_forecast"


def test_a_failing_tool_does_not_kill_the_tick(conn, cfg, monkeypatch):
    monkeypatch.setattr(tools.history, "fetch_data",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    t = tools.Tools(conn, cfg)
    assert "boom" in json.loads(t.call("get_status", {}))["error"]


def test_the_write_message_escapes_the_reason(conn, cfg, sent):
    """POLICY details contain "<", which is what silenced the first live days."""
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=True))
    t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up: peak 56.0 < 57.0")
    assert "peak 56.0 &lt; 57.0" in sent[0]
    assert "peak 56.0 < 57.0" not in sent[0]


def test_a_model_message_is_escaped_too(conn, cfg, monkeypatch):
    out = []
    monkeypatch.setattr(tools.telegram, "send",
                        lambda cfg, text, **k: out.append(text) or True)
    tools.Tools(conn, cfg).send_telegram("load < 2 kW & falling")
    assert out == ["load &lt; 2 kW &amp; falling"]
