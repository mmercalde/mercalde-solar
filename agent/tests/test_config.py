"""Config loading, and the bounds it is not allowed to widen."""

import json

import pytest

import config as cfgmod
import guard


def write(tmp_path, **over):
    with open(cfgmod.EXAMPLE_PATH) as f:
        cfg = json.load(f)
    cfg.update(over)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return str(path)


def test_the_shipped_example_is_already_inside_the_hard_limits():
    cfg = cfgmod.load(cfgmod.EXAMPLE_PATH)
    assert cfg["start_voltage_min"] >= guard.HARD_START_FLOOR
    assert cfg["stop_voltage_max"] <= guard.HARD_STOP_CEILING


def test_the_manifest_wins_over_a_stale_config_key(tmp_path, caplog):
    """The manifest owns the bounds. A config.json that still names one is
    told so out loud rather than silently ignored."""
    cfg = cfgmod.load(write(tmp_path, start_voltage_min=48.0))
    assert cfg["start_voltage_min"] == 52.0
    assert "config.json sets start_voltage_min=48.0" in caplog.text
    assert "the manifest wins" in caplog.text


def test_the_manifest_cannot_widen_past_the_battery(tmp_path, monkeypatch):
    """policy may sit inside the battery's floor and ceiling, never outside."""
    import system
    loose = dict(system.load())
    loose["policy"] = dict(loose["policy"], start_voltage_min=40.0,
                           stop_voltage_max=99.0)
    overlay = system.config_overlay(loose)
    assert overlay["start_voltage_min"] == loose["battery"]["floor_v"]
    assert overlay["stop_voltage_max"] == loose["battery"]["ceiling_v"]


def test_a_manifest_that_widened_past_the_code_is_still_clamped():
    """The last line of defence: whatever the files say, these two numbers."""
    cfg = cfgmod.clamp_to_hard_limits(
        {"start_voltage_min": 10.0, "stop_voltage_max": 90.0})
    assert cfg == {"start_voltage_min": 52.0, "stop_voltage_max": 57.0}


def test_the_shipped_manifest_sits_inside_the_hard_limits():
    cfg = cfgmod.load(cfgmod.EXAMPLE_PATH)
    assert cfg["start_voltage_min"] >= guard.HARD_START_FLOOR
    assert cfg["stop_voltage_max"] <= guard.HARD_STOP_CEILING


def test_the_hardware_facts_come_from_the_manifest(tmp_path):
    import system
    m = system.load()
    cfg = cfgmod.load(write(tmp_path))
    assert cfg["lat"] == m["site"]["latitude"]
    assert cfg["tz"] == m["site"]["timezone"]
    assert cfg["dashboard_url"] == m["network"]["dashboard_url"]
    assert cfg["assumed_charge_a"]["kubota"] == \
        m["generators"]["kubota"]["assumed_charge_a"]
    assert cfg["exercise"]["kubota_days"] == \
        m["generators"]["kubota"]["exercise"]["every_days"]


def test_the_secrets_stay_in_config_json(tmp_path):
    """The manifest is meant to be readable; the token is not in it."""
    import system
    import yaml
    raw = yaml.safe_load(open(system.MANIFEST_PATH))
    flat = json.dumps(raw).lower()
    for secret in ("token", "password", "chat_id", "secret", "apikey"):
        assert f'"{secret}"' not in flat, f"{secret} does not belong in the manifest"
