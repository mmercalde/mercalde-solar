"""Sending, and the escaping that the first live days needed.

Every message goes out with parse_mode=HTML so the digests can use <b>. A
plan record contains "peak 56.0 < 57.0"; Telegram read "< 5" as a start tag
and refused the whole message. Nothing retried, so the owner was told
nothing.
"""

import pytest

import telegram


class FakeAPI:
    """Telegram, as far as anything here is concerned."""

    def __init__(self, refuse_html=False):
        self.refuse_html = refuse_html
        self.posts = []

    def post(self, url, timeout=None, data=None):
        self.posts.append(data)
        bad = self.refuse_html and data.get("parse_mode") == "HTML"
        payload = ({"ok": False,
                    "description": "Bad Request: can't parse entities: "
                                   "Unsupported start tag \"5\" at byte offset 41"}
                   if bad else {"ok": True, "result": {"message_id": 1}})
        return type("R", (), {"json": lambda _s: payload})()


@pytest.fixture
def cfg_tg(cfg):
    cfg["telegram"] = {"token": "t", "chat_id": "1"}
    return cfg


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(telegram.requests, "post", fake.post)
    return fake


# --- escaping ----------------------------------------------------------------

def test_a_less_than_is_escaped():
    assert telegram.escape("peak 56.0 < 57.0") == "peak 56.0 &lt; 57.0"


def test_an_ampersand_is_escaped():
    assert telegram.escape("solar & load") == "solar &amp; load"


def test_both_together_survive():
    assert telegram.escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_escaping_leaves_ordinary_text_alone():
    text = "MEP 55.0 / 57.0 — 57.0 reachable in 1.8 h (7.5% SOC/h)"
    assert telegram.escape(text) == text


# --- sending -----------------------------------------------------------------

def test_a_message_goes_out_as_html(cfg_tg, api):
    assert telegram.send(cfg_tg, "<b>hello</b>") is True
    assert len(api.posts) == 1
    assert api.posts[0]["parse_mode"] == "HTML"


def test_a_refused_message_is_resent_as_plain_text(cfg_tg, api, caplog):
    """The whole point: the owner hears about the action either way."""
    api.refuse_html = True
    assert telegram.send(cfg_tg, "<b>Thresholds set</b>\npeak 56.0 < 57.0") is True
    assert len(api.posts) == 2
    assert "parse_mode" not in api.posts[1]
    assert api.posts[1]["text"] == "Thresholds set\npeak 56.0 < 57.0"
    assert "resending as plain text" in caplog.text


def test_the_resend_unescapes_what_was_escaped(cfg_tg, api):
    api.refuse_html = True
    telegram.send(cfg_tg, "<b>x</b>\n" + telegram.escape("peak 56.0 < 57.0"))
    assert api.posts[1]["text"] == "x\npeak 56.0 < 57.0"


def test_an_escaped_message_is_not_refused_in_the_first_place(cfg_tg, api):
    api.refuse_html = True     # only unescaped markup is refused
    text = "<b>Thresholds set</b>\n" + telegram.escape("peak 56.0 < 57.0")
    api.refuse_html = False
    assert telegram.send(cfg_tg, text) is True
    assert len(api.posts) == 1, "no retry was needed"


def test_a_message_refused_twice_reports_failure(cfg_tg, monkeypatch, caplog):
    monkeypatch.setattr(telegram, "_post",
                        lambda *a, **k: (False, "chat not found"))
    assert telegram.send(cfg_tg, "anything") is False
    assert "plain-text resend too" in caplog.text


def test_a_network_error_is_not_an_exception(cfg_tg, monkeypatch):
    import requests
    monkeypatch.setattr(telegram.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("no route")))
    assert telegram.send(cfg_tg, "anything") is False


def test_an_unconfigured_bot_drops_the_message(cfg, api):
    cfg["telegram"] = {"token": "", "chat_id": ""}
    assert telegram.send(cfg, "anything") is False
    assert api.posts == []
