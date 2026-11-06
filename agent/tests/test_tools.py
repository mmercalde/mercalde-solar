"""Tool behaviour, above all the shape of the one write the agent may make."""

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

import history
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


def OK(mep_start, mep_stop, kub_start, kub_stop):
    """A guard approval for exactly these four values."""
    return {"allowed": True,
            "values": {"mep_start": mep_start, "mep_stop": mep_stop,
                       "kub_start": kub_start, "kub_stop": kub_stop}}


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
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5,
                           approval=OK(mep_start=52.0, mep_stop=54.5, kub_start=52.0, kub_stop=54.5), session=session)
    assert session.calls == 1
    assert session.url == cfg["dashboard_url"] + "/config"
    assert urlparse(session.url).path == "/config"


def test_write_sends_the_four_threshold_params(cfg, session):
    tools.apply_thresholds(cfg, 52.0, 54.5, 55.5, 57.0,
                           approval=OK(mep_start=52.0, mep_stop=54.5, kub_start=55.5, kub_stop=57.0), session=session)
    assert session.params["mep.startVoltage"] == "52.0"
    assert session.params["mep.stopVoltage"] == "54.5"
    assert session.params["kub.startVoltage"] == "55.5"
    assert session.params["kub.stopVoltage"] == "57.0"


def test_write_sends_nothing_but_the_thresholds_and_the_marker(cfg, session):
    """SPEC section 4: never chargeRate, maxRuntime or cooldown. The single
    extra parameter is the section 9 access-log marker, which app.py ignores."""
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5,
                           approval=OK(mep_start=52.0, mep_stop=54.5, kub_start=52.0, kub_stop=54.5), session=session)
    assert set(session.params) == set(tools.THRESHOLD_PARAMS) | {"src"}
    assert session.params["src"] == "agent"


def test_write_never_sends_a_forbidden_parameter(cfg, session):
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5,
                           approval=OK(mep_start=52.0, mep_stop=54.5, kub_start=52.0, kub_stop=54.5), session=session)
    for bad in tools.FORBIDDEN_PARAMS:
        assert bad not in session.params
    for bad in ("chargeRate", "maxRuntime", "cooldown", "autoGenEnabled"):
        assert not any(bad in k for k in session.params)


def test_write_identifies_itself_for_the_watchdog(cfg, session):
    tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5,
                           approval=OK(mep_start=52.0, mep_stop=54.5, kub_start=52.0, kub_stop=54.5), session=session)
    assert session.headers["X-Agent"] == "solar-agent"


def test_write_formats_one_decimal(cfg, session):
    tools.apply_thresholds(cfg, 52, 54.53, 52.0, 57,
                           approval=OK(mep_start=52, mep_stop=54.53, kub_start=52.0, kub_stop=57), session=session)
    assert session.params["mep.startVoltage"] == "52.0"
    assert session.params["mep.stopVoltage"] == "54.5"
    assert session.params["kub.stopVoltage"] == "57.0"


def test_write_returns_the_live_config(cfg, session):
    live = tools.apply_thresholds(cfg, 52.0, 54.5, 52.0, 54.5,
                                  approval=OK(mep_start=52.0, mep_stop=54.5, kub_start=52.0, kub_stop=54.5), session=session)
    assert tools.thresholds_from_config(live) == {
        "mep_start": 55.5, "mep_stop": 57.0, "kub_start": 52.0, "kub_stop": 54.5}


def test_the_request_line_carries_only_expected_parameters(cfg, session):
    """What the Pi5 access log will actually show."""
    tools.apply_thresholds(cfg, 52.0, 54.5, 55.5, 57.0,
                           approval=OK(mep_start=52.0, mep_stop=54.5, kub_start=55.5, kub_stop=57.0), session=session)
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
        self.last_check = None

    def check(self, **kwargs):
        self.checked = kwargs
        return self.allowed, self.reason

    def note_write(self, applied, now=None, housekeeping=False):
        self.noted = applied

    def approval(self):
        vals = (self.last_check or {}).get("values") or self.checked
        return {"allowed": True,
                "values": {k: vals[k] for k in ("mep_start", "mep_stop",
                                                "kub_start", "kub_stop")}}


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


def test_the_message_carries_the_numbers_the_rule_fired_on(cfg):
    """The plan record has always had them. The Telegram had four voltages
    and a sentence the model wrote, so the two could disagree about one
    decision, and on 2026-08-30 they did."""
    import policy
    from stubs import StubModel
    import test_topup
    facts = {"now": test_topup.ts_at(cfg, "2026-08-30", 20, 24),
             "voltage": 53.6, "soc": 87.0,
             "sunrise_ts": test_topup.ts_at(cfg, "2026-08-31", 6, 22),
             "sunset_ts": test_topup.ts_at(cfg, "2026-08-30", 19, 14),
             "tomorrow_cloud": 56, "remaining_solar_wh": 0,
             "data": {"autoGenEnabled": True},
             "deficit": {"deficit_wh": 6463, "needed_wh": 15220,
                         "available_wh": 8757, "capacity_wh": 100000,
                         "available_source": "learned Wh-vs-V, 10 nights",
                         "soc_now_display": 87, "floor_v": 52.0},
             "baseline": dict(BEFORE), "thresholds": dict(BEFORE),
             "run_window_h": {"mep": 2.0, "kubota": 2.0},
             "topup": {"gens": {"mep": {"state": "idle"},
                                "kubota": {"state": "idle"}}}}
    rules = [policy.solo_top_up(cfg, facts, StubModel())]
    line = policy.numbers_line(rules)
    assert "deficit 6,463 Wh" in line
    assert "+15% = 7,432 Wh" in line
    assert "target 55.8 V" in line
    assert "MEP band (deficit ≤ 15,000 Wh)" in line
    assert "kW into the pack" in line
    assert "min of running" in line

    m = tools.write_message(AFTER, "solo top-up", before=BEFORE, voltage=53.6,
                            default_start=52.0, numbers=line)
    assert "deficit 6,463 Wh" in m and "target 55.8 V" in m


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
    assert len(named) == 16


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


