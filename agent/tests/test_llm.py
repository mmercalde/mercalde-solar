"""What a failed model request says it was. On 2026-11-05 two 500s from a full
context pool were logged as "unreachable", on a server that was answering."""

import json

import pytest
import requests

import llm

CFG = {"llm_url": "http://kamrui:8080/v1/chat/completions", "llm_model": "gemma"}


def response(status, body):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    return r


def failing_with(monkeypatch, outcome):
    def post(*a, **k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(llm.requests, "post", post)
    with pytest.raises(llm.LLMError) as e:
        llm.LLM(CFG, timeout=180).chat([{"role": "user", "content": "hi"}])
    return str(e.value)


@pytest.mark.parametrize("error", [requests.ReadTimeout("read timed out"),
                                   requests.ConnectTimeout("connect timed out")])
def test_a_timeout_says_timeout_and_how_long(monkeypatch, error):
    assert failing_with(monkeypatch, error) == "llama-server timed out after 180 s"


def test_an_http_error_gives_the_code_and_the_servers_reason(monkeypatch):
    body = json.dumps({"error": {"code": 500, "type": "server_error",
                                 "message": "Context size has been exceeded."}})
    assert failing_with(monkeypatch, response(500, body)) == (
        "llama-server returned HTTP 500: Context size has been exceeded.")


@pytest.mark.parametrize("body", ["<html>bad gateway</html>", "{}", "[]"])
def test_an_http_error_with_no_reason_still_gives_the_code(monkeypatch, body):
    assert failing_with(monkeypatch, response(502, body)) == "llama-server returned HTTP 502"


def test_a_refused_connection_is_the_only_thing_called_unreachable(monkeypatch):
    said = failing_with(monkeypatch, requests.ConnectionError("refused"))
    assert said.startswith(f"llama-server unreachable at {CFG['llm_url']}")


def test_a_200_that_is_not_json_is_not_called_unreachable(monkeypatch):
    said = failing_with(monkeypatch, response(200, "not json"))
    assert said.startswith("llama-server returned non-JSON")


def test_anything_else_requests_raises_is_still_an_llm_error(monkeypatch):
    said = failing_with(monkeypatch, requests.exceptions.ChunkedEncodingError("cut off"))
    assert said.startswith("llama-server request failed")
