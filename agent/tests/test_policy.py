"""The numeric POLICY rules, computed in Python rather than left to the model.

The night this module exists for: peak 55.0, 52 V projected 03:08, sunrise
06:21, and "no change" on every tick.
"""

from datetime import datetime

import pytest

import history
import policy
from stubs import StubModel


def ts_at(cfg, day, hour, minute=0):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=tz).timestamp())


@pytest.fixture
def model():
    return StubModel()


@pytest.fixture
def night(cfg):
    """The first live night, as the facts reached the model at 10 pm."""
    now = ts_at(cfg, "2026-08-27", 22)
    return {
        "now": now,
        "voltage": 54.2,
        "soc": 63.0,
        "peak_today": 55.0,
        "sunrise_ts": ts_at(cfg, "2026-08-28", 6, 21),
        "projection": {"reached": ts_at(cfg, "2026-08-28", 3, 8), "at": "3:08 am"},
        "tomorrow_cloud": 20,
        "thresholds": {"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
        "run_window_h": {"mep": 2.0, "kubota": 2.0},
    }


# --- POLICY 4: the rule that was missed -------------------------------------

def test_the_solo_top_up_fires_on_the_night_it_was_missed(cfg, night, model):
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"]
    assert r["proposal"] == {"mep_start": 55.0, "mep_stop": 57.0,
                             "kub_start": 52.0, "kub_stop": 56.0}


def test_the_firing_line_shows_every_number(cfg, night, model):
    """The owner's example, in the rate that is actually the generator's."""
    assert policy.line(policy.solo_top_up(cfg, night, model)) == (
        "POLICY 4 solo top-up: FIRES (peak 55.0 < 57.0; 52 V projected 3:08 am "
        "before sunrise 6:21 am; V 54.2 ≤ 55.0 → MEP; 57.0 reachable in 1.8 h "
        "at 90 A into the pack (15.0% SOC/h))")


def test_a_peak_that_reached_the_threshold_does_not_fire(cfg, night, model):
    night["peak_today"] = 57.4
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"] and r["detail"] == "peak 57.4 ≥ 57.0"


def test_reaching_52_after_sunrise_does_not_fire(cfg, night, model):
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 9, 0)}
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"] and "not before sunrise 6:21 am" in r["detail"]


def test_no_projection_does_not_fire_and_says_why(cfg, night, model):
    night["projection"] = {"reached": None, "reason": "pack capacity not learned"}
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"]
    assert "52 V not projected (pack capacity not learned)" in r["detail"]


def test_above_the_select_voltage_picks_the_kubota(cfg, night):
    night["voltage"] = 55.6
    model = StubModel(rates={"kubota": {"a": 120.0, "soc_per_h": 20.0}})
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["gen"] == "kubota" and r["mode"] == "solo"
    assert "V 55.6 > 55.0 → Kubota" in r["detail"]
    assert r["proposal"] == {"mep_start": 52.0, "mep_stop": 56.0,
                             "kub_start": 55.0, "kub_stop": 57.0}


def test_no_observed_rate_does_not_fire(cfg, night):
    model = StubModel(rates={})
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"] and "no observed charge rate for mep" in r["detail"]
    assert "POLICY 5" in r["detail"]


# --- POLICY 4 when 57.0 is out of reach in the window -----------------------

def test_an_unreachable_target_still_fires_at_the_best_it_can_reach(cfg, night):
    """The rule's point is to top the pack up. Two hours at 10 points an hour
    takes 63% to 83%, which is 56.3 V, so it asks for 56.0."""
    model = StubModel(rates={"mep": {"a": 60.0, "soc_per_h": 10.0}})
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["mode"] == "solo-reduced" and r["target"] == 56.0
    assert "57.0 needs 2.7 h" in r["detail"] and "POLICY 5" in r["detail"]
    assert ("highest reachable in 2.0 h is 56.0 (resting curve, 0 charging "
            "runs on record), so MEP alone to 56.0") in r["detail"]
    assert r["proposal"] == {"mep_start": 54.0, "mep_stop": 56.0,
                             "kub_start": 52.0, "kub_stop": 56.0}


def test_the_reduced_target_rounds_down_to_a_half_volt(cfg, night):
    """83.5% is 56.35 V, which is 56.0, not 56.5."""
    model = StubModel(rates={"mep": {"a": 61.0, "soc_per_h": 10.25}})
    r = policy.solo_top_up(cfg, night, model)
    assert r["target"] == 56.0


