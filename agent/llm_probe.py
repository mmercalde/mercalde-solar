#!/usr/bin/env python3
"""Probe the local llama-server before trusting it with the agent loop.

Checks, in order:
  1. the model endpoint answers and reports which GGUF is loaded
  2. a plain completion returns text with no <think> leakage
  3. a tool call round-trip: the model calls a tool, is given a result,
     and produces a final answer that quotes the tool's numbers
  4. host memory pressure (SPEC section 1: two 8B models is ~20 GB of 22)

Usage: python3 llm_probe.py [--url URL]
Exits non-zero if any check fails.
"""

import argparse
import json
import sys
import time
from urllib.parse import urlparse, urlunparse

import requests

import config
from llm import LLM, LLMError

PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "get_status",
        "description": "Current battery voltage and state of charge.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}]
PROBE_RESULT = {"batteryVoltage": 54.2, "battSocBM": 90}


def models_url(chat_url):
    """Turn .../v1/chat/completions into .../v1/models."""
    p = urlparse(chat_url)
    return urlunparse(p._replace(path=p.path.replace("/chat/completions", "/models")))


def check_model(url):
    r = requests.get(models_url(url), timeout=10)
    r.raise_for_status()
    body = r.json()
    entries = body.get("data") or body.get("models") or []
    names = [e.get("id") or e.get("name") or "?" for e in entries]
    print(f"  loaded: {', '.join(names) or '(none reported)'}")
    return bool(names)


def check_completion(llm):
    t = time.time()
    msg = llm.chat([{"role": "user", "content": "Reply with the single word: ready"}],
                   max_tokens=32)
    dt = time.time() - t
    text = msg.get("content", "")
    print(f"  reply: {text!r} in {dt:.1f}s")
    if "<think>" in text:
        print("  FAIL: <think> block leaked into content")
        return False
    return "ready" in text.lower()


def check_tool_round_trip(llm):
    messages = [
        {"role": "system", "content": "You manage an off-grid solar system. "
                                      "Use the tools. Quote only numbers the tools returned."},
        {"role": "user", "content": "What is the battery voltage and SOC right now?"},
    ]
    t = time.time()
    msg = llm.chat(messages, tools=PROBE_TOOL, max_tokens=256)
    calls = LLM.tool_calls(msg)
    if not calls:
        print(f"  FAIL: model answered without calling a tool: {msg.get('content')!r}")
        return False
    call_id, name, args = calls[0]
    print(f"  tool call: {name}({args})")
    if name != "get_status":
        print(f"  FAIL: called {name}, expected get_status")
        return False

    messages += [msg, {"role": "tool", "tool_call_id": call_id,
                       "name": name, "content": json.dumps(PROBE_RESULT)}]
    final = llm.chat(messages, tools=PROBE_TOOL, max_tokens=256)
    dt = time.time() - t
    text = final.get("content", "")
    print(f"  final: {text!r} in {dt:.1f}s total")
    if "54.2" not in text:
        print("  FAIL: final answer did not quote the tool's voltage")
        return False
    return True


def check_memory():
    """Report swap pressure rather than judging it; SPEC section 1 wants it reported."""
    try:
        with open("/proc/meminfo") as f:
            mi = {k.strip(): int(v.split()[0]) for k, v in
                  (line.split(":", 1) for line in f)}
    except OSError as e:
        print(f"  (no /proc/meminfo: {e})")
        return True
    total = mi["MemTotal"] / 1048576
    avail = mi["MemAvailable"] / 1048576
    sw_total = mi["SwapTotal"] / 1048576
    sw_used = (mi["SwapTotal"] - mi["SwapFree"]) / 1048576
    print(f"  RAM {total:.1f} GB total, {avail:.1f} GB available; "
          f"swap {sw_used:.1f}/{sw_total:.1f} GB used")
    if sw_used > 0.5:
        print("  NOTE: swap in use. Per SPEC section 1 the fix is stopping "
              "llama-server-abliterated.service, not shrinking the model.")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="override llm_url from config.json")
    args = ap.parse_args()

    cfg = config.load()
    if args.url:
        cfg["llm_url"] = args.url
    print(f"llama-server: {cfg['llm_url']}  model={cfg['llm_model']}")

    llm = LLM(cfg)
    checks = [
        ("model endpoint", lambda: check_model(cfg["llm_url"])),
        ("plain completion", lambda: check_completion(llm)),
        ("tool round-trip", lambda: check_tool_round_trip(llm)),
        ("host memory", check_memory),
    ]
    failed = []
    for name, fn in checks:
        print(f"\n[{name}]")
        try:
            if not fn():
                failed.append(name)
        except (requests.RequestException, LLMError) as e:
            print(f"  ERROR: {e}")
            failed.append(name)

    print()
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("All probes passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
