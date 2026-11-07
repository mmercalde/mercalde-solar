"""'Will a generator run tonight?': a crossing counts only before sunrise.

On 2026-11-06 the 7:00 pm plan said 52.0 V was not projected and POLICY 4 held
a deficit of -4,550 Wh. At 7:29 pm and again at 10:03 pm the model answered that
a run was expected tonight at 6:47 pm - a crossing the 24 h walk had found the
following evening, after a day whose recharge it does not credit.
"""

import json
from datetime import datetime

import pytest

import agent as agentmod
import eval_cases
import history
import tools as toolsmod
import topup as topupmod


def ts_at(cfg, day, hour, minute=0):
    return int(datetime.strptime(f"{day} {hour}:{minute}", "%Y-%m-%d %H:%M")
               .replace(tzinfo=history.tzinfo(cfg)).timestamp())


SURPLUS = {"deficit_wh": -4550, "needed_wh": 13800, "available_wh": 18350,
           "floor_v": 52.0, "hours": 11.0}
HELD = {"rule": 4, "name": "top-up", "fires": False, "held": True,
        "detail": "deficit -4,550 Wh; held until 9:00 pm; remaining solar today 0.0 kWh"}
NOTHING_SHORT = {"rule": 4, "name": "top-up", "fires": False,
                 "detail": "the pack holds 4,550 Wh more than the night needs "
                           "above 52.0 V, so nothing is short"}


def record(conn, ts, reached, policy=(HELD,), deficit=SURPLUS, text="plan"):
    proj = {"reached": reached, "voltage": 55.4, "available_wh": 18350}
    if reached is None:
        proj["reason"] = "not reached within 24 h"
    history.record_plan(conn, f"{text}\napplied: no change",
                        {"projection": proj, "policy": list(policy),
                         "deficit": deficit}, ts=ts)


@pytest.fixture(autouse=True)
def nine_pm(cfg):
    """The window as KAMRUI has it: system.yaml's 21:00."""
    cfg["topup_earliest"] = "21:00"


@pytest.fixture
def nov6(a, conn, cfg, monkeypatch):
    """The 2026-11-06 evening: 7:26 pm tick, a crossing at 6:47 pm the next day."""
    now = ts_at(cfg, "2026-11-06", 19, 29)
    monkeypatch.setattr(agentmod.time, "time", lambda: now)
    monkeypatch.setattr(a, "run_model", lambda *x, **k: (_ for _ in ()).throw(
        AssertionError("the model must not be called")))
    record(conn, ts_at(cfg, "2026-11-06", 19, 26), ts_at(cfg, "2026-11-07", 18, 47))
    return now


# --- the replay ---------------------------------------------------------------

def test_the_nov_6_case_is_no_run_tonight(a, nov6):
    reply = a.answer("Will a generator run tonight?")
    assert reply.startswith("No generator run is expected tonight")
    assert "holds through sunrise" in reply and "4.5 kWh to spare" in reply
    assert "6:47" not in reply
    assert "decides at 9:00 pm" in reply
    assert "expected tonight because" not in reply


def test_the_nov_6_case_in_spanish(a, nov6):
    reply = a.answer("¿Va a arrancar el generador esta noche?")
    assert reply.startswith("No se espera que funcione ningún generador esta noche")
    assert "aguanta hasta el amanecer" in reply and "6:47" not in reply
    assert "decide a las 9:00 pm" in reply


def test_the_owners_own_wording_routes_too(a, nov6):
    assert a.answer("is the generator going to run?").startswith("No generator run")
    assert a.answer("Is the generator going to run tonight?").startswith("No generator run")


