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
    assert len(named) == 14


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
    assert out["actions"][0]["allowed"] is True
    assert out["actions"][1]["result"] == "refused"


def test_recent_actions_is_bounded(conn, cfg):
    for i in range(80):
        history.record_action(conn, "x", {}, 1, "r", None, None, "allowed",
                              ts=1000 + i)
    assert tools.Tools(conn, cfg).get_recent_actions(500)["count"] == 50
    assert tools.Tools(conn, cfg).get_recent_actions(0)["count"] == 1
