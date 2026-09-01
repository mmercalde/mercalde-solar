"""The manifest: one file describing the system, read by everything."""

import textwrap

import pytest
import yaml

import config
import guard as guardmod
import prompts
import system


@pytest.fixture
def manifest():
    return system.load()


# --- the file itself ----------------------------------------------------------

def test_the_shipped_manifest_parses_and_is_complete(manifest):
    for section in system.REQUIRED:
        assert section in manifest, section


def test_a_missing_section_is_refused(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text("site: {name: x}\n")
    with pytest.raises(system.ManifestError) as e:
        system.load(str(path), force=True)
    assert "missing" in str(e.value)


def test_a_manifest_that_is_not_yaml_is_refused(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text("site: [unclosed\n")
    with pytest.raises(system.ManifestError) as e:
        system.load(str(path), force=True)
    assert "not valid YAML" in str(e.value)


def test_a_manifest_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(system.ManifestError) as e:
        system.load(str(tmp_path / "absent.yaml"), force=True)
    assert "cannot read" in str(e.value)


# --- the guard's constants and the manifest must agree -------------------------

def test_the_shipped_manifest_agrees_with_the_guard():
    assert system.check_hard_limits(guardmod.HARD_START_FLOOR,
                                    guardmod.HARD_STOP_CEILING)


def test_a_manifest_that_disagreed_would_not_start(manifest):
    """A number that can be edited in a data file is not a hard limit, so the
    guard keeps its own - and the two are checked against each other."""
    wrong = dict(manifest, battery=dict(manifest["battery"], floor_v=50.0))
    with pytest.raises(system.ManifestError) as e:
        system.check_hard_limits(52.0, 57.0, manifest=wrong)
    assert "one of them is wrong" in str(e.value)
    wrong = dict(manifest, battery=dict(manifest["battery"], ceiling_v=58.0))
    with pytest.raises(system.ManifestError) as e:
        system.check_hard_limits(52.0, 57.0, manifest=wrong)
    assert "HARD_STOP_CEILING" in str(e.value)


# --- what the manifest hands to the rest --------------------------------------

def test_the_overlay_carries_the_hardware_facts(manifest):
    o = system.config_overlay(manifest)
    assert o["lat"] == manifest["site"]["latitude"]
    assert o["dashboard_url"] == manifest["network"]["dashboard_url"]
    assert o["ags_max_run_hours"]["mep"] == \
        manifest["generators"]["max_run"]["ags_hours"]
    assert o["assumed_charge_a"] == {"mep": 140, "kubota": 80}


def test_the_overlay_carries_every_policy_constant(manifest):
    o = system.config_overlay(manifest)
    for key in manifest["policy"]:
        assert key in o, key


# --- the SYSTEM section of the prompt -----------------------------------------

def test_the_prompt_describes_the_hardware_the_manifest_lists(manifest):
    text = system.system_prompt_section(manifest)
    for inverter in manifest["inverters"]:
        assert str(inverter["slave"]) in text
        assert inverter["model"] in text
    for a in manifest["arrays"]["controllers"]:
        assert a["name"] in text and str(a["slave"]) in text
    assert str(manifest["battery"]["monitor"]["slave"]) in text
    assert manifest["battery"]["chemistry"] in text


def test_the_prompt_carries_the_magnum_note(manifest):
    text = system.system_prompt_section(manifest)
    assert "Magnum MS4048" in text and "not on Modbus" in text
    assert "only the shunt" in text


def test_the_prompt_states_the_floor_and_the_ceiling(manifest):
    text = system.system_prompt_section(manifest)
    assert "Never below 52.0 V, never above 57.0 V" in text
    assert "61.0 V and is not a target" in text


def test_the_prompt_carries_both_run_caps_and_the_exercises(manifest):
    text = system.system_prompt_section(manifest)
    assert "120 minutes by the Pi5" in text and "3 hours by the AGS" in text
    assert "every 5 days" in text and "every 3 days" in text


def test_editing_the_manifest_changes_the_prompt(manifest):
    """The point of generating it: the prompt cannot drift from the file."""
    changed = dict(manifest)
    changed["battery"] = dict(manifest["battery"], capacity_kwh_nominal=120)
    assert "120 kWh nominal" in system.system_prompt_section(changed)


def test_the_system_prompt_uses_the_generated_section():
    p = prompts.system_prompt()
    assert system.system_prompt_section() in p
    assert "MISSION" in p and "POLICY" in p


def test_the_manifest_records_the_window_the_site_is_tuned_to(manifest):
    """The manifest describes the system as it is, not as it shipped: the
    pre-dawn window was tuned to 3.0 h on the live agent and says so."""
    assert manifest["policy"]["predawn_hours"] == 3.0


def test_the_top_up_window_opens_at_the_hour_the_owner_set(manifest):
    """Nine at night, not sunset. It has to survive YAML: unquoted, 21:00 is
    read as sexagesimal and arrives as the integer 1260."""
    assert manifest["policy"]["topup_earliest"] == "21:00"
    assert config.load(config.EXAMPLE_PATH)["topup_earliest"] == "21:00"


def test_the_separation_has_one_copy(manifest):
    assert system.check_separation(guardmod.MIN_STOP_MINUS_START)
    with pytest.raises(system.ManifestError) as e:
        system.check_separation(1.5, manifest=manifest)
    assert "MIN_STOP_MINUS_START" in str(e.value)


def test_policy_takes_the_separation_from_config():
    """policy.py had its own copy of 2.0. It has none now."""
    import policy
    assert not hasattr(policy, "MIN_STOP_MINUS_START")
