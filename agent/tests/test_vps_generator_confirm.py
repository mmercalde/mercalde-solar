"""Start, stop and off act only on a CONFIRMED intent, and no test here reaches a generator.

The real vps/alexa_solar.py handler runs against a stand-in for Flask, with every
HTTP call recorded instead of sent. Alexa's intent confirmation decides between
CONFIRMED and DENIED; a voice start or stop must not move anything without it.
"""

import importlib.util
import os
import sys
import types

import pytest

VPS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vps", "alexa_solar.py")


class FakeThread:
    """Runs the work at once, so the recorded call is there when the handler returns."""

    def __init__(self, target=None, args=(), **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def alexa(monkeypatch):
    class FakeApp:
        def __init__(self, name):
            pass

        def route(self, *a, **k):
            return lambda f: f

    flask = types.ModuleType("flask")
    flask.Flask = FakeApp
    flask.request = types.SimpleNamespace(json=None)
    flask.jsonify = lambda body: body
    monkeypatch.setitem(sys.modules, "flask", flask)
    spec = importlib.util.spec_from_file_location("alexa_under_test", VPS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls = []

    def record(url, **kw):
        calls.append(url)
        return types.SimpleNamespace(status_code=200, text="OK", json=lambda: {})
    module.requests = types.SimpleNamespace(get=record, post=record)
    module.threading = types.SimpleNamespace(Thread=FakeThread)
    module.calls = calls
    module.flask_request = flask.request
    return module


def say(alexa, intent, status=None, generator="kubota", locale="en-US"):
    body = {"name": intent, "slots": {"generator": {"name": "generator", "value": generator}}}
    if status is not None:
        body["confirmationStatus"] = status
    alexa.flask_request.json = {"request": {"type": "IntentRequest", "locale": locale,
                                            "intent": body}}
    alexa.calls.clear()
    return alexa.alexa_handler()["response"]


def speech(response):
    return response["outputSpeech"]["text"]


GUARDED = ["StartGeneratorIntent", "StopGeneratorIntent", "SetGeneratorOffIntent"]


@pytest.mark.parametrize("intent", GUARDED)
@pytest.mark.parametrize("status", ["NONE", None, "", "SOMETHING_NEW"])
@pytest.mark.parametrize("generator", ["kubota", "mep", "both"])
def test_nothing_moves_without_confirmation(alexa, intent, status, generator):
    say(alexa, intent, status, generator)
    assert alexa.calls == []


@pytest.mark.parametrize("intent", GUARDED)
@pytest.mark.parametrize("generator", ["kubota", "mep", "both", "the blue one"])
def test_denied_moves_nothing_and_says_so(alexa, intent, generator):
    response = say(alexa, intent, "DENIED", generator)
    assert alexa.calls == []
    assert speech(response) == "OK, I won't."
    assert response["shouldEndSession"] is True


@pytest.mark.parametrize("intent", GUARDED)
def test_denied_in_spanish(alexa, intent):
    response = say(alexa, intent, "DENIED", "kubota", "es-US")
    assert alexa.calls == []
    assert speech(response) == "Está bien, no lo haré."


@pytest.mark.parametrize("generator,ids", [("kubota", ["50"]), ("mep", ["51"]),
                                           ("both", ["50", "51"])])
def test_a_confirmed_start_asks_the_pi5_for_mode_on(alexa, generator, ids):
    say(alexa, "StartGeneratorIntent", "CONFIRMED", generator)
    assert alexa.calls == [f"{alexa.ESP8266_GEN_URL}?id={i}&state=1" for i in ids]


@pytest.mark.parametrize("generator,ids", [("kubota", ["50"]), ("mep", ["51"]),
                                           ("both", ["50", "51"])])
def test_a_confirmed_stop_uses_the_graceful_stop(alexa, generator, ids):
    say(alexa, "StopGeneratorIntent", "CONFIRMED", generator)
    assert alexa.calls == [f"{alexa.ESP8266_STOPGEN_URL}?id={i}" for i in ids]


@pytest.mark.parametrize("generator,ids", [("kubota", ["50"]), ("mep", ["51"]),
                                           ("both", ["50", "51"])])
def test_a_confirmed_off_writes_mode_off(alexa, generator, ids):
    say(alexa, "SetGeneratorOffIntent", "CONFIRMED", generator)
    assert alexa.calls == [f"{alexa.ESP8266_GEN_URL}?id={i}&state=0" for i in ids]


@pytest.mark.parametrize("locale,asks", [("en-US", "Please confirm: set the kubota to off?"),
                                         ("es-US", "Confirma: poner el kubota en modo apagado?")])
def test_an_unconfirmed_off_asks_and_keeps_the_session_open(alexa, locale, asks):
    response = say(alexa, "SetGeneratorOffIntent", "NONE", "kubota", locale)
    assert speech(response) == asks and response["shouldEndSession"] is False


def test_auto_is_left_as_it_was(alexa):
    """Off was the one to guard. Auto hands the generator back to the AGS."""
    say(alexa, "SetGeneratorAutoIntent", "NONE", "kubota")
    assert alexa.calls == [f"{alexa.ESP8266_GEN_URL}?id=50&state=2"]


# --- which generator ---------------------------------------------------------------

ALL_FOUR = GUARDED + ["SetGeneratorAutoIntent"]
WHICH = {"en-US": "Which generator, the Kubota or the MEP?",
         "es-US": "¿Cuál generador, el Kubota o el MEP?",
         "es-MX": "¿Cuál generador, el Kubota o el MEP?"}


def say_slot(alexa, intent, slot, status="NONE", locale="en-US"):
    body = {"name": intent, "confirmationStatus": status}
    if slot is not None:
        body["slots"] = {"generator": slot}
    alexa.flask_request.json = {"request": {"type": "IntentRequest", "locale": locale,
                                            "intent": body}}
    alexa.calls.clear()
    return alexa.alexa_handler()["response"]


def resolved(raw, code, name=None):
    values = [{"value": {"name": name, "id": "x"}}] if name else []
    return {"name": "generator", "value": raw, "resolutions": {"resolutionsPerAuthority": [
        {"authority": "a", "status": {"code": code}, "values": values}]}}


NOBODY = [
    None,                                                # no slots at all
    {"name": "generator"},                               # slot with nothing in it
    {"name": "generator", "value": ""},
    {"name": "generator", "value": "unknown"},
    {"name": "generator", "value": "the blue one"},
    resolved("the blue one", "ER_SUCCESS_NO_MATCH"),
    resolved("unknown", "ER_SUCCESS_NO_MATCH"),
    {"name": "generator", "resolutions": {"resolutionsPerAuthority": []}},
    {"name": "generator", "resolutions": None},
]


@pytest.mark.parametrize("intent", ALL_FOUR)
@pytest.mark.parametrize("status", ["NONE", "CONFIRMED"])
@pytest.mark.parametrize("locale", list(WHICH))
@pytest.mark.parametrize("slot", NOBODY)
def test_no_generator_asks_which_and_moves_nothing(alexa, intent, status, locale, slot):
    response = say_slot(alexa, intent, slot, status, locale)
    assert alexa.calls == []
    assert speech(response) == WHICH[locale]
    assert response["shouldEndSession"] is False


@pytest.mark.parametrize("intent", GUARDED)
@pytest.mark.parametrize("slot", NOBODY)
def test_denied_needs_no_generator(alexa, intent, slot):
    response = say_slot(alexa, intent, slot, "DENIED")
    assert alexa.calls == []
    assert speech(response) == "OK, I won't."
    assert response["shouldEndSession"] is True


@pytest.mark.parametrize("intent", ALL_FOUR)
@pytest.mark.parametrize("slot", NOBODY)
def test_unknown_is_never_spoken(alexa, intent, slot):
    for status in ("NONE", "DENIED", "CONFIRMED"):
        assert "unknown" not in speech(say_slot(alexa, intent, slot, status)).lower()


def test_the_resolved_name_wins_over_what_was_heard(alexa):
    response = say_slot(alexa, "StartGeneratorIntent",
                        resolved("cubota", "ER_SUCCESS_MATCH", "kubota"))
    assert speech(response) == "Please confirm: start the kubota?"


def test_the_resolved_name_is_the_one_acted_on(alexa):
    say_slot(alexa, "StartGeneratorIntent",
             resolved("the military one", "ER_SUCCESS_MATCH", "mep"), "CONFIRMED")
    assert alexa.calls == [f"{alexa.ESP8266_GEN_URL}?id=51&state=1"]


def test_a_name_resolved_without_a_match_is_ignored(alexa):
    """Only ER_SUCCESS_MATCH counts, so the raw value decides here."""
    say_slot(alexa, "SetGeneratorOffIntent",
             resolved("kubota", "ER_SUCCESS_NO_MATCH", "mep"), "CONFIRMED")
    assert alexa.calls == [f"{alexa.ESP8266_GEN_URL}?id=50&state=0"]


def test_a_canonical_name_that_maps_to_nothing_falls_back_to_the_raw_value(alexa):
    say_slot(alexa, "StopGeneratorIntent",
             resolved("kubota", "ER_SUCCESS_MATCH", "generator one"), "CONFIRMED")
    assert alexa.calls == [f"{alexa.ESP8266_STOPGEN_URL}?id=50"]


def test_the_generator_ids_are_the_pi5s():
    source = open(os.path.join(os.path.dirname(VPS), "..", "pi5", "app.py")).read()
    assert "AGS_KUBOTA_ID = 50" in source and "AGS_MEP803A_ID = 51" in source


# --- a session that ended ---------------------------------------------------------

def ended(alexa, **fields):
    alexa.flask_request.json = {"request": {"type": "SessionEndedRequest", "locale": "en-US",
                                            **fields}}
    return alexa.alexa_handler()


def test_a_session_end_gets_an_empty_response_and_no_speech(alexa):
    assert ended(alexa, reason="USER_INITIATED") == {"version": "1.0", "response": {}}


def test_the_reason_a_session_failed_reaches_the_log(alexa, capsys):
    ended(alexa, reason="ERROR", error={"type": "INVALID_RESPONSE", "message": "bad"})
    out = capsys.readouterr().out
    assert "reason=ERROR" in out and "INVALID_RESPONSE" in out and "bad" in out


def test_an_ended_session_moves_nothing(alexa):
    alexa.calls.clear()
    ended(alexa, reason="ERROR")
    assert alexa.calls == []