def test_both_generators_are_proposed_when_one_cannot_clear_the_floor(cfg, night):
    """Under the 55.0 floor alone, but the pair makes 57.0 inside the window."""
    model = StubModel(rates={"mep": {"a": 20.0, "soc_per_h": 3.0}},
                      pair={"a": 180.0, "soc_per_h": 30.0})
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["mode"] == "both" and r["gen"] == "both"
    assert r["target"] == 57.0
    assert "55.0 is out of reach alone, but both together" in r["detail"]
    assert r["proposal"] == {"mep_start": 55.0, "mep_stop": 57.0,
                             "kub_start": 55.0, "kub_stop": 57.0}


def test_a_reachable_solo_target_is_preferred_to_running_both(cfg, night):
    """One engine to 56.0 beats two to 57.0; the pair is the last resort."""
    model = StubModel(rates={"mep": {"a": 60.0, "soc_per_h": 10.0}},
                      pair={"a": 180.0, "soc_per_h": 30.0})
    r = policy.solo_top_up(cfg, night, model)
    assert r["mode"] == "solo-reduced" and r["gen"] == "mep"


def test_the_pair_also_takes_the_best_it_can_reach(cfg, night):
    """Without this the everyday 56.0 stop could never be exceeded, no run
    would ever reach 57.0, and the charge curve could never learn its cost."""
    model = StubModel(rates={"mep": {"a": 20.0, "soc_per_h": 3.0}},
                      pair={"a": 80.0, "soc_per_h": 12.0})
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["mode"] == "both-reduced" and r["gen"] == "both"
    # 63% and two hours at 12 points an hour is 87%, which is 56.7 -> 56.5.
    assert r["target"] == 56.5
    assert "highest the pair can reach in 2.0 h is 56.5" in r["detail"]
    assert r["proposal"] == {"mep_start": 54.5, "mep_stop": 56.5,
                             "kub_start": 54.5, "kub_stop": 56.5}


def test_nothing_fires_when_neither_one_nor_both_can_do_anything_useful(cfg,
                                                                        night):
    model = StubModel(rates={"mep": {"a": 20.0, "soc_per_h": 3.0}},
                      pair={"a": 14.0, "soc_per_h": 2.0})
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"]
    assert "55.0 is out of reach alone and both together 57.0 needs" in r["detail"]
    assert "the pair cannot reach 55.0 either" in r["detail"]


def test_without_paired_history_the_pair_is_not_assumed(cfg, night):
    model = StubModel(rates={"mep": {"a": 20.0, "soc_per_h": 3.0}}, pair=None)
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"]
    assert "no observed charge rate for both generators" in r["detail"]


def test_a_start_below_the_current_voltage_is_called_out(cfg, night, model):
    """The start must clear the 57.0 stop by 2.0 V, so it cannot exceed 55.0."""
    night["voltage"] = 55.0          # picks the MEP, but 55.0 is not above it
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"]
    assert "the run begins when the pack falls to 55.0" in r["detail"]


# --- POLICY 3: storm ---------------------------------------------------------

def test_heavy_cloud_raises_both_stops(cfg, night, model):
    night["tomorrow_cloud"] = 85
    r = policy.storm_stop(cfg, night)
    assert r["fires"] and "85% daylight cloud ≥ 70%" in r["detail"]
    assert r["proposal"] == {"mep_start": 52.0, "mep_stop": 57.0,
                             "kub_start": 52.0, "kub_stop": 57.0}


def test_a_fair_forecast_does_not_raise_the_stops(cfg, night, model):
    r = policy.storm_stop(cfg, night)
    assert not r["fires"] and "20% daylight cloud < 70%" in r["detail"]


def test_stops_already_at_57_do_not_fire_again(cfg, night, model):
    night["tomorrow_cloud"] = 85
    night["thresholds"].update(mep_stop=57.0, kub_stop=57.0)
    r = policy.storm_stop(cfg, night)
    assert not r["fires"] and "already 57.0" in r["detail"]


def test_an_unknown_forecast_does_not_fire(cfg, night, model):
    night["tomorrow_cloud"] = None
    assert not policy.storm_stop(cfg, night)["fires"]


