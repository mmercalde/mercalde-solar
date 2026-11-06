"""What vps/alexa_solar.py puts on a screen. The real handler runs against a stand-in
for Flask, with every HTTP call answered here, so nothing reaches the Pi5 or the agent.
"""

import importlib.util
import json
import os
import re
import sys
import types

import pytest

VPS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vps", "alexa_solar.py")

LIMIT = 24 * 1024
SOLAR = {"batterySOC": 85, "batteryVoltage": 52.46, "mppt80PVPower": 100,
         "southArrayPVPower": 200, "westArrayPVPower": 300, "mep803aMode": 2,
         "kubotaMode": 0, "autoGenEnabled": True}
SECRET = "123456:SECRET-TOKEN"
CONFIG = {"config": {"mep803a": {"startVoltage": 52.0, "stopVoltage": 56.0},
                     "kubota": {"startVoltage": 51.5, "stopVoltage": 57.0},
                     "telegram": {"token": SECRET, "chatId": "99"}},
          "events": []}
WITH_SCREEN = {"System": {"device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}},
                          "viewport": {"shape": "RECTANGLE"}}}
NO_SCREEN = {"System": {"device": {"supportedInterfaces": {}}}}
READS = ["GetBatteryIntent", "GetStatusIntent", "GetSolarIntent", "GetGeneratorIntent"]


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
    spec = importlib.util.spec_from_file_location("alexa_screens", VPS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.calls = []
    module.routes = {"data": SOLAR, "config": CONFIG, "events": {"events": []}}
    module.agent = (200, "The battery is fine.")

    def get(url, **kw):
        module.calls.append(url)
        reply = module.routes[url.rsplit("/", 1)[1]]
        if isinstance(reply, Exception):
            raise reply
        return types.SimpleNamespace(status_code=200, text="", json=lambda: reply)

    def post(url, **kw):
        module.calls.append(url)
        code, text = module.agent
        return types.SimpleNamespace(status_code=code, text=text)

    module.requests = types.SimpleNamespace(get=get, post=post)
    module.flask_request = flask.request
    return module


def ask(alexa, intent=None, context=WITH_SCREEN, locale="en-US", query=None, kind="IntentRequest"):
    request = {"type": kind, "locale": locale}
    if intent:
        request["intent"] = {"name": intent}
        if query is not None:
            request["intent"]["slots"] = {"query": {"name": "query", "value": query}}
    alexa.flask_request.json = {"context": context, "request": request}
    alexa.calls.clear()
    return alexa.alexa_handler()


def directive(body):
    return body["response"]["directives"][0]


def payload(body):
    return directive(body)["datasources"]["solar"]


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


# --- keeping the screen up ------------------------------------------------------------

@pytest.mark.parametrize("intent", READS + ["GetEventsIntent", "AskAgentIntent"])
def test_a_read_answer_on_a_screen_leaves_the_session_unset(alexa, intent):
    body = ask(alexa, intent, query="is it fine" if intent == "AskAgentIntent" else None)
    assert "shouldEndSession" not in body["response"]
    assert directive(body)["document"]["settings"] == {"idleTimeout": 60000}


@pytest.mark.parametrize("intent", READS + ["GetEventsIntent", "AskAgentIntent"])
def test_no_screen_behaves_as_before(alexa, intent):
    body = ask(alexa, intent, NO_SCREEN, query="is it fine" if intent == "AskAgentIntent" else None)
    assert body["response"]["shouldEndSession"] is True
    assert "directives" not in body["response"]
    assert not [c for c in alexa.calls if c.endswith("/config")]


def test_a_screen_with_nothing_to_show_ends_as_before(alexa):
    alexa.routes["data"] = OSError("down")
    body = ask(alexa, "GetBatteryIntent")
    assert body["response"]["shouldEndSession"] is True and "directives" not in body["response"]


@pytest.mark.parametrize("name,status", [("StartGeneratorIntent", "NONE"),
                                         ("StopGeneratorIntent", "NONE"),
                                         ("SetGeneratorOffIntent", "NONE")])
def test_confirmation_dialogs_are_unchanged(alexa, name, status):
    alexa.flask_request.json = {"context": WITH_SCREEN, "request": {
        "type": "IntentRequest", "locale": "en-US", "intent": {
            "name": name, "confirmationStatus": status,
            "slots": {"generator": {"name": "generator", "value": "kubota"}}}}}
    alexa.calls.clear()
    body = alexa.alexa_handler()
    assert body["response"]["shouldEndSession"] is False
    assert "directives" not in body["response"] and alexa.calls == []


# --- the dashboard ----------------------------------------------------------------------

@pytest.mark.parametrize("kind,intent", [("LaunchRequest", None), ("IntentRequest", "AMAZON.HelpIntent")])
def test_launch_and_help_show_the_dashboard_and_keep_the_mic(alexa, kind, intent):
    body = ask(alexa, intent, kind=kind)
    assert payload(body)["totalSolar"] == 600
    assert body["response"]["shouldEndSession"] is False


def test_thresholds_are_under_each_generator(alexa):
    p = payload(ask(alexa, "GetGeneratorIntent"))
    assert p["mepThresholds"] == "Start 52.0 V · Stop 56.0 V"
    assert p["kubotaThresholds"] == "Start 51.5 V · Stop 57.0 V"


def test_thresholds_in_spanish(alexa):
    p = payload(ask(alexa, "GetGeneratorIntent", locale="es-MX"))
    assert p["mepThresholds"] == "Prende 52.0 V · Apaga 56.0 V"


@pytest.mark.parametrize("broken", [OSError("down"), {}, {"config": {"kubota": {}}},
                                    {"config": {"mep803a": {"startVoltage": None, "stopVoltage": 1},
                                                "kubota": {"startVoltage": 1, "stopVoltage": 1}}}])
def test_no_config_leaves_the_line_off(alexa, broken):
    alexa.routes["config"] = broken
    body = ask(alexa, "GetGeneratorIntent")
    assert payload(body)["mepThresholds"] == "" and payload(body)["kubotaThresholds"] == ""
    assert payload(body)["totalSolar"] == 600


def test_the_config_is_only_ever_read_with_no_parameters(alexa):
    ask(alexa, "GetGeneratorIntent")
    assert [c for c in alexa.calls if "/config" in c] == [alexa.ESP8266_CONFIG_URL]


def test_the_telegram_token_goes_nowhere(alexa, capsys):
    body = ask(alexa, "GetStatusIntent")
    assert SECRET not in json.dumps(body) and SECRET not in capsys.readouterr().out
    alexa.routes["config"] = {"config": {"telegram": {"token": SECRET}}}
    body = ask(alexa, "GetStatusIntent")
    assert SECRET not in json.dumps(body) and SECRET not in capsys.readouterr().out


def test_round_and_rectangle_are_each_chosen_by_when(alexa):
    items = directive(ask(alexa, "GetBatteryIntent"))["document"]["mainTemplate"]["items"]
    assert [i["when"] for i in items] == ["${viewport.shape == 'round'}",
                                          "${viewport.shape != 'round'}"]
    column = items[0]["item"]
    assert column["paddingLeft"] == "15vw" and column["paddingTop"] == "15vh"
    assert column.get("direction", "column") == "column"


def test_the_round_column_says_what_was_asked_for(alexa):
    items = directive(ask(alexa, "GetBatteryIntent"))["document"]["mainTemplate"]["items"]
    text = [c["text"] for c in items[0]["item"]["items"]]
    for field in ("batterySOC", "batteryVoltage", "totalSolar", "mepStatus", "kubotaStatus",
                  "mepThresholds", "kubotaThresholds"):
        assert any(field in t for t in text), field


# --- what APL actually accepts ------------------------------------------------------------

def every_screen(alexa):
    """Each document with the datasource it gets, from the handler itself."""
    alexa.routes["events"] = {"events": ["e1"]}
    bodies = [ask(alexa, "GetBatteryIntent"), ask(alexa, "GetEventsIntent"),
              ask(alexa, "AskAgentIntent", query="anything")]
    alexa.agent = (202, "")
    bodies.append(ask(alexa, "AskAgentIntent", query="anything"))
    return [directive(b) for b in bodies]


def test_no_document_binds_the_reserved_payload_parameter(alexa):
    for d in every_screen(alexa):
        assert "${payload." not in json.dumps(d["document"])
        assert d["document"]["mainTemplate"]["parameters"] == ["solar"]
        assert list(d["datasources"]) == ["solar"]


def test_every_builder_is_free_of_payload_bindings(alexa):
    for build in (alexa.build_apl_document, alexa.build_answer_document, alexa.build_events_document):
        assert "${payload." not in json.dumps(build())


def test_every_binding_has_a_value_to_show(alexa):
    """A name missing from the datasource renders blank on the device, silently."""
    for d in every_screen(alexa):
        names = set(re.findall(r"\$\{solar\.(\w+)", json.dumps(d["document"])))
        assert names, d["document"]
        assert names <= set(d["datasources"]["solar"]), names - set(d["datasources"]["solar"])


def test_no_style_keys_are_left(alexa):
    for d in every_screen(alexa):
        assert not [n for n in walk(d["document"]) if "style" in n]


def test_frames_hold_one_child_and_a_real_colour(alexa):
    for d in every_screen(alexa):
        frames = [n for n in walk(d["document"]) if n.get("type") == "Frame"]
        assert frames
        for frame in frames:
            assert "item" in frame and "items" not in frame
            assert re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", frame["backgroundColor"])


def test_every_page_has_the_background(alexa):
    for d in every_screen(alexa):
        for item in d["document"]["mainTemplate"]["items"]:
            assert item["type"] == "Frame" and item["backgroundColor"] == "#1a1a2e"


def test_the_dashboard_cards_are_rounded_frames(alexa):
    doc = alexa.build_apl_document()
    cards = [n for n in walk(doc) if n.get("borderRadius")]
    assert len(cards) == 4
    assert all(c["type"] == "Frame" and c["backgroundColor"] == "#FFFFFF1A" for c in cards)


# --- events -------------------------------------------------------------------------------

def test_events_are_the_last_five_newest_first(alexa):
    alexa.routes["events"] = {"events": [f"e{i}" for i in range(7)]}
    assert payload(ask(alexa, "GetEventsIntent"))["events"] == ["e0", "e1", "e2", "e3", "e4"]


@pytest.mark.parametrize("locale,title,empty", [("en-US", "RECENT EVENTS", "No recent events."),
                                                ("es-US", "EVENTOS RECIENTES", "No hay eventos recientes.")])
def test_events_with_nothing_to_list(alexa, locale, title, empty):
    p = payload(ask(alexa, "GetEventsIntent", locale=locale))
    assert p == {"title": title, "events": [empty]}


# --- agent questions -------------------------------------------------------------------------

def test_an_answer_shows_the_question_the_answer_and_the_strip(alexa):
    body = ask(alexa, "AskWhyIntent", query="did it start")
    p = payload(body)
    assert p["question"] == "why did it start" and p["answer"] == "The battery is fine."
    assert p["statusLine"] == "85% · 52.46 V · 600 W"
    assert p["genLine"] == "MEP-803A: AUTO · KUBOTA: OFF"
    assert body["response"]["outputSpeech"]["text"] == "The battery is fine."


def test_the_strip_in_spanish(alexa):
    p = payload(ask(alexa, "AskPorQueIntent", locale="es-US", query="arrancó"))
    assert p["question"] == "por qué arrancó"
    assert p["genLine"] == "MEP-803A: EN AUTOMATICO · KUBOTA: APAGADO"


@pytest.mark.parametrize("locale,line", [("en-US", "Answer sent to your phone"),
                                         ("es-US", "Respuesta enviada a tu teléfono")])
def test_a_202_shows_the_dashboard_and_the_line(alexa, locale, line):
    alexa.agent = (202, "")
    body = ask(alexa, "AskAgentIntent", locale=locale, query="anything")
    assert payload(body)["notice"] == line and payload(body)["totalSolar"] == 600
    assert payload(body)["mepThresholds"]
    assert "shouldEndSession" not in body["response"]


def test_a_202_without_the_pi5_still_shows_the_line(alexa):
    alexa.agent = (202, "")
    alexa.routes["data"] = OSError("down")
    assert payload(ask(alexa, "AskAgentIntent", query="anything"))["answer"] == "Answer sent to your phone"


def test_an_answer_without_the_pi5_has_no_strip(alexa):
    alexa.routes["data"] = OSError("down")
    p = payload(ask(alexa, "AskAgentIntent", query="anything"))
    assert p["statusLine"] == "" and p["answer"] == "The battery is fine."


def test_the_agent_failing_still_gets_a_screen(alexa):
    alexa.agent = (500, "")
    assert payload(ask(alexa, "AskAgentIntent", query="anything"))["answer"] == alexa.AGENT_FALLBACK["en"]


def test_a_slow_agent_skips_the_pi5_so_alexa_gets_its_answer(alexa):
    alexa.AGENT_STATUS_DEADLINE = 0
    body = ask(alexa, "AskAgentIntent", query="anything")
    assert payload(body)["statusLine"] == ""
    assert alexa.calls == [alexa.AGENT_ASK_URL]


def test_the_figures_after_an_agent_answer_use_the_short_timeout(alexa):
    seen = []
    real = alexa.requests.get
    alexa.requests = types.SimpleNamespace(
        get=lambda url, **kw: seen.append(kw["timeout"]) or real(url, **kw), post=alexa.requests.post)
    alexa.agent = (202, "")
    ask(alexa, "AskAgentIntent", query="anything")
    assert seen == [alexa.AGENT_STATUS_TIMEOUT, alexa.AGENT_STATUS_TIMEOUT]


def test_no_empty_question_screen(alexa):
    body = ask(alexa, "AskAgentIntent", query="  ")
    assert "directives" not in body["response"] and body["response"]["shouldEndSession"] is False


def test_nothing_on_these_paths_writes(alexa):
    for intent in READS + ["GetEventsIntent", "AMAZON.HelpIntent"]:
        ask(alexa, intent)
        assert all(c.rsplit("/", 1)[1] in ("data", "config", "events") for c in alexa.calls), intent


# --- size -------------------------------------------------------------------------------------

def test_a_long_answer_is_clipped_on_screen_but_not_in_speech(alexa):
    alexa.agent = (200, "é" * 5000)
    body = ask(alexa, "AskAgentIntent", locale="es-US", query="q" * 1000)
    p = payload(body)
    assert len(p["answer"]) == alexa.SCREEN_ANSWER_CHARS and p["answer"].endswith("…")
    assert len(p["question"]) == alexa.SCREEN_QUESTION_CHARS
    assert body["response"]["outputSpeech"]["text"] == "é" * 5000


@pytest.mark.parametrize("intent,query", [("GetStatusIntent", None), ("GetEventsIntent", None),
                                          ("AskAgentIntent", "q" * 1000)],
                         ids=["dashboard", "events", "answer"])
def test_every_screen_fits_alexas_limit(alexa, intent, query):
    # The agent's ceiling is 1,024 tokens, about 5,000 characters, in accented Spanish.
    alexa.agent = (200, ("la batería está en él según el generador, ¿qué pasó? " * 100)[:5000])
    alexa.routes["events"] = {"events": ["0" * 200] * 5}
    size = len(json.dumps(ask(alexa, intent, locale="es-US", query=query)).encode())
    assert size < LIMIT, size


def test_templates_leave_the_values_in_the_datasource(alexa):
    """So an answer can't be read as an APL expression: it never enters the document."""
    alexa.agent = (200, "${viewport.width}")
    body = ask(alexa, "AskAgentIntent", query="${1+1}")
    doc = json.dumps(directive(body)["document"])
    assert "viewport.width" not in doc and "1+1" not in doc
    assert all("when" in i for i in directive(ask(alexa, "GetBatteryIntent"))["document"]["mainTemplate"]["items"])