def test_a_trimmed_write_applies_the_part_that_stands(conn, cfg, sent):
    """The guard kept the start change and dropped the stop change."""
    guard = StubGuard(allowed=True)
    guard.last_check = {"values": {"mep_start": 52.0, "mep_stop": 56.0,
                                   "kub_start": 52.0, "kub_stop": 56.0},
                        "requested": {"mep_start": 52.0, "mep_stop": 56.0,
                                      "kub_start": 52.0, "kub_stop": 54.5},
                        "refused": ["kubota is running; its stop cannot be "
                                    "lowered from 56.0 to 54.5 mid-run"]}
    t = tools.Tools(conn, cfg, guard=guard)
    out = t.set_gen_thresholds(52.0, 56.0, 52.0, 54.5, "back to default")
    assert out["applied"] is True
    assert out["requested"]["kub_stop"] == 54.5
    assert len(out["refused_parts"]) == 1
    assert "Not done: kubota is running" in sent[0]


def test_an_untrimmed_write_says_nothing_about_refusals(conn, cfg, sent):
    guard = StubGuard(allowed=True)
    guard.last_check = {"values": {"mep_start": 55.5, "mep_stop": 57.0,
                                   "kub_start": 52.0, "kub_stop": 54.5},
                        "refused": []}
    t = tools.Tools(conn, cfg, guard=guard)
    out = t.set_gen_thresholds(55.5, 57.0, 52.0, 54.5, "solo top-up")
    assert out["refused_parts"] == [] and "Not done" not in sent[0]


# --- the model may not narrate a write --------------------------------------

def test_a_refused_write_replaces_the_models_message(conn, cfg, monkeypatch):
    """At 12:17 am it said "Adjusted generator thresholds to 52.0/54.5" after
    the guard had refused exactly that. Nothing had been adjusted."""
    out = []
    monkeypatch.setattr(tools.telegram, "send",
                        lambda cfg, text, **k: out.append(text) or True)
    guard = StubGuard(allowed=False,
                      reason="kubota is running; its stop cannot be lowered")
    t = tools.Tools(conn, cfg, guard=guard)
    t.set_gen_thresholds(52.0, 54.5, 52.0, 54.5, "back to default")
    t.send_telegram("Adjusted generator thresholds to 52.0/54.5.")
    assert len(out) == 1
    assert "Adjusted" not in out[0]
    assert "proposed MEP 52.0/54.5, Kubota 52.0/54.5" in out[0]
    assert "refused: kubota is running" in out[0]


def test_a_tick_with_no_refusal_sends_what_the_model_wrote(conn, cfg,
                                                            monkeypatch):
    out = []
    monkeypatch.setattr(tools.telegram, "send",
                        lambda cfg, text, **k: out.append(text) or True)
    tools.Tools(conn, cfg).send_telegram("The pack is healthy tonight.")
    assert out == ["The pack is healthy tonight."]


def test_every_refusal_of_the_tick_is_reported(conn, cfg, monkeypatch):
    out = []
    monkeypatch.setattr(tools.telegram, "send",
                        lambda cfg, text, **k: out.append(text) or True)
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=False, reason="rate limit"))
    t.set_gen_thresholds(52.0, 54.5, 52.0, 54.5, "one")
    t.set_gen_thresholds(55.0, 57.0, 52.0, 56.0, "two")
    t.send_telegram("anything at all")
    assert out[0].count("proposed MEP") == 2
    assert "55.0/57.0" in out[0]


def test_a_refusal_message_is_still_escaped(conn, cfg, monkeypatch):
    out = []
    monkeypatch.setattr(tools.telegram, "send",
                        lambda cfg, text, **k: out.append(text) or True)
    t = tools.Tools(conn, cfg,
                    guard=StubGuard(allowed=False, reason="peak 56.0 < 57.0"))
    t.set_gen_thresholds(52.0, 54.5, 52.0, 54.5, "x")
    t.send_telegram("whatever")
    assert "peak 56.0 &lt; 57.0" in out[0]


def test_a_dry_run_shows_the_refusal_rather_than_the_narration(conn, cfg):
    t = tools.Tools(conn, cfg, guard=StubGuard(allowed=False, reason="no"),
                    dry_run=True)
    t.set_gen_thresholds(52.0, 54.5, 52.0, 54.5, "x")
    out = t.send_telegram("Adjusted the thresholds.")
    assert "proposed MEP" in out["text"] and "Adjusted" not in out["text"]


# --- no write reaches the dashboard without the guard ------------------------

def test_a_write_without_an_approval_is_refused(cfg, session):
    """The heartbeat reached /config without passing check(). Nothing can."""
    with pytest.raises(tools.WriteNotApproved):
        tools.apply_thresholds(cfg, 52.0, 56.0, 52.0, 56.0, approval=None,
                               session=session)
    assert session.calls == 0


def test_an_approval_for_other_values_does_not_authorise_this_write(cfg, session):
    with pytest.raises(tools.WriteNotApproved) as e:
        tools.apply_thresholds(cfg, 53.3, 57.0, 53.3, 57.0,
                               approval=OK(52.0, 56.0, 52.0, 56.0),
                               session=session)
    assert "has not approved" in str(e.value)
    assert session.calls == 0


def test_a_refused_check_leaves_no_approval_to_write_with(conn, cfg, session):
    guard = StubGuard(allowed=False, reason="daylight hold")
    guard.approval = lambda: None
    t = tools.Tools(conn, cfg, guard=guard)
    out = t.set_gen_thresholds(53.3, 57.0, 53.3, 57.0, "re-asserting intent")
    assert out["applied"] is False and session.calls == 0


# --- the five tools that describe the system itself --------------------------

LIVE_DATA = {
    "batteryVoltage": 54.45, "battSocBM": 87, "battAhRemaining": 1568,
    "battMinToDischarge": 614, "battCurrent": 40.2, "battPower": 2192,
    "battMonitorOnline": True,
    "mppt80PVPower": 1763, "mppt80PVVoltage": 192.52, "mppt80PVCurrent": 9.16,
    "mppt80ChargeStatus": 769,
    "southArrayPVPower": 1399, "southArrayPVVoltage": 78.21,
    "southArrayPVCurrent": 17.9, "southArrayChargeStatus": 769,
    "westArrayPVPower": 2042, "westArrayPVVoltage": 74.53,
    "westArrayPVCurrent": 27.41, "westArrayChargeStatus": 769,
    "mep803aAction": 10, "kubotaAction": 10,
}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(tools.history, "fetch_data", lambda *a, **k: dict(LIVE_DATA))


