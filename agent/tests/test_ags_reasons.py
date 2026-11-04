"""The AGS reason tables and the exercise-time decode, as pi5/app.py ships them.

app.py cannot be imported here - it wants Flask, a gateway and a Modbus
socket - so this pulls the two constant tables and the one pure function out
of the real file the way test_watchdog.py runs the real shell script. A copy
of them in this directory would only prove the copy right.
"""

import ast
import os

import pytest

import history

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "pi5", "app.py")

WANT = ("GEN_ON_REASON", "GEN_OFF_REASON", "_exercise_time",
        "REG_GENERATOR_ON_REASON", "REG_GENERATOR_OFF_REASON",
        "REG_EXERCISE_DAYS", "REG_EXERCISE_DURATION", "REG_EXERCISE_START")


@pytest.fixture(scope="module")
def app_ns():
    """Just the pieces named in WANT, executed on their own."""
    tree = ast.parse(open(APP).read())
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in WANT for t in node.targets):
            keep.append(node)
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), APP, "exec"), ns)
    missing = [n for n in WANT if n not in ns]
    assert not missing, f"app.py no longer defines {missing}"
    return ns


def test_the_registers_are_the_ones_the_spec_names(app_ns):
    assert app_ns["REG_GENERATOR_ON_REASON"] == 0x0044
    assert app_ns["REG_GENERATOR_OFF_REASON"] == 0x0045
    assert app_ns["REG_EXERCISE_DAYS"] == 0x006F
    assert app_ns["REG_EXERCISE_DURATION"] == 0x0070
    assert app_ns["REG_EXERCISE_START"] == 0x0071


def test_the_on_reason_table_is_the_spec_table(app_ns):
    assert app_ns["GEN_ON_REASON"] == {
        0: "not_on", 1: "dc_voltage_low", 2: "battery_soc_low",
        3: "ac_current_high", 4: "contact_closed", 5: "manual_on",
        6: "exercise", 7: "non_quiet_time", 8: "ext_on_via_ags",
        9: "ext_on_via_gen", 10: "unable_to_stop", 11: "ac_power_high",
        12: "dc_current_high"}


def test_the_off_reason_table_is_the_spec_table(app_ns):
    """AGS spec 2.5. A code not in here still comes through as code_N."""
    assert app_ns["GEN_OFF_REASON"] == {
        0: "not_off", 1: "dc_voltage_high", 2: "battery_soc_high",
        3: "ac_current_low", 4: "contact_opened", 5: "reached_absorp",
        6: "reached_float", 7: "manual_off", 8: "max_run_time",
        9: "max_auto_cycle", 10: "exercise_done", 11: "quiet_time",
        12: "ext_off_via_ags", 13: "safe_mode", 14: "ext_off_via_gen",
        15: "ext_shutdown", 16: "auto_off", 17: "fault", 18: "unable_to_start",
        19: "power_low", 20: "dc_current_low", 21: "ac_good"}


# On-reasons the dashboard names and history.py has no entry for, deliberately:
# _classify keeps the name as the run's kind rather than rounding it to 'auto'.
KEPT_BY_NAME = {"ext_on_via_ags", "ext_on_via_gen", "unable_to_stop",
                "ac_power_high", "dc_current_high"}


def test_every_reason_the_dashboard_can_send_is_accounted_for(app_ns):
    """The two files spell these strings independently; they must agree."""
    named = set(app_ns["GEN_ON_REASON"].values())
    handled = (set(history.REASON_KIND) | {history.REASON_VOLTAGE}
               | set(history.REASON_NONE))
    assert named - handled == KEPT_BY_NAME
    assert handled - named == set(), f"agent invents a reason: {handled - named}"


@pytest.mark.parametrize("reason", sorted(KEPT_BY_NAME))
def test_a_reason_without_an_entry_is_kept_rather_than_rounded(conn, cfg, reason):
    assert history._classify(conn, "kubota", 1000, 60, None, cfg,
                             on_reason=reason) == reason


@pytest.mark.parametrize("raw,want", [
    (540, "09:00"),        # minutes past midnight, the old assumption
    (1129, "18:49"),       # the Kubota's actual exercise, 6:49 PM
    (0, "00:00"),
    (1439, "23:59"),
    (0x1231, "18:49"),     # the same time packed as hour << 8 | minute
    (0x0900, "09:00"),
    (None, None),
    (65535, None),         # a failed read dressed as a value
    (0x183C, None),        # hour 24, minute 60: neither encoding
])
def test_the_exercise_time_decodes_or_declines(app_ns, raw, want):
    assert app_ns["_exercise_time"](raw) == want


def test_the_ambiguous_band_is_named_rather_than_papered_over(app_ns):
    """0-1439 is always read as minutes, and that is a real limit.

    A packed hour-and-minute whose hour is 0-5 lands inside that band
    (0x053B is 1339) and would be read as a time in the small hours. Both of
    this site's exercise times are outside it - 09:00 packs to 2304 and
    18:49 to 4657 - so the decode is unambiguous for the values that matter
    here. If an AGS ever reports a 3 a.m. exercise, this is the assumption
    to check first.
    """
    ex = app_ns["_exercise_time"]
    assert ex(0x0900) == "09:00" and 0x0900 > 1439
    assert ex(0x1231) == "18:49" and 0x1231 > 1439
    # Inside the band, minutes win. 0x0300 is 3:00 am packed and 12:48 as
    # minutes; the decode says 12:48 and this records that it does.
    assert ex(0x0300) == "12:48"
