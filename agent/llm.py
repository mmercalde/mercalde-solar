"""OpenAI-compatible client for the local llama-server (Qwen3-8B).

Verified against llama-server --jinja: tool calls come back in
message.tool_calls with JSON-string arguments, finish_reason "tool_calls".
Thinking is suppressed via chat_template_kwargs.enable_thinking so the
content field is the answer and nothing else.
"""

import json
import logging
import re

import requests

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_think(text):
    """Drop any <think> block that leaks through despite enable_thinking=False."""
    return THINK_RE.sub("", text or "").strip()


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg, timeout=180):
        self.url = cfg["llm_url"]
        self.model = cfg["llm_model"]
        self.timeout = timeout

    def chat(self, messages, tools=None, temperature=0.2, max_tokens=1024):
        """One completion. Returns the assistant message dict."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            payload["tools"] = tools
        try:
            r = requests.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
        except requests.RequestException as e:
            raise LLMError(f"llama-server unreachable at {self.url}: {e}") from e
        except ValueError as e:
            raise LLMError(f"llama-server returned non-JSON: {e}") from e
        try:
            msg = body["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected completion shape: {body!r}") from e
        msg["content"] = strip_think(msg.get("content"))
        msg.setdefault("role", "assistant")
        return msg

    @staticmethod
    def tool_calls(msg):
        """Normalise message.tool_calls into [(id, name, args_dict), ...]."""
        out = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                log.warning("tool call %s had unparseable arguments: %r", fn.get("name"), raw)
                args = {}
            out.append((tc.get("id", ""), fn.get("name", ""), args))
        return out
