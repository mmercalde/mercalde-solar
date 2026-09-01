"""The per-generator top-up state machine, one test per transition.

The night behind it is 2026-08-30, when rule 4 re-derived the whole top-up
from the pack voltage on every tick and wrote three different Kubota
decisions between 7:20 and 8:55 pm.
"""

from datetime import datetime

import pytest

import history
import policy
import topup
from stubs import StubModel


def ts_at(cfg, day, hour, minute=0):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=tz).timestamp())


@pytest.fixture
def tu(cfg, tmp_path):
    return topup.TopUp(cfg, path=str(tmp_path / "topup_state.json"))


@pytest.fixture
def evening(cfg):
    """Half past seven on 2026-08-30, an hour after sunset."""
    return ts_at(cfg, "2026-08-30", 19, 30)


def observed(**kw):
    o = {"action": history.GEN_STOPPED, "mode": 2, "voltage": 53.8,
         "stop_v": 56.0, "cap_minutes": 120.0, "run": None,
         "ags_online": True, "auto_gen_enabled": True}
    o.update(kw)
    return {"kubota": o, "mep": dict(o)}


def request(tu, evening, gen="kubota", start=54.0, stop=56.0):
    tu.roll(evening)
    return tu.request(gen, start, stop, evening)


# --- idle → requested -------------------------------------------------------

def test_a_raised_start_is_the_request(tu, evening):
    moved = request(tu, evening)
    assert moved["from"] == topup.IDLE and moved["to"] == topup.REQUESTED
    assert tu.status("kubota") == topup.REQUESTED
    assert tu.entry("kubota")["start"] == 54.0
    assert tu.entry("kubota")["stop"] == 56.0
    # The other generator is untouched.
    assert tu.status("mep") == topup.IDLE


def test_a_generator_is_asked_once_a_night(tu, evening):
    request(tu, evening)
    assert tu.request("kubota", 54.6, 56.6, evening + 3600) is None
    assert tu.entry("kubota")["start"] == 54.0


def test_the_state_survives_being_reloaded(cfg, tmp_path, evening):
    path = str(tmp_path / "topup_state.json")
    first = topup.TopUp(cfg, path=path)
    first.roll(evening)
    first.request("kubota", 54.0, 56.0, evening)
    again = topup.TopUp(cfg, path=path)
    assert again.status("kubota") == topup.REQUESTED
    assert again.entry("kubota")["start"] == 54.0


# --- requested → running ----------------------------------------------------

def test_running_when_the_action_says_so(tu, evening):
    request(tu, evening)
    moved = tu.advance(observed(action=history.GEN_RUNNING), evening + 120)
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota", topup.RUNNING)]


def test_nothing_moves_while_it_is_still_starting(tu, evening):
    request(tu, evening)
    assert tu.advance(observed(), evening + 60) == []
    assert tu.status("kubota") == topup.REQUESTED


# --- requested → failed_to_start --------------------------------------------

def test_five_minutes_under_the_start_with_nothing_running(tu, evening):
    request(tu, evening)
    moved = tu.advance(observed(voltage=53.8), evening + 300)
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota",
                                                     topup.FAILED_TO_START)]
    assert tu.entry("kubota")["ags_mode"] == 2


def test_nothing_has_failed_while_the_pack_is_above_the_start(tu, evening):
    request(tu, evening, start=54.0)
    assert tu.advance(observed(voltage=55.2), evening + 3600) == []
    assert tu.status("kubota") == topup.REQUESTED


def test_four_minutes_is_not_yet_a_failure(tu, evening):
    request(tu, evening)
    assert tu.advance(observed(voltage=53.8), evening + 240) == []


# --- requested / running → stopped_by_owner ---------------------------------

def test_the_ags_going_off_before_it_starts(tu, evening):
    request(tu, evening)
    moved = tu.advance(observed(mode=0), evening + 60)
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota",
                                                     topup.STOPPED_BY_OWNER)]


def test_the_ags_going_off_mid_run(tu, evening):
    request(tu, evening)
    tu.advance(observed(action=history.GEN_RUNNING), evening + 120)
    moved = tu.advance(observed(action=history.GEN_RUNNING, mode=0),
                       evening + 600)
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota",
                                                     topup.STOPPED_BY_OWNER)]


