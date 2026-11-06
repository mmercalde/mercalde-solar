"""The question words Alexa drops, as vps/alexa_solar.py puts them back. Read from
the real file, the way test_ags_reasons.py reads pi5/app.py: it wants Flask."""

import ast
import os

import pytest

VPS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vps", "alexa_solar.py")
SOURCE = open(VPS, encoding="utf-8").read()

ENGLISH = {"AskWhyIntent": "why", "AskHowIntent": "how", "AskWhatIntent": "what",
           "AskWhatIsIntent": "what is", "AskWhenIntent": "when", "AskWhichIntent": "which",
           "AskWillIntent": "will", "AskIsIntent": "is", "AskAreIntent": "are",
           "AskDidIntent": "did", "AskDoIntent": "do", "AskDoesIntent": "does",
           "AskWasIntent": "was", "AskShouldIntent": "should"}
SPANISH = {"AskPorQueIntent": "por qué", "AskComoIntent": "cómo", "AskCuantoIntent": "cuánto",
           "AskCuantaIntent": "cuánta", "AskCuantosIntent": "cuántos", "AskQueIntent": "qué",
           "AskCualIntent": "cuál", "AskCuandoIntent": "cuándo", "AskHayIntent": "hay",
           "AskVaIntent": "va a", "AskEstaIntent": "está"}


@pytest.fixture(scope="module")
def table():
    for node in ast.parse(SOURCE).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "AGENT_PREFIX":
            return ast.literal_eval(node.value)
    raise AssertionError("AGENT_PREFIX not found")


def test_the_table_is_the_briefs_table(table):
    assert table == {"AskAgentIntent": "", **ENGLISH, **SPANISH}


def test_no_control_intent_can_reach_the_agent(table):
    for name in ("StartGeneratorIntent", "StopGeneratorIntent", "SetGeneratorAutoIntent",
                 "SetGeneratorOffIntent", "EnableAutoGenIntent", "DisableAutoGenIntent"):
        assert name not in table


def test_every_control_and_read_intent_is_matched_before_the_table():
    """The handler checks them first, so the table cannot take one over."""
    at = SOURCE.index("elif intent in AGENT_PREFIX:")
    for name in ("GetBatteryIntent", "StartGeneratorIntent", "StopGeneratorIntent",
                 "SetGeneratorAutoIntent", "SetGeneratorOffIntent", "GetEventsIntent",
                 "EnableAutoGenIntent", "DisableAutoGenIntent"):
        assert SOURCE.index(f"'{name}'") < at


def test_the_handler_rebuilds_the_question_and_logs_it():
    assert 'question = f"{AGENT_PREFIX[intent]} {query}".strip()' in SOURCE
    assert 'print(f"DEBUG {intent}: {question}")' in SOURCE
    assert "speech = ask_agent(question, 'es' if spanish else 'en')" in SOURCE