def test_the_specs_are_the_manifest(conn, cfg):
    import system
    assert tools.Tools(conn, cfg).get_system_specs() == system.load()


def test_the_mppt_detail_is_per_controller(conn, cfg, live):
    out = tools.Tools(conn, cfg).get_mppt_detail()
    by_name = {c["name"]: c for c in out["controllers"]}
    assert set(by_name) == {"mppt80", "south", "west"}
    assert by_name["west"]["watts"] == 2042
    assert by_name["west"]["pv_volts"] == 74.53
    assert by_name["west"]["pv_amps"] == 27.41
    assert by_name["west"]["slave"] == 30
    assert by_name["mppt80"]["controller"] == "Schneider MPPT 80 600"
    assert out["total_w"] == 1763 + 1399 + 2042


def test_the_mppt_detail_carries_the_energy_counters(conn, cfg, live):
    for device, kwh in (("west", 6.94), ("south", 6.23), ("mppt80", 6.78)):
        conn.execute("INSERT INTO counters (ts, device, counter, period, kwh) "
                     "VALUES (1000, ?, 'energy_from_pv', 'today', ?)",
                     (device, kwh))
    conn.commit()
    out = tools.Tools(conn, cfg).get_mppt_detail()
    by_name = {c["name"]: c for c in out["controllers"]}
    assert by_name["west"]["kwh_today"] == 6.94
    assert out["total_kwh_today"] == 19.95


def test_a_controller_with_no_counter_row_says_none(conn, cfg, live):
    out = tools.Tools(conn, cfg).get_mppt_detail()
    assert all(c["kwh_today"] is None for c in out["controllers"])


def test_the_battery_detail_carries_the_monitor(conn, cfg, live):
    out = tools.Tools(conn, cfg).get_battery_detail()
    assert out["ah_remaining"] == 1568
    assert out["minutes_to_discharge"] == 614
    assert out["net_current_a"] == 40.2
    assert out["monitor"]["slave"] == 191 and out["monitor"]["online"] is True
    assert out["limits"] == {"floor_v": 52.0, "ceiling_v": 57.0, "full_v": 61.0}


def test_the_battery_detail_says_there_is_no_temperature(conn, cfg, live):
    """It is not published over Modbus, so the tool says so rather than
    leaving the model to wonder whether it forgot to look."""
    out = tools.Tools(conn, cfg).get_battery_detail()
    assert out["temperature_c"] is None
    assert "publishes no temperature" in out["temperature_note"]


def test_the_battery_detail_separates_nominal_from_learned(conn, cfg, live):
    out = tools.Tools(conn, cfg).get_battery_detail()
    assert out["nominal"]["capacity_kwh"] == 96
    assert out["learned"]["capacity_wh"] is None, "nothing learned from an empty db"


def test_the_guard_state_reports_what_it_will_permit(conn, cfg, monkeypatch,
                                                     tmp_path):
    import guard as guardmod
    import sun
    monkeypatch.setattr(sun, "times", lambda *a, **k: (1000, 2000))
    g = guardmod.Guard(conn, cfg, state_path=str(tmp_path / "s.json"))
    out = tools.Tools(conn, cfg, guard=g).get_guard_state()
    assert out["hard_limits"]["start_floor_v"] == 52.0
    assert out["hard_limits"]["stop_ceiling_v"] == 57.0
    assert out["baseline"] == {"mep_start": 52.0, "mep_stop": 56.0,
                               "kub_start": 52.0, "kub_stop": 56.0}
    assert out["owner_baseline"] is None
    assert out["rate_limit"]["in_force"] is False
    assert out["daylight_hold"]["in_daylight"] is False
    assert out["learning_gate"]["open"] is False


def test_the_guard_state_shows_the_rate_limit_running(conn, cfg, monkeypatch,
                                                      tmp_path):
    import guard as guardmod
    import sun
    monkeypatch.setattr(sun, "times", lambda *a, **k: (1000, 2000))
    g = guardmod.Guard(conn, cfg, state_path=str(tmp_path / "s.json"))
    g.note_write({"mep_start": 52.0, "mep_stop": 56.0,
                  "kub_start": 52.0, "kub_stop": 56.0}, now=int(time.time()) - 600)
    out = tools.Tools(conn, cfg, guard=g).get_guard_state()
    assert out["rate_limit"]["in_force"] is True
    assert out["rate_limit"]["minutes_since"] == 10


def test_the_guard_state_needs_a_guard(conn, cfg):
    assert "error" in tools.Tools(conn, cfg).get_guard_state()


def test_recent_actions_come_from_the_audit_trail(conn, cfg):
    for i in range(6):
        history.record_action(conn, "set_gen_thresholds", {"mep_start": 52.0},
                              i % 2, f"reason {i}", 54.2, 71,
                              "allowed" if i % 2 else "refused", ts=1000 + i)
    out = tools.Tools(conn, cfg).get_recent_actions(3)
    assert out["count"] == 3
    assert [a["reason"] for a in out["actions"]] == \
        ["reason 5", "reason 4", "reason 3"]
    assert out["actions"][0]["result"] == "allowed"
    assert out["actions"][1]["result"] == "refused"


def test_recent_actions_is_bounded(conn, cfg):
    for i in range(80):
        history.record_action(conn, "x", {}, 1, "r", None, None, "allowed",
                              ts=1000 + i)
    assert tools.Tools(conn, cfg).get_recent_actions(500)["count"] == 20
    assert tools.Tools(conn, cfg).get_recent_actions(0)["count"] == 1


# --- Named windows -----------------------------------------------------------
#
# On 2026-09-02 "load energy used overnight?" was answered with
# get_history(hours=24): 25,273 Wh, the day's correct load, called a night's.
# The tool had no way to say "since sunset", so the model passed the nearest
# number it could. These tests are about the words being arguments.

import sun as sunmod  # noqa: E402


