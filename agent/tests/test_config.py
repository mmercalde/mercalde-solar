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


def test_a_start_minimum_below_the_floor_is_clamped(tmp_path, caplog):
    cfg = cfgmod.load(write(tmp_path, start_voltage_min=48.0))
    assert cfg["start_voltage_min"] == guard.HARD_START_FLOOR
    assert "below the hard floor" in caplog.text


def test_a_stop_maximum_above_the_ceiling_is_clamped(tmp_path, caplog):
    cfg = cfgmod.load(write(tmp_path, stop_voltage_max=59.5))
    assert cfg["stop_voltage_max"] == guard.HARD_STOP_CEILING
    assert "above the hard ceiling" in caplog.text


def test_both_at_once(tmp_path, caplog):
    cfg = cfgmod.load(write(tmp_path, start_voltage_min=40.0,
                            stop_voltage_max=99.0))
    assert cfg["start_voltage_min"] == 52.0 and cfg["stop_voltage_max"] == 57.0
    assert "hard floor" in caplog.text and "hard ceiling" in caplog.text


def test_a_tighter_config_is_left_alone(tmp_path, caplog):
    """Config may narrow the range. It may only never widen it."""
    cfg = cfgmod.load(write(tmp_path, start_voltage_min=53.0,
                            stop_voltage_max=56.0))
    assert cfg["start_voltage_min"] == 53.0 and cfg["stop_voltage_max"] == 56.0
    assert caplog.text == ""


def test_the_clamp_is_silent_at_the_limits_themselves(tmp_path, caplog):
    cfg = cfgmod.load(write(tmp_path, start_voltage_min=52.0,
                            stop_voltage_max=57.0))
    assert cfg["start_voltage_min"] == 52.0 and cfg["stop_voltage_max"] == 57.0
    assert caplog.text == ""


def test_the_clamp_can_be_applied_on_its_own():
    cfg = cfgmod.clamp_to_hard_limits(
        {"start_voltage_min": 10.0, "stop_voltage_max": 90.0})
    assert cfg == {"start_voltage_min": 52.0, "stop_voltage_max": 57.0}
