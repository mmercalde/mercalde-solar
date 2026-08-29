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
    t = tools.Tools(conn, cfg, guard=guard)
    out = t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert out["applied"] is True
    assert guard.noted == {"mep_start": 55.5, "mep_stop": 57.0,
                           "kub_start": 52.0, "kub_stop": 54.5}


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