def a_day_and_a_night(conn, cfg, day="2026-09-02"):
    """A day whose sunlit hours draw twice what its dark hours do.

    Load lands 500 Wh in every hour from 07:00 to 18:00 and 250 Wh in each of
    the rest, so a night and a day are told apart by the number, not by
    trusting the span the tool reports.
    """
    tz = history.tzinfo(cfg)
    from datetime import datetime as dt
    for d in (day, "2026-09-03"):
        base = int(dt.strptime(d, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
        for h in range(24):
            ts = base + h * 3600
            lit = 7 <= h < 18
            history.put_hourly(conn, ts, "load", None, None, None,
                               500.0 if lit else 250.0, None, None, 60, "live")
            history.put_hourly(conn, ts, "solar", None, None,
                               1200.0 if lit else 0.0, None, None, None,
                               60, "live")
            history.put_hourly(conn, ts, "battery", 54.0, None, None,
                               300.0 if not lit else 0.0, None, None,
                               60, "live")
            conn.execute("INSERT INTO samples (ts, battery_v) VALUES (?,?)",
                         (ts, 57.0 if lit else 53.0))
    conn.commit()


def at(cfg, when):
    from datetime import datetime as dt
    return int(dt(*when, tzinfo=history.tzinfo(cfg)).timestamp())


def frozen(monkeypatch, ts):
    monkeypatch.setattr(tools.time, "time", lambda: ts)


def test_overnight_is_the_night_and_not_the_last_24_hours(conn, cfg, monkeypatch):
    """The incident, in one assertion."""
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    t = tools.Tools(conn, cfg)
    night = t.get_history(window="overnight")
    day = t.get_history(hours=24)
    # Sunset 7:11 pm to 6 am: eleven dark hours at 250 Wh, plus the hour the
    # sun set in. The day behind it includes eleven lit hours at 500.
    assert night["load_wh"] < 4000
    assert day["load_wh"] > 7000
    assert night["hours"] < 12 and day["hours"] == 24


def test_a_named_window_states_its_own_span(conn, cfg, monkeypatch):
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    out = tools.Tools(conn, cfg).get_history(window="overnight")
    assert out["window"] == "overnight"
    assert "7:11 pm" in out["window_start"]
    assert "6:00 am" in out["window_end"]
    assert "sunset" in out["window_label"]


def test_last_night_reads_the_finished_night_in_the_evening(conn, cfg, monkeypatch):
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 21, 0)))
    t = tools.Tools(conn, cfg)
    out = t.get_history(window="last_night")
    assert out["window"] == "last_night"
    assert "7:11 pm" in out["window_start"] and "2026-09-02" in out["window_start"]
    assert "2026-09-03" in out["window_end"] and "am" in out["window_end"]
    assert "last night" in out["window_label"]
    assert "last_night" in t.get_history(window="overnight")["window_note"]
    assert "last_night" not in out["window_note"]


def test_trailing_hours_say_they_are_not_overnight(conn, cfg, monkeypatch):
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    out = tools.Tools(conn, cfg).get_history(hours=24)
    assert out["window"] is None
    assert "overnight" in out["window_note"]
    assert out["window_start"] and out["window_end"]


def test_today_and_yesterday_are_calendar_days(conn, cfg, monkeypatch):
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    t = tools.Tools(conn, cfg)
    today = t.get_history(window="today")
    yesterday = t.get_history(window="yesterday")
    assert today["hours"] == 6                      # midnight to 6 am
    assert today["load_wh"] == 1500                 # six dark hours at 250
    assert yesterday["hours"] == 24
    assert yesterday["load_wh"] == 8750             # 11*500 + 13*250
    # Yesterday stops at yesterday: today's hours must not leak in.
    assert yesterday["solar_wh"] == 13200


def test_the_default_is_still_a_trailing_day(conn, cfg, monkeypatch):
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    t = tools.Tools(conn, cfg)
    assert t.get_history(24) == t.get_history()
    assert t.get_history(hours=1000)["hours"] == 720
    assert t.get_history(hours=0)["hours"] == 1


def test_load_and_battery_out_are_never_offered_as_two_flows(conn, cfg, monkeypatch):
    """26,356 Wh "used" was 25,273 of load plus what the pack gave up."""
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    out = tools.Tools(conn, cfg).get_history(window="overnight")
    assert out["battery_out_wh"] > 0 and out["load_wh"] > 0
    assert "never add them" in out["load_vs_battery_note"]
    assert "load_wh" in out["load_vs_battery_note"]


def test_a_window_it_does_not_know_names_the_ones_it_does(conn, cfg, monkeypatch):
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    out = tools.Tools(conn, cfg).get_history(window="last fortnight")
    assert "error" in out
    assert out["windows"] == list(sunmod.WINDOWS)


def test_the_window_reaches_the_model_through_call(conn, cfg, monkeypatch):
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    out = json.loads(tools.Tools(conn, cfg).call(
        "get_history", {"window": "overnight"}))
    assert out["window"] == "overnight"
    schema = [s for s in tools.SCHEMAS
              if s["function"]["name"] == "get_history"][0]["function"]
    assert schema["parameters"]["properties"]["window"]["enum"] == \
        list(sunmod.WINDOWS)
    assert "overnight" in schema["description"]


# --- Why a generator is running ---------------------------------------------
#
# 2026-09-03, 6:49 PM: the Kubota was running an AGS exercise and the agent
# said "voltage dropped below 52.0 V" at 59.4 V, then added that no load was
# being drawn. Neither claim came from a field. Both now have one.

AGS_DATA = {
    "batteryVoltage": 59.4, "battPower": 2400, "battCurrent": 40.0,
    "battMonitorOnline": True, "battAhRemaining": 1500,
    "battMinToDischarge": 900,
    "mppt80PVPower": 0, "southArrayPVPower": 0, "westArrayPVPower": 0,
    "acPower1": 600, "acPower2": 500,
    "mep803aAction": history.GEN_STOPPED, "kubotaAction": history.GEN_RUNNING,
    "mep803aMode": 2, "kubotaMode": 2,
    "mepAgsOnline": True, "kubotaAgsOnline": True,
    "mepOnReason": "not_on", "kubotaOnReason": "exercise",
    "autoGenEnabled": True, "pollErrors": 0, "lastUpdate": "01:02:03",
}


