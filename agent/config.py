"""Config loading for the solar agent.

One JSON file, agent/config.json (gitignored), shaped like config.example.json.
Every module takes the resulting dict; nothing reads the file more than once.
"""

import json
import os

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(AGENT_DIR, "config.json")
EXAMPLE_PATH = os.path.join(AGENT_DIR, "config.example.json")
DATA_DIR = os.path.join(AGENT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "history.sqlite")
AUDIT_LOG = os.path.join(DATA_DIR, "audit.log")

# Keys that must be present and non-empty before the agent will start.
REQUIRED = [
    "dashboard_url", "llm_url", "llm_model", "lat", "lon", "tz",
    "default_start", "default_stop",
    "start_voltage_min", "start_voltage_max",
    "stop_voltage_min", "stop_voltage_max",
]


def load(path=None):
    """Read config.json, falling back to the shipped example for any key it omits."""
    path = path or CONFIG_PATH
    with open(EXAMPLE_PATH) as f:
        cfg = json.load(f)
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            # Merge one level deep so {"telegram": {"token": "x"}} keeps chat_id's default.
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    elif path == CONFIG_PATH:
        raise SystemExit(
            "agent/config.json not found. Copy agent/config.example.json to "
            "agent/config.json and fill in the Telegram and gateway credentials."
        )
    missing = [k for k in REQUIRED if cfg.get(k) in (None, "")]
    if missing:
        raise SystemExit("config.json is missing required keys: " + ", ".join(missing))
    os.makedirs(DATA_DIR, exist_ok=True)
    return cfg
