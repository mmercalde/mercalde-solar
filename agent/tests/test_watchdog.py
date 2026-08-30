"""The Pi5 watchdog's own copy of the hard limits.

It runs when the agent is not answering, so it cannot ask the agent what its
limits are. The two numbers are duplicated in the shell script on purpose,
and this checks the duplicate against the original.
"""

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