def a_live_poll(monkeypatch, **over):
    data = dict(AGS_DATA, **over)
    monkeypatch.setattr(tools.history, "fetch_data", lambda *a, **k: data)
    monkeypatch.setattr(tools.history, "fetch_config",
                        lambda *a, **k: LIVE_CONFIG["config"])
    return data


def test_status_carries_the_ags_reason(conn, cfg, monkeypatch):
    a_live_poll(monkeypatch)
    out = tools.Tools(conn, cfg).get_status()
    assert out["kubota"]["run_reason"] == "exercise"
    assert out["kubota"]["run_reason_note"] is None
    assert out["mep"]["run_reason"] == "not_on"


def test_status_times_a_running_generator(conn, cfg, monkeypatch):
    a_live_poll(monkeypatch)
    now = int(time.time())
    for i in range(20):
        conn.execute("INSERT INTO samples (ts, kub_action, kub_on_reason) "
                     "VALUES (?,?,?)",
                     (now - (19 - i) * 60, history.GEN_RUNNING, "exercise"))
    conn.commit()
    out = tools.Tools(conn, cfg).get_status()
    assert out["kubota"]["running_minutes"] == 19.0
    assert ":" in out["kubota"]["started_at"]
    # The one that is not running is not given a start time.
    assert "started_at" not in out["mep"]


def test_a_missing_reason_says_it_is_missing(conn, cfg, monkeypatch):
    """Silence would let the model reach for the voltage again."""
    a_live_poll(monkeypatch, kubotaOnReason=None)
    out = tools.Tools(conn, cfg).get_status()
    assert out["kubota"]["run_reason"] is None
    note = out["kubota"]["run_reason_note"]
    assert "not recorded" in note and "Never infer" in note


def test_the_sample_supplies_a_reason_this_poll_lost(conn, cfg, monkeypatch):
    a_live_poll(monkeypatch, kubotaOnReason=None)
    now = int(time.time())
    for i in range(20):
        conn.execute("INSERT INTO samples (ts, kub_action, kub_on_reason) "
                     "VALUES (?,?,?)",
                     (now - (19 - i) * 60, history.GEN_RUNNING, "exercise"))
    conn.commit()
    out = tools.Tools(conn, cfg).get_status()
    assert out["kubota"]["run_reason"] == "exercise"
    assert "the minute it started" in out["kubota"]["run_reason_note"]


def test_load_while_a_generator_runs_is_not_zero_load(conn, cfg, monkeypatch):
    """"no load is being drawn" was said about a null."""
    a_live_poll(monkeypatch)
    out = tools.Tools(conn, cfg).get_status()
    assert out["load_w"] is None
    assert "not zero load" in out["load_w_note"]
    assert "cannot be read" in out["load_w_note"]


def test_a_zero_reading_is_labelled_as_a_reading(conn, cfg, monkeypatch):
    """The inverters read 0 W through an AC transfer."""
    a_live_poll(monkeypatch, kubotaAction=history.GEN_STOPPED, acPower1=0, acPower2=0)
    out = tools.Tools(conn, cfg).get_status()
    assert out["load_w"] == 0
    assert "AC transfer" in out["load_w_note"]


def test_an_unreported_load_is_not_zero_either(conn, cfg, monkeypatch):
    a_live_poll(monkeypatch, kubotaAction=history.GEN_STOPPED,
         acPower1=None, acPower2=None)
    out = tools.Tools(conn, cfg).get_status()
    assert out["load_w"] is None
    assert "not reported" in out["load_w_note"]


def test_an_ordinary_load_gets_no_note(conn, cfg, monkeypatch):
    a_live_poll(monkeypatch, kubotaAction=history.GEN_STOPPED)
    out = tools.Tools(conn, cfg).get_status()
    assert out["load_w"] == 1100
    assert out["load_w_note"] is None


def test_the_schema_points_the_model_at_run_reason(conn, cfg):
    schema = [s for s in tools.SCHEMAS
              if s["function"]["name"] == "get_status"][0]["function"]
    assert "run_reason" in schema["description"]
    assert "only source" in schema["description"]


def test_the_ask_prompt_sends_why_is_it_running_to_run_reason():
    import prompts
    p = prompts.ask_prompt()
    assert "why a generator is running" in p
    assert "run_reason" in p
    assert "the reason is not recorded" in p
    assert "Never infer a" in p and "voltage or the thresholds" in p
    # And the load claim that came with it.
    assert "no load is being drawn" in p
    assert "AC transfer" in p


def test_the_run_reason_decides_nothing(conn, cfg):
    """A reason is reported, never acted on. No threshold moves because a
    run turned out to be an exercise. If a later commit wants that, this is
    the test to delete on purpose."""
    import guard
    src = open(guard.__file__).read()
    for name in ("run_reason", "on_reason", "REASON_KIND", "exercise"):
        assert name not in src, f"guard.py should not know about {name}"


# --- Tonight's shortfall -----------------------------------------------------
#
# 2026-11-04: get_load_forecast returned available_wh 26,164, and both models
# reported that as "the shortfall". The night had 9 kWh to spare.

class Night:
    """A load model that answers only what the tool asks it."""

    def __init__(self, deficit, left=0):
        self.deficit = deficit
        self.left = left

    def load_forecast(self, hours, now=None):
        return {"hours": hours, "total_wh": 9000, "by_hour": []}

    def project_voltage(self, target_v, now=None):
        return {"reached": None, "reason": "not reached within 24 h",
                "voltage": 56.58, "available_wh": 26164}

    def overnight_deficit(self, until, floor_v=52.0, now=None):
        self.asked = (until, floor_v, now)
        return self.deficit

    def overnight_drawdown(self, month=None, now=None):
        return None

    def remaining_solar_wh(self, now=None, until=None):
        return self.left


def night_tools(conn, cfg, deficit, left=0):
    t = tools.Tools(conn, cfg)
    t.model = Night(deficit, left)
    return t


SPARE = {"deficit_wh": -9063, "needed_wh": 17101, "available_wh": 26164, "hours": 11.2}
SHORT = {"deficit_wh": 6508, "needed_wh": 15000, "available_wh": 8492, "hours": 8.0}