def test_a_run_that_ends_short_of_both_is_the_owners(tu, evening):
    """2026-08-30: the Kubota ran 83 minutes, stopped at 55.4, and its stop
    was 56.6 and its cap 120 minutes. Neither was reached."""
    request(tu, evening, stop=56.6)
    tu.advance(observed(action=history.GEN_RUNNING, stop_v=56.6), evening + 60)
    moved = tu.advance(observed(stop_v=56.6, run={"stop_v": 55.4,
                                                  "duration_min": 83.0}),
                       evening + 5040)
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota",
                                                     topup.STOPPED_BY_OWNER)]
    assert "short of its 56.6 stop" in moved[0]["why"]


# --- running → done ---------------------------------------------------------

def test_a_run_that_reaches_its_stop_is_done(tu, evening):
    request(tu, evening, stop=56.0)
    tu.advance(observed(action=history.GEN_RUNNING), evening + 60)
    moved = tu.advance(observed(run={"stop_v": 56.02, "duration_min": 74.0}),
                       evening + 4500)
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota", topup.DONE)]
    assert "its stop threshold" in moved[0]["why"]


def test_a_run_that_ends_on_the_cap_is_done(tu, evening):
    request(tu, evening, stop=56.6)
    tu.advance(observed(action=history.GEN_RUNNING, stop_v=56.6), evening + 60)
    moved = tu.advance(observed(stop_v=56.6, run={"stop_v": 55.1,
                                                  "duration_min": 120.0}),
                       evening + 7300)
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota", topup.DONE)]
    assert "runtime cap" in moved[0]["why"]


# --- the night --------------------------------------------------------------

def test_the_night_is_named_by_the_sunset_that_opened_it(tu, cfg):
    assert tu.night_key(ts_at(cfg, "2026-08-30", 21)) == "2026-08-30"
    # Before the next sunset it is still that night.
    assert tu.night_key(ts_at(cfg, "2026-08-31", 2)) == "2026-08-30"
    assert tu.night_key(ts_at(cfg, "2026-08-31", 14)) == "2026-08-30"
    assert tu.night_key(ts_at(cfg, "2026-08-31", 21)) == "2026-08-31"


def test_a_settled_generator_holds_until_the_next_sunset(tu, cfg, evening):
    request(tu, evening)
    tu.advance(observed(action=history.GEN_RUNNING), evening + 60)
    tu.advance(observed(run={"stop_v": 56.02, "duration_min": 74.0}),
               evening + 4500)
    assert tu.status("kubota") == topup.DONE
    assert tu.roll(ts_at(cfg, "2026-08-31", 14)) == []      # same night
    assert tu.status("kubota") == topup.DONE
    moved = tu.roll(ts_at(cfg, "2026-08-31", 21))           # the next sunset
    assert [(m["gen"], m["to"]) for m in moved] == [("kubota", topup.IDLE)]


# --- what the owner is told -------------------------------------------------

def test_the_owner_is_told_once(tu, evening):
    request(tu, evening)
    tu.advance(observed(mode=0), evening + 60)
    assert tu.pending_notices() == ["kubota"]
    tu.mark_notified("kubota")
    assert tu.pending_notices() == []


def test_the_failure_message_names_the_ags_state(tu, evening):
    request(tu, evening)
    tu.advance(observed(voltage=53.8), evening + 300)
    text = topup.failed_to_start_message("kubota", tu.entry("kubota"), 52.0,
                                         other="mep")
    assert "Kubota didn't start — AGS state 2 (Auto)" in text
    assert "controller may need a reset" in text
    assert "start was 54.0 with the pack at 53.80 V" in text
    assert "goes back to 52.0" in text
    assert "re-evaluated with the MEP" in text


def test_the_failure_message_says_when_autogen_is_off(tu, evening):
    request(tu, evening)
    tu.advance(observed(voltage=53.8, auto_gen_enabled=False), evening + 300)
    text = topup.failed_to_start_message("kubota", tu.entry("kubota"), 52.0)
    assert "auto-gen is disabled on the dashboard" in text
    assert "held until the next sunset" in text


# --- what the rule does with it ---------------------------------------------