def test_a_crossing_before_sunrise_is_a_run(a, conn, cfg, monkeypatch):
    now = ts_at(cfg, "2026-11-06", 19, 29)
    monkeypatch.setattr(agentmod.time, "time", lambda: now)
    short = {"deficit_wh": 3000, "needed_wh": 13000, "available_wh": 10000,
             "floor_v": 52.0}
    record(conn, ts_at(cfg, "2026-11-06", 19, 26), ts_at(cfg, "2026-11-07", 3, 0),
           deficit=short)
    reply = a.gen_tonight_answer("en")
    assert reply.startswith("Yes, a generator run is expected tonight")
    assert "3:00 am" in reply and "before sunrise" in reply
    es = a.gen_tonight_answer("es")
    assert es.startswith("Sí, se espera") and "3:00 am" in es


# --- after the window: what the rule decided ---------------------------------

def test_after_nine_the_answer_is_the_rules_decision(a, conn, cfg, monkeypatch):
    record(conn, ts_at(cfg, "2026-11-06", 19, 26), ts_at(cfg, "2026-11-07", 18, 47))
    record(conn, ts_at(cfg, "2026-11-06", 21, 56), ts_at(cfg, "2026-11-07", 18, 51),
           policy=(NOTHING_SHORT,))
    now = ts_at(cfg, "2026-11-06", 22, 3)
    reply = a.gen_tonight_answer("en", now=now)
    assert reply.startswith("No generator run is expected tonight")
    assert "At 9:56 pm the top-up rule decided no run" in reply
    assert "nothing is short" in reply
    assert "decides at" not in reply and "6:51" not in reply
    es = a.gen_tonight_answer("es", now=now)
    assert "la regla de recarga decidió no arrancar" in es and "sobran 4.5 kWh" in es
    assert "decide a las" not in es


def test_after_midnight_it_is_still_last_evenings_decision(a, conn, cfg):
    record(conn, ts_at(cfg, "2026-11-06", 21, 56), None, policy=(NOTHING_SHORT,))
    # After midnight rule 4 is held again, until tonight's 9:00 pm.
    record(conn, ts_at(cfg, "2026-11-07", 0, 56), None,
           policy=(dict(HELD, detail="deficit -4,100 Wh; held until 9:00 pm"),))
    reply = a.gen_tonight_answer("en", now=ts_at(cfg, "2026-11-07", 1, 0))
    assert "At 9:56 pm the top-up rule decided no run" in reply
    assert "decides at" not in reply


def test_a_top_up_under_way_is_a_yes(a, conn, cfg):
    record(conn, ts_at(cfg, "2026-11-06", 21, 11), None,
           deficit={"deficit_wh": 7000, "needed_wh": 15000, "available_wh": 8000,
                    "floor_v": 52.0})
    a.topup.state = {"night": "2026-11-06", "gens": {
        "mep": {"state": topupmod.RUNNING, "since": ts_at(cfg, "2026-11-06", 21, 3),
                "requested_at": ts_at(cfg, "2026-11-06", 21, 0), "stop": 56.4},
        "kubota": {"state": topupmod.IDLE, "since": 0}}}
    reply = a.gen_tonight_answer("en", now=ts_at(cfg, "2026-11-06", 21, 20))
    assert reply.startswith("Yes, a generator run is on tonight")
    assert "The MEP is running on tonight's top-up, to 56.4 V" in reply


# --- the tool and the plan context -------------------------------------------

class Walk:
    """A load model whose 24 h walk crosses at a fixed time."""

    def __init__(self, reached):
        self.reached = reached

    def load_forecast(self, hours, now=None):
        return {"hours": hours, "total_wh": 9000, "by_hour": []}

    def project_voltage(self, target_v, now=None):
        return {"reached": self.reached, "at": "6:47 pm", "hours": 23.3,
                "voltage": 55.4, "available_wh": 18350}

    def overnight_deficit(self, until, floor_v=52.0, now=None):
        return SURPLUS

    def overnight_drawdown(self, month=None, now=None):
        return None

    def remaining_solar_wh(self, now=None, until=None):
        return 0