def test_available_is_named_for_what_it_is(conn, cfg):
    out = night_tools(conn, cfg, SPARE).get_load_forecast(24)
    p = out["projected_52v"]
    assert "available_wh" not in json.dumps(out)
    assert p["available_above_start_wh"] == 26164
    assert p["available_above_start_kwh"] == 26.2


def test_a_night_with_energy_to_spare_has_no_shortfall(conn, cfg):
    night = night_tools(conn, cfg, SPARE).get_load_forecast(24)["tonight"]
    assert night["shortfall_wh"] == 0 and night["shortfall_kwh"] == 0.0
    assert night["expected_draw_wh"] == 17101
    assert night["available_above_start_wh"] == 26164
    assert night["verdict"].startswith("No shortfall")
    assert "26.2 kWh" in night["verdict"] and "17.1 kWh" in night["verdict"]


def test_a_short_night_says_by_how_much(conn, cfg):
    night = night_tools(conn, cfg, SHORT).get_load_forecast(24)["tonight"]
    assert night["shortfall_wh"] == 6508 and night["shortfall_kwh"] == 6.5
    assert night["verdict"].startswith("Shortfall of 6.5 kWh")


def test_exactly_enough_is_no_shortfall(conn, cfg):
    even = {"deficit_wh": 0, "needed_wh": 9000, "available_wh": 9000, "hours": 8.0}
    assert night_tools(conn, cfg, even).get_load_forecast(24)["tonight"]["shortfall_wh"] == 0


def test_when_the_model_cannot_say_nor_does_the_tool(conn, cfg):
    night = night_tools(conn, cfg, {"deficit_wh": None, "reason": "load profile not learned"}
                        ).get_load_forecast(24)["tonight"]
    assert night["shortfall_wh"] is None
    assert "load profile not learned" in night["verdict"]


SUN_UP = (" Not final: solar still to come will cover load and charge the pack, "
          "so this will likely shrink; the top-up rule decides at 9:00 pm.")
SUN_DOWN = " Not final: the top-up rule decides at 9:00 pm."


def tonight_at(conn, cfg, monkeypatch, when, deficit=SHORT, left=0):
    frozen(monkeypatch, at(cfg, when))
    cfg = dict(cfg, topup_earliest="21:00")
    return night_tools(conn, cfg, deficit, left).get_load_forecast(24)["tonight"]


def test_sun_up_with_a_shortfall_says_it_will_likely_shrink(conn, cfg, monkeypatch):
    """A midday "shortfall" read as settled, while the plan held for the sun. The
    remaining solar is a field and not a sentence: expected_draw has already
    netted it against the load, so adding it to the pack counts it twice."""
    night = tonight_at(conn, cfg, monkeypatch, (2026, 11, 5, 12, 0), SHORT, left=12345)
    assert night["remaining_solar_today_wh"] == 12345
    assert night["remaining_solar_today_kwh"] == 12.3
    assert night["verdict"].startswith("Shortfall of 6.5 kWh")
    assert night["verdict"].endswith(SUN_UP)
    assert "12.3" not in night["verdict"] and "12345" not in night["verdict"]


def test_sun_up_with_no_shortfall_has_nothing_to_shrink(conn, cfg, monkeypatch):
    night = tonight_at(conn, cfg, monkeypatch, (2026, 11, 5, 12, 0), SPARE, left=12345)
    assert night["remaining_solar_today_wh"] == 12345
    assert night["verdict"].startswith("No shortfall")
    assert night["verdict"].endswith(SUN_DOWN)
    assert "shrink" not in night["verdict"] and "solar" not in night["verdict"]


def test_the_forecast_tells_the_model_to_quote_the_verdict_whole():
    schema = next(s["function"] for s in tools.SCHEMAS
                  if s["function"]["name"] == "get_load_forecast")
    assert "quote tonight.verdict in full" in schema["description"]
    assert "Not final" in schema["description"]


def test_from_sunset_until_the_window_opens_only_the_rule_is_left(conn, cfg, monkeypatch):
    night = tonight_at(conn, cfg, monkeypatch, (2026, 11, 5, 18, 0), left=99999)
    assert night["remaining_solar_today_wh"] == 0
    assert night["verdict"].endswith(SUN_DOWN)
    assert "solar" not in night["verdict"]


def test_after_the_window_opens_the_verdict_stands_on_its_own(conn, cfg, monkeypatch):
    night = tonight_at(conn, cfg, monkeypatch, (2026, 11, 5, 22, 0))
    assert night["remaining_solar_today_wh"] == 0
    assert night["remaining_solar_today_kwh"] == 0.0
    assert night["verdict"].startswith("Shortfall of 6.5 kWh")
    assert "Not final" not in night["verdict"]


def test_before_dawn_neither_todays_sun_nor_todays_window_is_tonights(conn, cfg, monkeypatch):
    """At 3 am remaining_solar_wh counts the day ahead and 9 pm is still to come,
    and neither has anything to do with the night that ends at sunrise."""
    night = tonight_at(conn, cfg, monkeypatch, (2026, 11, 5, 3, 0), left=30000)
    assert night["remaining_solar_today_wh"] == 0
    assert "Not final" not in night["verdict"]


def test_in_daylight_a_forecast_that_cannot_say_is_not_taken_for_no_sun(conn, cfg, monkeypatch):
    night = tonight_at(conn, cfg, monkeypatch, (2026, 11, 5, 12, 0), left=None)
    assert night["remaining_solar_today_wh"] is None
    assert night["remaining_solar_today_kwh"] is None
    assert night["verdict"].endswith(SUN_UP)


def test_the_last_of_the_daylight_with_no_solar_left_promises_none(conn, cfg, monkeypatch):
    night = tonight_at(conn, cfg, monkeypatch, (2026, 11, 5, 12, 0), left=0)
    assert night["verdict"].endswith(SUN_DOWN)


def test_the_shortfall_asks_about_the_next_sunrise_above_52(conn, cfg, monkeypatch):
    now = at(cfg, (2026, 9, 3, 21, 0))
    frozen(monkeypatch, now)
    t = night_tools(conn, cfg, SPARE)
    t.get_load_forecast(24)
    until, floor, asked_now = t.model.asked
    assert until == sunmod.next_sunrise(cfg, now=now) and until > now
    assert floor == 52.0 and asked_now == now


