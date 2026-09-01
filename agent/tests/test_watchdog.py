"""The Pi5 watchdog's own copy of the hard limits.

It runs when the agent is not answering, so it cannot ask the agent what its
limits are. The two numbers are duplicated in the shell script on purpose,
and this checks the duplicate against the original.
"""

import json
import os
import shutil
import subprocess

import pytest

import guard

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "pi5", "agent_watchdog.sh")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="bash is not available")


def within(start, stop):
    """Run the script's own check, with none of the rest of the script."""
    out = subprocess.run(
        ["bash", "-c",
         f'WATCHDOG_LIB_ONLY=1 source "{SCRIPT}"; '
         f'if within_hard_limits "{start}" "{stop}"; then echo yes; '
         f'else echo no; fi'],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1] == "yes"


def shell_value(name):
    out = subprocess.run(
        ["bash", "-c", f'WATCHDOG_LIB_ONLY=1 source "{SCRIPT}"; echo "${name}"'],
        capture_output=True, text=True, timeout=30)
    return out.stdout.strip().splitlines()[-1]


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", SCRIPT]).returncode == 0


def test_the_scripts_limits_match_the_guards():
    """Two copies, one number each. This is the test that keeps them equal."""
    assert float(shell_value("HARD_START_FLOOR")) == guard.HARD_START_FLOOR
    assert float(shell_value("HARD_STOP_CEILING")) == guard.HARD_STOP_CEILING


def test_the_everyday_defaults_are_allowed():
    assert within("52.0", "56.0")


def test_the_limits_themselves_are_allowed():
    assert within("52.0", "57.0")


@pytest.mark.parametrize("start", ["51.9", "51.0", "48.0", "0"])
def test_a_start_below_the_floor_is_refused(start):
    assert not within(start, "56.0")


@pytest.mark.parametrize("stop", ["57.1", "58.0", "60.0"])
def test_a_stop_above_the_ceiling_is_refused(stop):
    assert not within("52.0", stop)


@pytest.mark.parametrize("start,stop", [("", ""), ("x", "y"), ("52.0", ""),
                                        ("nan", "56.0")])
def test_an_unusable_value_is_refused_rather_than_guessed_at(start, stop):
    """A state file the agent wrote badly is not a reason to write anything."""
    assert not within(start, stop)


def test_the_fallbacks_the_script_ships_with_are_inside_the_limits():
    assert within(shell_value("FALLBACK_START"), shell_value("FALLBACK_STOP"))


# --- whose thresholds are these? --------------------------------------------

def ownership(state, config_json):
    """The script's own check: does /config hold what the agent last wrote?"""
    out = subprocess.run(
        ["bash", "-c",
         f'WATCHDOG_LIB_ONLY=1 source "{SCRIPT}"; '
         f'intended_matches_live "$1" "$2"; echo "rc=$?"', "_",
         state, config_json],
        capture_output=True, text=True, timeout=30)
    return int(out.stdout.strip().splitlines()[-1].split("=")[1])


def live(mep_start=52.0, mep_stop=56.0, kub_start=52.0, kub_stop=56.0):
    return json.dumps({"config": {
        "mep803a": {"startVoltage": mep_start, "stopVoltage": mep_stop},
        "kubota": {"startVoltage": kub_start, "stopVoltage": kub_stop}}})


@pytest.fixture
def state_file(tmp_path):
    def write(intended=None, **rest):
        d = {"last_seen": 1788000000, "learning_open": True}
        d.update(rest)
        if intended is not None:
            d["intended"] = intended
        p = tmp_path / "state.json"
        p.write_text(json.dumps(d))
        return str(p)
    return write


AGENTS = {"mep_start": 52.0, "mep_stop": 56.0,
          "kub_start": 54.6, "kub_stop": 56.6}


def test_the_agents_own_thresholds_may_be_reset(state_file):
    assert ownership(state_file(intended=AGENTS),
                     live(kub_start=54.6, kub_stop=56.6)) == 0


def test_thresholds_the_owner_moved_are_left_alone(state_file):
    """2026-08-30, 9:24 pm: the owner set Kubota 54.6/56.6 by hand. Six silent
    hours later a watchdog comparing only with the defaults would have
    written 52.0/56.0 over it."""
    assert ownership(state_file(intended=AGENTS),
                     live(kub_start=52.0, kub_stop=56.0)) == 1


def test_one_threshold_differing_is_enough(state_file):
    assert ownership(state_file(intended=AGENTS),
                     live(mep_stop=56.6, kub_start=54.6, kub_stop=56.6)) == 1


def test_a_tenth_of_a_volt_is_a_difference(state_file):
    assert ownership(state_file(intended=AGENTS),
                     live(kub_start=54.5, kub_stop=56.6)) == 1


def test_an_unrecorded_last_write_shows_nothing(state_file):
    """An old state file, or one written before the agent ever answered."""
    assert ownership(state_file(), live()) == 2


def test_an_unreadable_state_file_shows_nothing(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json")
    assert ownership(str(p), live()) == 2


def test_a_config_the_script_cannot_parse_shows_nothing(state_file):
    assert ownership(state_file(intended=AGENTS), '{"config": {}}') == 2