def test_the_tool_does_not_hand_over_a_crossing_past_sunrise(conn, cfg, monkeypatch):
    now = ts_at(cfg, "2026-11-06", 19, 29)
    monkeypatch.setattr(toolsmod.time, "time", lambda: now)
    t = toolsmod.Tools(conn, cfg)
    t.model = Walk(ts_at(cfg, "2026-11-07", 18, 47))
    p = t.get_load_forecast(24)["projected_52v"]
    assert p["before_sunrise"] is False
    assert "reached" not in p and "at" not in p and "hours" not in p
    assert p["reason"].startswith("not reached before sunrise (")
    assert "Sat Nov 7" in p["next_sunrise"]
    assert "6:47" not in json.dumps(p)


def test_the_tool_keeps_a_crossing_before_sunrise(conn, cfg, monkeypatch):
    now = ts_at(cfg, "2026-11-06", 19, 29)
    monkeypatch.setattr(toolsmod.time, "time", lambda: now)
    t = toolsmod.Tools(conn, cfg)
    t.model = Walk(ts_at(cfg, "2026-11-07", 3, 0))
    p = t.get_load_forecast(24)["projected_52v"]
    assert p["before_sunrise"] is True and p["at"] == "3:00 am"
    assert p["reached"] == ts_at(cfg, "2026-11-07", 3, 0)


def test_the_dated_label_carries_the_day(cfg):
    assert toolsmod.dated_clock(ts_at(cfg, "2026-11-07", 18, 47), cfg) == "6:47 pm Sat Nov 7"


def test_the_plan_context_no_longer_calls_it_tonight(a, nov6):
    ctx = a.plan_context()
    assert "IS expected tonight" not in ctx
    assert "In plain terms: No generator run is expected tonight" in ctx


def test_the_plan_record_line_drops_a_crossing_past_sunrise(a, cfg):
    from test_agent import base_facts
    f = base_facts(cfg)
    f["now"] = ts_at(cfg, "2026-11-06", 19, 26)
    f["projection"] = {"reached": ts_at(cfg, "2026-11-07", 18, 47), "at": "6:47 pm"}
    text = a.plan_record(f, "no change", "no change")
    assert "projected 52.0 V at: not before sunrise" in text
    assert "6:47" not in text


# --- what the question looks like --------------------------------------------

@pytest.mark.parametrize("q", [
    "Will a generator run tonight?", "will the generator run tonight",
    "Is the generator going to run tonight?", "is the generator going to run?",
    "is a generator going to start tonight", "Will either generator run tonight?",
    "will the MEP run tonight", "Is the Kubota gonna run tonight?",
    "will there be a generator run tonight", "is a generator run expected tonight?",
    "is the generator running tonight", "do we need the generator tonight",
    "does the generator need to run tonight",
])
def test_an_english_generator_question_is_recognised(q):
    assert agentmod.gen_tonight_language(q) == "en"


@pytest.mark.parametrize("q", [
    "¿Va a arrancar el generador esta noche?", "va a funcionar el generador",
    "¿El generador va a arrancar esta noche?", "¿Se va a encender el generador esta noche?",
    "¿Arrancará el generador esta noche?", "¿Habrá que prender el generador esta noche?",
])
def test_a_spanish_generator_question_is_recognised(q):
    assert agentmod.gen_tonight_language(q) == "es"


@pytest.mark.parametrize("q", [
    "Will a generator run tonight, and what's the shortfall?",
    "Is a generator running right now?", "is the generator running",
    "Is a generator running right now, and what is the house drawing?",
    "why did the generator run tonight", "how long will the generator run tonight",
    "what is tonight's plan", "¿Está funcionando el generador?",
    "¿Por qué arrancó el generador?", "",
])
def test_other_questions_are_not(q):
    assert not agentmod.is_gen_tonight_question(q)


def test_no_exam_case_is_answered_by_this(cfg):
    for case in eval_cases.CASES:
        assert not agentmod.is_gen_tonight_question(case["question"]), case["id"]


# --- in daylight there is no crossing to quote --------------------------------

MORNING_SHORT = {"deficit_wh": 16100, "needed_wh": 30000, "available_wh": 13900,
                 "floor_v": 52.0}