def test_a_projection_with_no_figure_is_left_alone():
    p = {"reached": None, "reason": "no battery sample"}
    assert tools.relabel_projection(p) == p


def test_the_load_model_keeps_its_own_field_name():
    """policy.py reads available_wh from the deficit: only the tool output changes."""
    p = {"available_wh": 100}
    tools.relabel_projection(p)
    assert p == {"available_wh": 100}


# --- Energy in the unit people say --------------------------------------------
#
# 2026-11-04, in Spanish: "33,377 kWh de solar", from solar_wh 33377.

def test_history_gives_kwh_beside_every_energy_total(conn, cfg, monkeypatch):
    a_day_and_a_night(conn, cfg)
    frozen(monkeypatch, at(cfg, (2026, 9, 3, 6, 0)))
    out = tools.Tools(conn, cfg).get_history(window="yesterday")
    for key in ("solar", "load", "battery_out"):
        wh, kwh = out[f"{key}_wh"], out[f"{key}_kwh"]
        if wh is None:
            assert kwh is None
        else:
            assert kwh == round(wh / 1000.0, 1), key
    assert out["solar_wh"] and out["load_wh"], "the Wh fields stay, for code"


def test_the_today_block_has_kwh_too(conn, cfg, monkeypatch):
    now = at(cfg, (2026, 9, 3, 6, 0))
    frozen(monkeypatch, now)
    conn.execute("INSERT INTO daily (day, solar_wh, load_wh) VALUES (?, 31295.4, 17087.5)",
                 (history.local_day(now, cfg),))
    today = tools.Tools(conn, cfg).get_history(hours=1)["today"]
    assert today["solar_kwh"] == 31.3 and today["load_kwh"] == 17.1
    assert today["solar_wh"] == 31295.4


def test_the_forecast_has_kwh_too(conn, cfg):
    class WithDrawdown(Night):
        def overnight_drawdown(self, month=None, now=None):
            return {"month": 11, "wh": 17754, "nights": 9}
    t = tools.Tools(conn, cfg)
    t.model = WithDrawdown(SPARE)
    out = t.get_load_forecast(24)
    assert out["total_kwh"] == 9.0 and out["total_wh"] == 9000
    assert out["overnight_drawdown"]["kwh"] == 17.8
    assert out["overnight_drawdown"]["wh"] == 17754


def test_the_weather_estimate_has_kwh_too(conn, cfg, monkeypatch):
    class Sky:
        def estimate_solar_wh(self, cloud, day=None):
            return {"wh": 32105, "clear_day_wh": 33261}
    monkeypatch.setattr(tools.weather, "summary", lambda cfg, **k: {
        "today": {"cloud_pct": 87}, "today_date": "2026-11-04",
        "next_daylight": {"cloud_pct": 17}, "next_daylight_date": "2026-11-05"})
    monkeypatch.setattr(tools.weather, "cloud_of", lambda w: w["cloud_pct"])
    t = tools.Tools(conn, cfg)
    t.model = Sky()
    w = t.get_weather()["next_daylight"]
    assert w["estimated_solar_kwh"] == 32.1 and w["clear_day_kwh"] == 33.3
    assert w["estimated_solar_wh"] == 32105


def test_no_figure_stays_no_figure():
    assert tools._kwh(None) is None and tools._kwh(0) == 0.0
    assert tools._kwh(33377) == 33.4


# --- Fuel for the days asked about --------------------------------------------
#
# 2026-11-04, "this week": both models gave the month's gallons as the week's.

def _run(conn, gen, start_ts, minutes, gal, kind="auto"):
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, gross_w, "
        "fuel_gal, solo, kind) VALUES (?,?,?,?,4000,?,1,?)",
        (gen, start_ts, start_ts + int(minutes * 60), minutes, gal, kind))
    conn.commit()


def test_fuel_is_given_for_the_days_asked_about(conn, cfg):
    now = int(time.time())
    _run(conn, "mep", now - 2 * 86400, 120, 1.5)
    _run(conn, "kubota", now - 3 * 86400, 90, 0.75)
    _run(conn, "mep", now - 40 * 86400, 60, 9.0)
    out = tools.Tools(conn, cfg).get_gen_runtime(7)["fuel_window"]
    none = {"unpriced_runs": 0, "exercise_runs": 0, "exercise_hours": 0.0, "exercise_fuel_gal": 0}
    assert out["mep"] == {"runs": 1, "hours": 2.0, "fuel_gal": 1.5, **none}
    assert out["kubota"] == {"runs": 1, "hours": 1.5, "fuel_gal": 0.75, **none}
    assert out["total"] == {"runs": 2, "hours": 3.5, "fuel_gal": 2.25, **none}
    assert "Last 7 days" in out["summary"] and "modelled" in out["summary"]
    assert "total 3.5 h / 2.25 gal" in out["summary"]


def test_the_month_to_date_fields_stay(conn, cfg):
    now = int(time.time())
    _run(conn, "mep", now - 3600, 60, 1.0)
    fuel = tools.Tools(conn, cfg).get_gen_runtime(7)["fuel"]
    assert "fuel_mtd_gal" in fuel["mep"] and "summary" in fuel


def test_an_exercise_run_burns_fuel_and_its_share_is_shown(conn, cfg):
    now = int(time.time())
    _run(conn, "kubota", now - 3600, 30, 0.2, kind="exercise")
    _run(conn, "kubota", now - 7200, 60, 1.0)
    out = tools.Tools(conn, cfg).get_gen_runtime(7)["fuel_window"]
    assert out["kubota"]["runs"] == 2 and out["kubota"]["hours"] == 1.5
    assert out["kubota"]["fuel_gal"] == 1.2
    assert out["kubota"]["exercise_runs"] == 1
    assert out["kubota"]["exercise_hours"] == 0.5
    assert out["kubota"]["exercise_fuel_gal"] == 0.2
    assert "of which exercise 0.5 h / 0.20 gal" in out["summary"]


def test_a_window_with_no_exercise_does_not_mention_it(conn, cfg):
    _run(conn, "kubota", int(time.time()) - 7200, 60, 1.0)
    assert "exercise" not in tools.Tools(conn, cfg).get_gen_runtime(7)["fuel_window"]["summary"]