@pytest.fixture
def facts(cfg):
    """The 8:24 pm tick on 2026-08-30, when the third top-up was derived."""
    return {
        "now": ts_at(cfg, "2026-08-30", 20, 24),
        "voltage": 53.6, "soc": 87.0, "peak_today": 54.8,
        "sunrise_ts": ts_at(cfg, "2026-08-31", 6, 22),
        "sunset_ts": ts_at(cfg, "2026-08-30", 19, 14),
        "projection": {"reached": ts_at(cfg, "2026-08-31", 1, 45)},
        "tomorrow_cloud": 56, "remaining_solar_wh": 0,
        "data": {"autoGenEnabled": True},
        "deficit": {"deficit_wh": 6463, "needed_wh": 15220,
                    "available_wh": 8757, "capacity_wh": 100000,
                    "available_source": "learned Wh-vs-V, 10 nights",
                    "soc_now_display": 87, "floor_v": 52.0},
        "thresholds": {"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 54.0, "kub_stop": 56.0},
        "baseline": {"mep_start": 52.0, "mep_stop": 56.0,
                     "kub_start": 52.0, "kub_stop": 56.0},
        "run_window_h": {"mep": 2.0, "kubota": 2.0},
        "topup": {"night": "2026-08-30",
                  "gens": {"mep": {"state": topup.IDLE},
                           "kubota": {"state": topup.IDLE}}},
    }


def test_the_rule_fires_from_idle(cfg, facts):
    assert policy.solo_top_up(cfg, facts, StubModel())["fires"]


@pytest.mark.parametrize("state", [topup.REQUESTED, topup.RUNNING])
def test_the_rule_is_held_while_a_top_up_is_in_flight(cfg, facts, state):
    facts["topup"]["gens"]["kubota"] = {"state": state, "since": facts["now"]}
    r = policy.solo_top_up(cfg, facts, StubModel())
    assert not r["fires"] and r["held"]
    assert f"kubota is {state}" in r["detail"]
    assert "already decided" in r["detail"]
    assert "deficit 6,463 Wh" in r["detail"]


@pytest.mark.parametrize("state", [topup.DONE, topup.STOPPED_BY_OWNER])
def test_the_rule_is_held_once_the_night_has_had_its_run(cfg, facts, state):
    facts["topup"]["gens"]["kubota"] = {"state": state, "since": facts["now"]}
    r = policy.solo_top_up(cfg, facts, StubModel())
    assert not r["fires"] and r["held"]
    assert "once per night" in r["detail"]


def test_a_failed_generator_is_left_out_and_the_other_is_asked(cfg, facts):
    facts["topup"]["gens"]["kubota"] = {"state": topup.FAILED_TO_START,
                                        "since": facts["now"]}
    r = policy.solo_top_up(cfg, facts, StubModel())
    assert r["fires"] and r["gen"] == "mep"


def test_two_failures_leave_nothing_to_ask(cfg, facts):
    for gen in ("mep", "kubota"):
        facts["topup"]["gens"][gen] = {"state": topup.FAILED_TO_START,
                                       "since": facts["now"]}
    r = policy.solo_top_up(cfg, facts, StubModel())
    assert not r["fires"] and r["held"]
    assert "neither started" in r["detail"]


def test_a_disabled_autogen_holds_the_rule(cfg, facts):
    """Auto-gen was off from 7:26 pm on 2026-08-30 and the agent went on
    setting start thresholds the Pi5 would never act on."""
    facts["data"]["autoGenEnabled"] = False
    r = policy.solo_top_up(cfg, facts, StubModel())
    assert not r["fires"] and r["held"]
    assert "auto-gen is disabled" in r["detail"]


def test_the_firing_rule_carries_the_numbers(cfg, facts):
    """The band is the MEP's: the pack's 53.6 V puts it at 56% of the way up,
    and the Kubota cannot lift 22 points of charge inside its two hours. The
    shunt's 87% would have said this was easy."""
    r = policy.solo_top_up(cfg, facts, StubModel())
    assert r["deficit_wh"] == 6463 and r["margin_pct"] == 15
    assert r["padded_wh"] == 7432
    assert r["band"].startswith("MEP band")
    assert r["net_w"] is not None and r["run_minutes"] is not None


# --- the agent's side of it -------------------------------------------------

def test_the_owner_is_told_once_and_not_again(a, monkeypatch, evening):
    """Driven by the state, not by the transition: whichever call to gather()
    moved the machine, the message is still owed and still sent once."""
    sent = []
    monkeypatch.setattr("telegram.send", lambda cfg, text: sent.append(text))
    a.dry_run = False
    a.topup.roll(evening)
    a.topup.request("kubota", 54.0, 56.0, evening)
    a.topup.advance(observed(mode=0), evening + 60)

    assert a.notify_topup({}) == ["kubota"]
    assert len(sent) == 1 and "ended as the owner's" in sent[0]
    assert a.notify_topup({}) == []
    assert len(sent) == 1