def test_in_daylight_a_crossing_tonight_is_not_a_yes(a, conn, cfg, monkeypatch):
    """7:09 am on 2026-11-07, the first answer after the fix: the walk reached
    7:03 pm without crediting a single hour of the day's sun, and the reply
    said "Yes, a generator run is expected tonight"."""
    now = ts_at(cfg, "2026-11-07", 7, 10)
    monkeypatch.setattr(agentmod.time, "time", lambda: now)
    monkeypatch.setattr(a.model, "remaining_solar_wh", lambda now=None: 9000)
    record(conn, ts_at(cfg, "2026-11-07", 7, 9), ts_at(cfg, "2026-11-07", 19, 3),
           deficit=MORNING_SHORT)
    reply = a.answer("is the generator going to run tonight?")
    assert reply.startswith("Too early to tell: today's forecast is about 9.0 kWh "
                            "of sun, which goes into the pack first; the top-up "
                            "rule decides at 9:00 pm.")
    assert "Yes" not in reply and "7:03" not in reply and "likely tonight" not in reply
    # The shortfall counts tonight's drain and none of today's charging.
    assert "16.1" not in reply and "short" not in reply
    es = a.answer("¿Va a arrancar el generador esta noche?")
    assert es.startswith("Es muy pronto para saberlo: el pronóstico de hoy es de "
                         "unos 9.0 kWh de sol, que entran primero a la batería; "
                         "la regla de recarga decide a las 9:00 pm.")
    assert "7:03" not in es and "16.1" not in es


def test_in_daylight_without_a_solar_figure_it_still_says_too_early(a, conn, cfg,
                                                                     monkeypatch):
    monkeypatch.setattr(a.model, "remaining_solar_wh", lambda now=None: None)
    record(conn, ts_at(cfg, "2026-11-07", 13, 0), None, deficit=MORNING_SHORT)
    reply = a.gen_tonight_answer("en", now=ts_at(cfg, "2026-11-07", 13, 2))
    assert reply.startswith("Too early to tell: today's sun goes into the pack "
                            "first; the top-up rule decides at 9:00 pm.")
    assert "16.1" not in reply


def test_after_sunset_the_shortfall_is_quoted(a, conn, cfg):
    """Sunset is about 4:53 pm here; by 7:00 pm the day's charging is in."""
    record(conn, ts_at(cfg, "2026-11-07", 19, 0), None, deficit=MORNING_SHORT)
    reply = a.gen_tonight_answer("en", now=ts_at(cfg, "2026-11-07", 19, 2))
    assert "16.1 kWh short of reaching sunrise above 52.0 V" in reply
    assert "Too early" not in reply and "decides at 9:00 pm" in reply
    es = a.gen_tonight_answer("es", now=ts_at(cfg, "2026-11-07", 19, 2))
    assert "le faltan 16.1 kWh" in es and "Es muy pronto" not in es


def test_before_dawn_a_crossing_is_still_a_run(a, conn, cfg):
    record(conn, ts_at(cfg, "2026-11-07", 5, 0), ts_at(cfg, "2026-11-07", 5, 45))
    reply = a.gen_tonight_answer("en", now=ts_at(cfg, "2026-11-07", 5, 2))
    assert reply.startswith("Yes, a generator run is expected tonight")
    assert "5:45 am" in reply


def test_the_tool_hands_over_no_crossing_in_daylight(conn, cfg, monkeypatch):
    now = ts_at(cfg, "2026-11-07", 7, 10)
    monkeypatch.setattr(toolsmod.time, "time", lambda: now)
    t = toolsmod.Tools(conn, cfg)
    t.model = Walk(ts_at(cfg, "2026-11-07", 19, 3))
    p = t.get_load_forecast(24)["projected_52v"]
    assert p["before_sunrise"] is None and "reached" not in p and "at" not in p
    assert p["reason"].startswith("not projected in daylight")
    assert "7:03" not in json.dumps(p)


def test_a_spanish_question_is_answered_in_spanish_whatever_ask_says(a, nov6):
    assert a.answer("¿Va a arrancar el generador esta noche?", "en").startswith("No se espera")