def test_the_days_fuel_counts_an_exercise_run_and_shows_its_share(conn, cfg):
    now = int(time.time())
    _run(conn, "kubota", now - 60, 30, 0.2, kind="exercise")
    fuel = tools.Tools(conn, cfg).fuel_totals(now=now)["kubota"]
    assert fuel["runs_today"] == 1 and fuel["hours_today"] == 0.5
    assert fuel["fuel_today_gal"] == 0.2
    assert fuel["exercise_hours_today"] == 0.5 and fuel["exercise_fuel_today_gal"] == 0.2


def test_runtime_totals_split_out_exercise_and_label_each_run(conn, cfg):
    now = int(time.time())
    _run(conn, "kubota", now - 3600, 14, 0.1, kind="exercise")
    _run(conn, "kubota", now - 7200, 60, 1.0)
    out = tools.Tools(conn, cfg).get_gen_runtime(1)
    assert out["totals"]["kubota"] == {"minutes": 74.0, "runs": 2,
                                       "exercise_minutes": 14.0, "exercise_runs": 1}
    assert sorted(r["kind"] for r in out["runs"]) == ["auto", "exercise"]
    assert out["generators_with_no_runs"] == ["mep"]


def test_a_generator_that_only_exercised_did_run(conn, cfg):
    """"Neither generator ran" was the wrong answer on 2026-11-05."""
    _run(conn, "kubota", int(time.time()) - 3600, 14, 0.1, kind="exercise")
    assert "kubota" not in tools.Tools(conn, cfg).get_gen_runtime(1)["generators_with_no_runs"]


def test_charge_rate_learning_still_ignores_exercise(conn, cfg):
    """Showing exercise runs must not teach anything from them. The exercise run
    here would move every rate if it were read: long enough to count, with a
    gross ten times the real one."""
    now = int(time.time())
    def learned():
        t = tools.Tools(conn, cfg)
        out = t.get_gen_runtime(30)
        return (out["charge_rates"], t.model.charge_rates(),
                t.model.charge_rate("kubota", solo=True),
                [dict(r) for r in history.charge_samples(conn)])
    for i in range(3):
        conn.execute(
            "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, solo, kind, "
            "gross_w, load_w, rate_a, rate_v_per_h, fuel_gal) "
            "VALUES ('kubota', ?, ?, 60, 1, 'auto', 3000, 800, 40, 1.0, 0.5)",
            (now - (i + 1) * 86400, now - (i + 1) * 86400 + 3600))
        ts = now - (i + 1) * 86400 + 600
        conn.execute("INSERT INTO samples (ts, battery_v, batt_soc) VALUES (?, 54.5, 70)", (ts,))
    conn.commit()
    before = learned()
    assert before[1], "the fixture must teach a rate, or this proves nothing"
    start = now - 4 * 3600
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, solo, kind, "
        "gross_w, load_w, rate_a, rate_v_per_h, fuel_gal) "
        "VALUES ('kubota', ?, ?, 60, 1, 'exercise', 30000, 800, 400, 10.0, 5.0)",
        (start, start + 3600))
    conn.execute("INSERT INTO samples (ts, battery_v, batt_soc) VALUES (?, 58.0, 99)", (start + 600,))
    conn.commit()
    assert learned() == before
    # and the run is there to be seen
    assert "exercise" in [r["kind"] for r in tools.Tools(conn, cfg).get_gen_runtime(30)["runs"]]


def test_days_with_no_runs_burned_nothing(conn, cfg):
    out = tools.Tools(conn, cfg).fuel_window([], 1)
    assert out["total"] == {"runs": 0, "hours": 0.0, "fuel_gal": 0, "unpriced_runs": 0,
                            "exercise_runs": 0, "exercise_hours": 0.0, "exercise_fuel_gal": 0}
    assert "Last 1 day:" in out["summary"]


def test_a_run_with_no_fuel_figure_is_owned_up_to(conn, cfg):
    now = int(time.time())
    _run(conn, "mep", now - 3600, 60, 1.0)
    _run(conn, "mep", now - 7200, 60, None)
    _run(conn, "kubota", now - 9000, 30, None)
    out = tools.Tools(conn, cfg).get_gen_runtime(7)["fuel_window"]
    assert out["mep"]["fuel_gal"] == 1.0 and out["mep"]["unpriced_runs"] == 1
    assert out["kubota"]["fuel_gal"] is None
    assert out["total"]["unpriced_runs"] == 2
    assert "2 runs had no fuel figure" in out["summary"]
    assert "kubota 0.5 h / no fuel figure" in out["summary"]



# --- Which tool answers "how many kWh yesterday" --------------------------------
#
# 2026-11-05 and 11-06: "kWh" drew the question to the monthly tool, and October's
# 1,025 or the month so far was given as yesterday's. "How much solar", with no
# kWh in it, went to get_history. The first thing each description says is the rule.

def _description(name):
    return next(s["function"]["description"] for s in tools.SCHEMAS
                if s["function"]["name"] == name)


def test_the_monthly_tool_rules_out_a_single_day_first():
    opening = _description("get_monthly_summary")[:120]
    assert "Whole calendar months only" in opening
    for word in ("today", "yesterday", "last night", "single day", "get_history"):
        assert word in opening


def test_the_history_tool_claims_how_much_today_or_yesterday_first():
    opening = _description("get_history")[:120]
    for word in ("how much solar", "today or yesterday", "kWh", 'window="yesterday"'):
        assert word in opening


def test_the_verdicts_tonight_energy_writes_become_the_spanish_sentences(conn, cfg, monkeypatch):
    """with_not_final reads the time and the form out of the real sentence."""
    def spanish(when, deficit):
        night = tonight_at(conn, cfg, monkeypatch, when, deficit, left=12345)
        return tools.with_not_final("Déficit.", [("get_load_forecast", {}, {"tonight": night})], "es")
    assert spanish((2026, 11, 5, 12, 0), SHORT) == (
        "Déficit. No es definitivo: la energía solar que aún falta cubrirá el consumo y "
        "cargará la batería, así que esto probablemente se reducirá; la regla de recarga "
        "decide a las 9:00 pm.")
    assert spanish((2026, 11, 5, 12, 0), SPARE) == (
        "Déficit. No es definitivo: la regla de recarga decide a las 9:00 pm.")
