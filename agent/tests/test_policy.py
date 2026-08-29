"""The numeric POLICY rules, computed in Python rather than left to the model.

The night this module exists for: peak 55.0, 52 V projected 03:08, sunrise
06:21, and "no change" on every tick.
"""

from datetime import datetime

import pytest

import history
import policy


def ts_at(cfg, day, hour, minute=0):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=tz).timestamp())


@pytest.fixture
def night(cfg):
    """The first live night, as the facts reached the model at 22:00."""
    now = ts_at(cfg, "2026-08-27", 22)
    return {
        "now": now,
        "voltage": 54.2,
        "peak_today": 55.0,
        "sunrise_ts": ts_at(cfg, "2026-08-28", 6, 21),
        "projection": {"reached": ts_at(cfg, "2026-08-28", 3, 8), "at": "03:08"},
        "tomorrow_cloud": 20,
        "thresholds": {"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
        "charge_rates": {"mep": {"v_per_h": 1.6}, "kubota": {"v_per_h": 1.0}},
        "run_window_h": {"mep": 2.0, "kubota": 2.0},
    }


# --- POLICY 4: the rule that was missed -------------------------------------

def test_the_solo_top_up_fires_on_the_night_it_was_missed(cfg, night):
    r = policy.solo_top_up(cfg, night)
    assert r["fires"]
    assert r["proposal"] == {"mep_start": 55.0, "mep_stop": 57.0,
                             "kub_start": 52.0, "kub_stop": 56.0}


def test_the_firing_line_shows_every_number(cfg, night):
    """The owner's example, to the digit."""
    assert policy.line(policy.solo_top_up(cfg, night)) == (
        "POLICY 4 solo top-up: FIRES (peak 55.0 < 57.0; 52 V projected 03:08 "
        "before sunrise 06:21; V 54.2 ≤ 55.0 → MEP; 57.0 reachable in 1.8 h "
        "at 1.6 V/h)")


def test_a_peak_that_reached_the_threshold_does_not_fire(cfg, night):
    night["peak_today"] = 57.4
    r = policy.solo_top_up(cfg, night)
    assert not r["fires"] and r["detail"] == "peak 57.4 ≥ 57.0"


def test_reaching_52_after_sunrise_does_not_fire(cfg, night):
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 9, 0)}
    r = policy.solo_top_up(cfg, night)
    assert not r["fires"] and "not before sunrise 06:21" in r["detail"]


def test_no_projection_does_not_fire_and_says_why(cfg, night):
    night["projection"] = {"reached": None, "reason": "pack capacity not learned"}
    r = policy.solo_top_up(cfg, night)
    assert not r["fires"]
    assert "52 V not projected (pack capacity not learned)" in r["detail"]


def test_above_the_select_voltage_picks_the_kubota(cfg, night):
    night["voltage"] = 55.6
    r = policy.solo_top_up(cfg, night)
    assert r["fires"] and r["gen"] == "kubota"
    assert "V 55.6 > 55.0 → Kubota" in r["detail"]
    assert r["proposal"] == {"mep_start": 52.0, "mep_stop": 56.0,
                             "kub_start": 55.0, "kub_stop": 57.0}


def test_an_unreachable_target_does_not_fire(cfg, night):
    """POLICY 5: at 1.0 V/h the MEP cannot do 54.2 -> 57.0 in two hours."""
    night["charge_rates"]["mep"] = {"v_per_h": 1.0}
    r = policy.solo_top_up(cfg, night)
    assert not r["fires"]
    assert "57.0 needs 2.8 h at 1.0 V/h but the run window is 2.0 h" in r["detail"]
    assert "POLICY 5" in r["detail"]


def test_no_observed_rate_does_not_fire(cfg, night):
    night["charge_rates"] = {"mep": None, "kubota": None}
    r = policy.solo_top_up(cfg, night)
    assert not r["fires"] and "no observed solo charge rate for MEP" in r["detail"]


def test_a_start_below_the_current_voltage_is_called_out(cfg, night):
    """The start must clear the 57.0 stop by 2.0 V, so it cannot exceed 55.0."""
    night["voltage"] = 55.0          # picks the MEP, but 55.0 is not above it
    r = policy.solo_top_up(cfg, night)
    assert r["fires"]
    assert "the run begins when the pack falls to 55.0" in r["detail"]


# --- POLICY 3: storm ---------------------------------------------------------

def test_heavy_cloud_raises_both_stops(cfg, night):
    night["tomorrow_cloud"] = 85
    r = policy.storm_stop(cfg, night)
    assert r["fires"] and "85% daylight cloud ≥ 70%" in r["detail"]
    assert r["proposal"] == {"mep_start": 52.0, "mep_stop": 57.0,
                             "kub_start": 52.0, "kub_stop": 57.0}


def test_a_fair_forecast_does_not_raise_the_stops(cfg, night):
    r = policy.storm_stop(cfg, night)
    assert not r["fires"] and "20% daylight cloud < 70%" in r["detail"]


