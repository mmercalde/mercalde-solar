"""What vps/alexa_solar.py puts on a screen. The real handler runs against a stand-in
for Flask, with every HTTP call answered here, so nothing reaches the Pi5 or the agent.
"""

from datetime import date, datetime, timedelta, timezone
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
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
DAY = date(2026, 11, 6)
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

    sunrise, sunset = module.sun_times(DAY)
    module.current_time = lambda: (sunrise + sunset) / 2
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
    assert payload(body)["solarKw"] == "0.6 kW"
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
    assert payload(body)["solarKw"] == "0.6 kW"


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


def scene_text(alexa, index):
    items = alexa.build_apl_document()["mainTemplate"]["items"][index]["item"]["items"]
    return " ".join(i["text"] for i in items if i["type"] == "Text")


def test_the_spot_has_no_thresholds_or_arrays(alexa):
    text = scene_text(alexa, 0)
    for field in ("Thresholds", "mpptW", "southW", "westW", "arrayTitle"):
        assert field not in text, field
    for field in ("nowText", "batterySOC", "batteryVoltage", "skyEvent", "houseKw"):
        assert field in text, field


def test_the_show_has_all_of_it(alexa):
    text = scene_text(alexa, 1)
    for field in ("mepThresholds", "kubotaThresholds", "mpptW", "southW", "westW", "arrayTitle",
                  "autoText", "nowText", "skyEvent", "batterySOC", "houseLabel", "mepWord", "kubotaWord"):
        assert field in text, field


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
        names = set(re.findall(r"\bsolar\.(\w+)", json.dumps(d["document"])))
        assert names, d["document"]
        assert names <= set(d["datasources"]["solar"]), names - set(d["datasources"]["solar"])


def test_no_style_keys_are_left(alexa):
    for d in every_screen(alexa):
        assert not [n for n in walk(d["document"]) if "style" in n]


def test_frames_hold_one_child(alexa):
    for d in every_screen(alexa):
        frames = [n for n in walk(d["document"]) if n.get("type") == "Frame"]
        assert frames
        assert all("item" in f and "items" not in f for f in frames)


def test_every_page_has_the_sky_behind_it(alexa):
    for d in every_screen(alexa):
        for item in d["document"]["mainTemplate"]["items"]:
            assert item["type"] == "Frame" and item["backgroundColor"] == "${solar.sky}"


def test_no_card_fills_are_left(alexa):
    for d in every_screen(alexa):
        assert not [n for n in walk(d["document"]) if n.get("borderRadius")]


@pytest.mark.parametrize("key", ["show", "spot"])
def test_the_scene_gets_every_parameter_it_declares(alexa, key):
    doc = alexa.build_apl_document()
    declared = {p["name"] for p in doc["graphics"][f"{key}Scene"]["parameters"]}
    component = alexa.scene_component(getattr(alexa, key.upper()))
    assert declared <= set(component), declared - set(component)
    used = set(re.findall(r"\$\{(\w+)", json.dumps(doc["graphics"][f"{key}Scene"]["items"])))
    assert used <= declared, used - declared


def every_string(node):
    for n in walk(node):
        for value in n.values():
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                yield from (v for v in value if isinstance(v, str))


def test_no_emoji_anywhere(alexa):
    alexa.routes["events"] = {"events": ["\u26a0\ufe0f MEP-803A AGS went OFFLINE (FC 0x83)",
                                         "\u2705 Kubota AGS back ONLINE"]}
    alexa.agent = (200, "All good \u2600\ufe0f \U0001F50B")
    for intent, query in (("GetBatteryIntent", None), ("GetEventsIntent", None),
                          ("AskAgentIntent", "\u26a1 now")):
        for locale in ("en-US", "es-US"):
            d = directive(ask(alexa, intent, locale=locale, query=query))
            assert not [t for t in every_string(d) if EMOJI.search(t)]


def test_emoji_leave_the_event_text_readable(alexa):
    alexa.routes["events"] = {"events": ["\u26a0\ufe0f MEP-803A AGS went OFFLINE", "Kubota stopped \u2192 AUTO"]}
    assert payload(ask(alexa, "GetEventsIntent"))["events"] == ["MEP-803A AGS went OFFLINE",
                                                                "Kubota stopped \u2192 AUTO"]


# --- the sun ---------------------------------------------------------------------------------

@pytest.mark.parametrize("day,rise,sets", [(date(2026, 11, 6), "06:09", "16:53"),
                                           (date(2026, 11, 7), "06:10", "16:52")])