# --- POLICY 3: the pre-dawn 54.5 case ---------------------------------------

def test_a_run_landing_before_a_clear_sunrise_drops_the_stop(cfg, night, model):
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 5, 0)}
    r = policy.predawn_stop(cfg, night)
    assert r["fires"] and "1.4 h before sunrise 6:21 am ≤ 2.0 h" in r["detail"]
    assert r["proposal"]["mep_stop"] == 54.5 and r["proposal"]["kub_stop"] == 54.5


def test_three_hours_before_sunrise_is_not_shortly_before(cfg, night, model):
    r = policy.predawn_stop(cfg, night)
    assert not r["fires"] and "3.2 h before sunrise" in r["detail"]


def test_a_cloudy_sunrise_is_not_a_clear_one(cfg, night, model):
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 5, 0)}
    night["tomorrow_cloud"] = 60
    r = policy.predawn_stop(cfg, night)
    assert not r["fires"] and "not a clear sunrise" in r["detail"]


def test_policy_4_supersedes_the_pre_dawn_case(cfg, night, model):
    """Both want the stop moved; a top-up to 57.0 is not served by 54.5."""
    night["projection"] = {"reached": ts_at(cfg, "2026-08-28", 5, 0)}
    rules = policy.evaluate(cfg, night, model)
    dawn = [r for r in rules if r["name"].startswith("pre-dawn")][0]
    assert not dawn["fires"] and dawn["held"]
    assert "POLICY 4 fires" in dawn["detail"]
    assert policy.line(dawn).startswith("POLICY 3 pre-dawn stop 54.5: held")


# --- the whole evaluation ---------------------------------------------------

def test_evaluate_returns_every_rule_in_rule_order(cfg, night, model):
    rules = policy.evaluate(cfg, night, model)
    assert [r["rule"] for r in rules] == [3, 3, 4]
    assert [r["fires"] for r in rules] == [False, False, True]


def test_a_rule_that_cannot_be_evaluated_never_fires(cfg, model):
    """Missing facts must read as "no", never as an unexamined yes."""
    for r in policy.evaluate(cfg, {}, model):
        assert not r["fires"]
        assert "unknown" in r["detail"] or "not projected" in r["detail"]


# --- misses ------------------------------------------------------------------

def test_no_change_past_a_firing_rule_is_a_miss(cfg, night, model):
    rules = policy.evaluate(cfg, night, model)
    missed = policy.misses(rules, "recommend: no change - pack is healthy", None)
    assert [r["rule"] for r in missed] == [4]


def test_an_explicit_overrule_is_not_a_miss(cfg, night, model):
    rules = policy.evaluate(cfg, night, model)
    text = ("overrule POLICY 4: the MEP is due for its exercise run at 09:00.\n"
            "recommend: no change - topping up now would waste that run")
    assert policy.misses(rules, text, None) == []


def test_setting_what_the_rule_asked_for_is_not_a_miss(cfg, night, model):
    rules = policy.evaluate(cfg, night, model)
    write = {"applied": True, "now": {"mep_start": 55.0, "mep_stop": 57.0,
                                      "kub_start": 52.0, "kub_stop": 56.0}}
    assert policy.misses(rules, "recommend: MEP solo to 57.0", write) == []


def test_a_guard_refusal_is_not_the_models_miss(cfg, night, model):
    """The model proposed the rule's values; the guard is what said no."""
    rules = policy.evaluate(cfg, night, model)
    write = {"applied": False, "refused_by": "guard",
             "would_set": {"mep_start": 55.0, "mep_stop": 57.0,
                           "kub_start": 52.0, "kub_stop": 56.0},
             "reason": "last write was 30 minutes ago"}
    assert policy.misses(rules, "recommend: MEP solo to 57.0", write) == []


def test_writing_something_else_entirely_is_still_a_miss(cfg, night, model):
    rules = policy.evaluate(cfg, night, model)
    write = {"applied": True, "now": {"mep_start": 52.0, "mep_stop": 54.5,
                                      "kub_start": 52.0, "kub_stop": 54.5}}
    assert [r["rule"] for r in policy.misses(rules, "recommend: 54.5", write)] == [4]


def test_nothing_firing_can_never_be_a_miss(cfg, night, model):
    night["peak_today"] = 57.5
    assert policy.misses(policy.evaluate(cfg, night, model), "recommend: no change", None) == []


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