def test_stops_already_at_57_do_not_fire_again(cfg, night):
    night["tomorrow_cloud"] = 85
    night["thresholds"].update(mep_stop=57.0, kub_stop=57.0)
    r = policy.storm_stop(cfg, night)
    assert not r["fires"] and "already 57.0" in r["detail"]


def test_an_unknown_forecast_does_not_fire(cfg, night):
    night["tomorrow_cloud"] = None
    assert not policy.storm_stop(cfg, night)["fires"]


# --- POLICY 3: the pre-dawn 54.5 case ---------------------------------------

def test_a_run_landing_before_a_clear_sunrise_drops_the_stop(cfg, night):
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 5, 0)}
    r = policy.predawn_stop(cfg, night)
    assert r["fires"] and "1.4 h before sunrise 06:21 ≤ 2.0 h" in r["detail"]
    assert r["proposal"]["mep_stop"] == 54.5 and r["proposal"]["kub_stop"] == 54.5


def test_three_hours_before_sunrise_is_not_shortly_before(cfg, night):
    r = policy.predawn_stop(cfg, night)
    assert not r["fires"] and "3.2 h before sunrise" in r["detail"]


def test_a_cloudy_sunrise_is_not_a_clear_one(cfg, night):
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 5, 0)}
    night["tomorrow_cloud"] = 60
    r = policy.predawn_stop(cfg, night)
    assert not r["fires"] and "not a clear sunrise" in r["detail"]


def test_policy_4_supersedes_the_pre_dawn_case(cfg, night):
    """Both want the stop moved; a top-up to 57.0 is not served by 54.5."""
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 5, 0)}
    rules = policy.evaluate(cfg, night)
    dawn = [r for r in rules if r["name"].startswith("pre-dawn")][0]
    assert not dawn["fires"] and dawn["held"]
    assert "POLICY 4 fires" in dawn["detail"]
    assert policy.line(dawn).startswith("POLICY 3 pre-dawn stop 54.5: held")


# --- the whole evaluation ---------------------------------------------------

def test_evaluate_returns_every_rule_in_rule_order(cfg, night):
    rules = policy.evaluate(cfg, night)
    assert [r["rule"] for r in rules] == [3, 3, 4]
    assert [r["fires"] for r in rules] == [False, False, True]


def test_a_rule_that_cannot_be_evaluated_never_fires(cfg):
    """Missing facts must read as "no", never as an unexamined yes."""
    for r in policy.evaluate(cfg, {}):
        assert not r["fires"]
        assert "unknown" in r["detail"] or "not projected" in r["detail"]


# --- misses ------------------------------------------------------------------

def test_no_change_past_a_firing_rule_is_a_miss(cfg, night):
    rules = policy.evaluate(cfg, night)
    missed = policy.misses(rules, "recommend: no change - pack is healthy", None)
    assert [r["rule"] for r in missed] == [4]


def test_an_explicit_overrule_is_not_a_miss(cfg, night):
    rules = policy.evaluate(cfg, night)
    text = ("overrule POLICY 4: the MEP is due for its exercise run at 09:00.\n"
            "recommend: no change - topping up now would waste that run")
    assert policy.misses(rules, text, None) == []


def test_setting_what_the_rule_asked_for_is_not_a_miss(cfg, night):
    rules = policy.evaluate(cfg, night)
    write = {"applied": True, "now": {"mep_start": 55.0, "mep_stop": 57.0,
                                      "kub_start": 52.0, "kub_stop": 56.0}}
    assert policy.misses(rules, "recommend: MEP solo to 57.0", write) == []


def test_a_guard_refusal_is_not_the_models_miss(cfg, night):
    """The model proposed the rule's values; the guard is what said no."""
    rules = policy.evaluate(cfg, night)
    write = {"applied": False, "refused_by": "guard",
             "would_set": {"mep_start": 55.0, "mep_stop": 57.0,
                           "kub_start": 52.0, "kub_stop": 56.0},
             "reason": "last write was 30 minutes ago"}
    assert policy.misses(rules, "recommend: MEP solo to 57.0", write) == []


def test_writing_something_else_entirely_is_still_a_miss(cfg, night):
    rules = policy.evaluate(cfg, night)
    write = {"applied": True, "now": {"mep_start": 52.0, "mep_stop": 54.5,
                                      "kub_start": 52.0, "kub_stop": 54.5}}
    assert [r["rule"] for r in policy.misses(rules, "recommend: 54.5", write)] == [4]


def test_nothing_firing_can_never_be_a_miss(cfg, night):
    night["peak_today"] = 57.5
    assert policy.misses(policy.evaluate(cfg, night), "recommend: no change", None) == []


@pytest.mark.parametrize("text", [
    "overrule POLICY 4: reason",
    "Overrule policy 4 - reason",
    "OVERRULED: POLICY 4 because the pack is fine",
    "overruling policy 4: reason",
])
def test_the_overrule_line_is_recognised_however_it_is_written(text):
    assert policy.overruled(text) == {4}


def test_merely_mentioning_a_rule_is_not_an_overrule():
    assert policy.overruled("POLICY 4 says to top up, so I will.") == set()