def test_sun_times_are_within_two_minutes_of_open_meteo(alexa, day, rise, sets):
    for ts, expected in zip(alexa.sun_times(day), (rise, sets)):
        t = datetime.fromtimestamp(ts, alexa.SITE_TZ)
        hour, minute = map(int, expected.split(":"))
        assert abs((t.hour * 60 + t.minute) - (hour * 60 + minute)) <= 2


def test_the_sky_turns_at_the_exact_times(alexa):
    sunrise, sunset = alexa.sun_times(DAY)
    assert alexa.sky_state(sunrise - 1)["day"] is False
    assert alexa.sky_state(sunrise)["day"] is True
    assert alexa.sky_state(sunset)["day"] is True
    assert alexa.sky_state(sunset + 1)["day"] is False


def test_the_next_event(alexa):
    sunrise, sunset = alexa.sun_times(DAY)
    tomorrow = alexa.sun_times(DAY + timedelta(days=1))[0]
    assert alexa.sky_state(sunrise + 60)["next"] == sunset
    assert alexa.sky_state(sunrise - 60)["next"] == sunrise
    assert alexa.sky_state(sunset + 60)["next"] == tomorrow


@pytest.mark.parametrize("name", ["SHOW", "SPOT"])
def test_the_sun_runs_along_the_semicircle(alexa, name):
    layout = getattr(alexa, name)
    cx, r, horizon = layout["arc_cx"], layout["arc_r"], layout["horizon"]
    assert alexa.sun_xy(layout, 0) == (cx - r, horizon)
    assert alexa.sun_xy(layout, 0.5) == (cx, horizon - r)
    assert alexa.sun_xy(layout, 1) == (cx + r, horizon)
    x, y = alexa.sun_xy(layout, 0.25)
    assert abs(((x - cx) ** 2 + (y - horizon) ** 2) ** 0.5 - r) < 0.2


@pytest.mark.parametrize("prefix", ["show", "spot"])
def test_the_datasource_has_the_sun_at_rise_noon_and_set(alexa, prefix):
    layout = getattr(alexa, prefix.upper())
    sunrise, sunset = alexa.sun_times(DAY)
    for now, fraction in ((sunrise, 0), ((sunrise + sunset) / 2, 0.5), (sunset, 1)):
        data = alexa.build_apl_datasource(SOLAR, now=now)
        assert (data[f"{prefix}SunX"], data[f"{prefix}SunY"]) == alexa.sun_xy(layout, fraction)


def test_solar_noon_is_within_a_minute_of_the_top_of_the_arc(alexa):
    sunrise, sunset = alexa.sun_times(DAY)
    # 720 - 4 * lon - eqtime, with the equation of time near +16.4 minutes on this date
    noon = datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp() + 60 * (720 + 4 * 117.06 - 16.4)
    assert abs((sunrise + sunset) / 2 - noon) < 60


def test_day_shows_the_sun_and_night_the_moon(alexa):
    sunrise, sunset = alexa.sun_times(DAY)
    day = alexa.build_apl_datasource(SOLAR, now=sunrise + 3600)
    night = alexa.build_apl_datasource(SOLAR, now=sunset + 3600)
    assert (day["dayOpacity"], day["nightOpacity"]) == (1, 0)
    assert (night["dayOpacity"], night["nightOpacity"]) == (0, 1)
    assert (day["sky"], day["ground"], day["horizon"]) == ("#13306B", "#081734", "#2B4A8A")
    assert (night["sky"], night["ground"], night["horizon"]) == ("#070B1A", "#04060D", "#1C2440")
    assert day["secondary"] == "#AFC3E8" and night["secondary"] == "#8B95A5"


def test_seven_stars_at_fixed_places(alexa):
    for layout in (alexa.SHOW, alexa.SPOT):
        assert len(layout["stars"]) == 7


@pytest.mark.parametrize("now,spanish,text", [
    (datetime(2026, 11, 6, 23, 41, tzinfo=timezone.utc), False, "3:41 pm"),
    (datetime(2026, 11, 6, 23, 41, tzinfo=timezone.utc), True, "3:41 p. m."),
    (datetime(2026, 11, 6, 14, 9, tzinfo=timezone.utc), False, "6:09 am"),
    (datetime(2026, 11, 6, 14, 9, tzinfo=timezone.utc), True, "6:09 a. m."),
    (datetime(2026, 11, 7, 8, 5, tzinfo=timezone.utc), False, "12:05 am"),
    (datetime(2026, 11, 6, 20, 5, tzinfo=timezone.utc), False, "12:05 pm"),
    (datetime(2026, 7, 1, 19, 5, tzinfo=timezone.utc), False, "12:05 pm"),  # summer time
])
def test_twelve_hour_clock_in_tijuana(alexa, now, spanish, text):
    assert alexa.clock_text(now.timestamp(), spanish) == text


