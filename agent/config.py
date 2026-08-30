"""Config loading for the solar agent.

One JSON file, agent/config.json (gitignored), shaped like config.example.json.
Every module takes the resulting dict; nothing reads the file more than once.
"""

import json
import logging
import os

import system

log = logging.getLogger(__name__)

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
    user = {}
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
    # The manifest owns everything that describes the system or the rules for
    # it; config.json keeps what is secret or per-install. Where both name a
    # key the manifest wins, and the stale one is said out loud rather than
    # silently ignored.
    manifest = system.load()
    overlay = system.config_overlay(manifest)
    for key, value in overlay.items():
        if key in user and user[key] != value:
            log.warning("config.json sets %s=%r, but system.yaml says %r; "
                        "the manifest wins", key, user[key], value)
        cfg[key] = value

    missing = [k for k in REQUIRED if cfg.get(k) in (None, "")]
    if missing:
        raise SystemExit("config.json is missing required keys: " + ", ".join(missing))
    clamp_to_hard_limits(cfg)
    os.makedirs(DATA_DIR, exist_ok=True)
    return cfg


def clamp_to_hard_limits(cfg):
    """Pull the configured bounds inside the guard's absolute limits.

    Config may make the permitted range tighter than the pack's hard limits.
    It may not make it looser, and an edit that tries to is a mistake worth
    saying out loud rather than silently obeying or silently ignoring. The
    guard refuses such a write anyway; this stops config and the guard from
    disagreeing about what is permitted in the first place.

    guard is imported here rather than at the top because it imports config:
    by the time load() runs, this module is built and the cycle cannot bite.
    """
    import guard

    if cfg["start_voltage_min"] < guard.HARD_START_FLOOR:
        log.warning("config start_voltage_min %s is below the hard floor %s; "
                    "using the floor", cfg["start_voltage_min"],
                    guard.HARD_START_FLOOR)
        cfg["start_voltage_min"] = guard.HARD_START_FLOOR
    if cfg["stop_voltage_max"] > guard.HARD_STOP_CEILING:
        log.warning("config stop_voltage_max %s is above the hard ceiling %s; "
                    "using the ceiling", cfg["stop_voltage_max"],
                    guard.HARD_STOP_CEILING)
        cfg["stop_voltage_max"] = guard.HARD_STOP_CEILING
    return cfg