@pytest.mark.parametrize("spanish,offset,text", [(False, 3600, "Sunset 4:5"), (True, 3600, "Atardecer 4:5"),
                                                 (False, -3600, "Sunrise 6:"), (True, -3600, "Amanecer 6:")])
def test_the_sky_event_line(alexa, spanish, offset, text):
    sunrise, sunset = alexa.sun_times(DAY)
    now = sunrise + offset
    assert alexa.build_apl_datasource(SOLAR, spanish, now=now)["skyEvent"].startswith(text)


# --- the flows ---------------------------------------------------------------------------------

def scene_at(alexa, now=None, **fields):
    return alexa.build_apl_datasource(dict(SOLAR, **fields), now=now)


def test_the_solar_flow_needs_daylight_and_over_50_w(alexa):
    sunrise, sunset = alexa.sun_times(DAY)
    noon = (sunrise + sunset) / 2
    over = dict(mppt80PVPower=51, southArrayPVPower=0, westArrayPVPower=0)
    exact = dict(mppt80PVPower=50, southArrayPVPower=0, westArrayPVPower=0)
    assert scene_at(alexa, noon, **over)["solarFlow"] is True
    assert scene_at(alexa, noon, **over)["solarOpacity"] == 1
    assert scene_at(alexa, noon, **exact)["solarFlow"] is False
    assert scene_at(alexa, noon, **exact)["solarOpacity"] == 0
    assert scene_at(alexa, sunset + 3600, **over)["solarFlow"] is False


@pytest.mark.parametrize("gen,field,flow", [("mep", "mep803aAction", "mepFlow"),
                                            ("kubota", "kubotaAction", "kubotaFlow")])
def test_a_generator_flow_only_while_it_runs(alexa, gen, field, flow):
    assert scene_at(alexa, **{field: 9})[flow] is True
    assert scene_at(alexa, **{field: 9})[f"{gen}Opacity"] == 1
    for action in (10, 2, 5, 255):
        assert scene_at(alexa, **{field: action})[flow] is False, action
        assert scene_at(alexa, **{field: action})[f"{gen}Opacity"] == 0


def test_charge_power_per_generator(alexa):
    data = scene_at(alexa, mep803aAction=9, kubotaAction=9,
                    chargePower1=2100, chargePower2=2000, chargePower3=1800)
    assert data["mepKw"] == "4.1 kW" and data["kubotaKw"] == "1.8 kW"


def test_house_flow_and_load(alexa):
    data = scene_at(alexa, acPower1=505, acPower2=786)
    assert data["houseFlow"] is True and data["houseKw"] == "1.3 kW"
    assert data["houseLabel"] == "1.3 kW house"
    assert alexa.build_apl_datasource(dict(SOLAR, acPower1=505, acPower2=786), True)["houseLabel"] == "1.3 kW casa"


@pytest.mark.parametrize("field,value", [("mep803aAction", 9), ("kubotaAction", 9), ("mep803aMode", 1)])
def test_the_house_flow_hides_while_a_generator_runs(alexa, field, value):
    data = scene_at(alexa, acPower1=505, acPower2=786, **{field: value})
    assert data["houseFlow"] is False and data["houseOpacity"] == 0


def test_more_power_moves_the_dashes_faster_within_limits(alexa):
    assert alexa.flow_divisor(5000) < alexa.flow_divisor(1000) < alexa.flow_divisor(100)
    assert alexa.flow_divisor(0) == 60 and alexa.flow_divisor(100000) == 10


@pytest.mark.parametrize("key,period", [("show", 9), ("spot", 8)])
def test_the_dashes_animate_on_elapsed_time(alexa, key, period):
    component = alexa.scene_component(getattr(alexa, key.upper()))
    assert component["solarDash"] == "${-(elapsedTime / solar.solarDiv) % " + str(period) + "}"


# --- battery and generator states ---------------------------------------------------------------

@pytest.mark.parametrize("soc,colour,width", [(0, "#F87171", 0), (39, "#F87171", 25.7), (50, "#FBBF24", 33),
                                              (69, "#FBBF24", 45.5), (70, "#34D399", 46.2),
                                              (99, "#34D399", 65.3), (100, "#34D399", 66)])
def test_battery_fill_and_colour(alexa, soc, colour, width):
    data = scene_at(alexa, batterySOC=soc)
    assert data["batteryColor"] == colour
    assert alexa.battery_fill_width(soc, 66) == width
    assert f"h{float(width)}v" in data["showFillPath"]


def test_battery_fill_stays_inside_the_icon(alexa):
    assert alexa.battery_fill_width(130, 66) == 66 and alexa.battery_fill_width(-5, 66) == 0


@pytest.mark.parametrize("fields,word,word_es,dot", [
    (dict(mep803aMode=0, mep803aAction=10), "Off", "Apagado", "#5B6575"),
    (dict(mep803aMode=2, mep803aAction=10), "Auto", "Automático", "#FBBF24"),
    (dict(mep803aMode=1, mep803aAction=9), "Running", "Prendido", "#34D399"),
    (dict(mep803aMode=2, mep803aAction=9), "Running", "Prendido", "#34D399"),  # an exercise
    (dict(mep803aMode=1, mep803aAction=2), "Running", "Prendido", "#34D399"),  # cranking
    (dict(mep803aMode=2, mep803aAction=5), "Running", "Prendido", "#34D399"),  # cooling down
    (dict(mep803aMode=1, mep803aAction=10), "Running", "Prendido", "#34D399"),  # just requested
    (dict(mep803aMode=2, mep803aAction=10, mepState="ags_fault"), "Fault", "Falla", "#F87171"),
    (dict(mep803aMode=1, mep803aAction=9, mepFault=True), "Fault", "Falla", "#F87171"),
])
def test_generator_words(alexa, fields, word, word_es, dot):
    english = alexa.build_apl_datasource(dict(SOLAR, **fields))
    spanish = alexa.build_apl_datasource(dict(SOLAR, **fields), True)
    assert (english["mepWord"], english["mepDot"]) == (word, dot)
    assert spanish["mepWord"] == word_es


def test_the_rest_of_the_spanish(alexa):
    sunrise, _ = alexa.sun_times(DAY)
    data = alexa.build_apl_datasource(SOLAR, True, now=sunrise + 3600)
    assert data["autoText"] == "Control automático activado"
    assert alexa.build_apl_datasource(dict(SOLAR, autoGenEnabled=False), True)["autoText"] == "Control automático desactivado"
    assert (data["arrayTitle"], data["southLabel"], data["westLabel"]) == ("Solar por arreglo", "Sur", "Oeste")
    english = alexa.build_apl_datasource(SOLAR, now=sunrise + 3600)
    assert english["autoText"] == "Auto control on" and english["arrayTitle"] == "Solar by array"


def test_answer_and_events_use_the_palette(alexa):
    _, sunset = alexa.sun_times(DAY)
    alexa.current_time = lambda: sunset + 3600
    assert payload(ask(alexa, "GetEventsIntent"))["sky"] == "#070B1A"
    assert payload(ask(alexa, "AskAgentIntent", query="anything"))["secondary"] == "#8B95A5"


# --- events -------------------------------------------------------------------------------

def test_events_are_the_last_five_newest_first(alexa):
    alexa.routes["events"] = {"events": [f"e{i}" for i in range(7)]}
    assert payload(ask(alexa, "GetEventsIntent"))["events"] == ["e0", "e1", "e2", "e3", "e4"]


@pytest.mark.parametrize("locale,title,empty", [("en-US", "Recent events", "No recent events."),
                                                ("es-US", "Eventos recientes", "No hay eventos recientes.")])
def test_events_with_nothing_to_list(alexa, locale, title, empty):
    p = payload(ask(alexa, "GetEventsIntent", locale=locale))
    assert p["title"] == title and p["events"] == [empty]


# --- agent questions -------------------------------------------------------------------------

def test_an_answer_shows_the_question_the_answer_and_the_strip(alexa):
    body = ask(alexa, "AskWhyIntent", query="did it start")
    p = payload(body)
    assert p["question"] == "why did it start" and p["answer"] == "The battery is fine."
    assert p["statusLine"] == "85% · 52.5 V · 600 W"
    assert p["genLine"] == "MEP-803A: Auto · Kubota: Off"
    assert body["response"]["outputSpeech"]["text"] == "The battery is fine."


def test_the_strip_in_spanish(alexa):
    p = payload(ask(alexa, "AskPorQueIntent", locale="es-US", query="arrancó"))
    assert p["question"] == "por qué arrancó"
    assert p["genLine"] == "MEP-803A: Automático · Kubota: Apagado"


@pytest.mark.parametrize("locale,line", [("en-US", "Answer sent to your phone"),
                                         ("es-US", "Respuesta enviada a tu teléfono")])
def test_a_202_shows_the_dashboard_and_the_line(alexa, locale, line):
    alexa.agent = (202, "")
    body = ask(alexa, "AskAgentIntent", locale=locale, query="anything")
    assert payload(body)["notice"] == line and payload(body)["solarKw"] == "0.6 kW"
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
