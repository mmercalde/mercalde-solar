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
def cfg(cfg):
    """A 2.0 h pre-dawn window, so these tests do not move when the site is
    retuned. test_system.py checks the manifest's own value reaches config."""
    return dict(cfg, predawn_hours=2.0)


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
        "sunset_ts": ts_at(cfg, "2026-08-27", 19, 24),
        "remaining_solar_wh": 0,
        # 100 kWh of pack, and ten points of charge to the volt, so a
        # kilowatt-hour is one point and 52.0 V is 40%.
        "deficit": {"deficit_wh": 9000, "needed_wh": 32000,
                    "available_wh": 23000, "capacity_wh": 100000,
                    "soc_now": 63, "soc_floor": 40.0, "floor_v": 52.0,
                    "hours": 8.2, "source": "last 14 nights"},
        "thresholds": {"mep_start": 52.0, "mep_stop": 56.0,
                       "kub_start": 52.0, "kub_stop": 56.0},
        "baseline": {"mep_start": 52.0, "mep_stop": 56.0,
                     "kub_start": 52.0, "kub_stop": 56.0},
        "run_window_h": {"mep": 2.0, "kubota": 2.0},
    }


# --- POLICY 4: the top-up, by deficit ---------------------------------------

def test_the_deficit_sets_the_target(cfg, night, model):
    """9,000 Wh short, plus 15%, is 10,350 Wh: 10.35 points of a 100 kWh pack,
    which from 63% is 73.35% and 55.3 V, rounded up to 55.5."""
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"]
    assert "deficit 9,000 Wh to sunrise above 52.0 V" in r["detail"]
    assert "needs 32,000, holds 23,000" in r["detail"]
    assert "+15% is 10,350 Wh → stop 55.5" in r["detail"]


def test_the_stop_clears_the_start_the_guard_will_require(cfg, night, model):
    """The pack is at 54.2, so the start goes to 54.4 and the stop cannot be
    the 55.5 the deficit alone asked for."""
    r = policy.solo_top_up(cfg, night, model)
    assert "raised to 56.4 to clear a start above 54.2 V by 2.0 V" in r["detail"]
    assert r["target"] == 56.4 and r["start"] == 54.4
    assert r["proposal"]["mep_start"] == 54.4 and r["proposal"]["mep_stop"] == 56.4


def test_the_other_generator_stays_at_the_owners_baseline(cfg, night, model):
    night["baseline"] = {"mep_start": 52.0, "mep_stop": 55.0,
                         "kub_start": 52.0, "kub_stop": 55.0}
    r = policy.solo_top_up(cfg, night, model)
    assert r["proposal"]["kub_start"] == 52.0 and r["proposal"]["kub_stop"] == 55.0


def test_the_run_is_made_to_begin_now(cfg, night, model):
    r = policy.solo_top_up(cfg, night, model)
    assert "start 54.4 is above the pack's 54.2 V, so the run begins now" \
        in r["detail"]
    assert r["proposal"]["mep_start"] > night["voltage"]


def test_a_pack_with_more_than_the_night_needs_does_not_fire(cfg, night, model):
    night["deficit"] = dict(night["deficit"], deficit_wh=-4000)
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"]
    assert "holds 4,000 Wh more than the night needs above 52.0 V" in r["detail"]


def test_a_deficit_too_small_to_be_worth_a_run_does_not_fire(cfg, night, model):
    night["deficit"] = dict(night["deficit"], deficit_wh=5500)
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"]
    assert "5,500 Wh is under the 6,000 Wh a run is worth" in r["detail"]
    assert "POLICY 3's pre-dawn stop" in r["detail"]


def test_an_unknown_deficit_says_why(cfg, night, model):
    night["deficit"] = {"deficit_wh": None, "reason": "pack capacity not learned"}
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"]
    assert "the deficit is not known (pack capacity not learned)" in r["detail"]


def test_a_target_is_never_above_the_ceiling(cfg, night):
    """40 kWh would want 66 V. The ceiling is the ceiling."""
    model = StubModel(pair={"gross_w": 41900, "soc_per_h": 40.0})
    night["deficit"] = dict(night["deficit"], deficit_wh=40000)
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["target"] == cfg["solo_target"] == 57.0


def test_a_full_pack_cannot_have_a_run_started_now(cfg, night, model):
    """At 55.4 V a start above the pack needs a stop over the ceiling."""
    night["voltage"] = 55.4
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"]
    assert "over the 57.0 ceiling, so no run can be started now" in r["detail"]


# --- POLICY 4: which generators, by how big the deficit is -------------------

def test_a_small_deficit_picks_the_kubota(cfg, night):
    model = StubModel(rates={"kubota": {"gross_w": 13900, "soc_per_h": 12.0},
                             "mep": {"gross_w": 16900, "soc_per_h": 15.0}})
    night["deficit"] = dict(night["deficit"], deficit_wh=7000)
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["gen"] == "kubota" and r["mode"] == "kubota"
    assert "Kubota band (deficit ≤ 8,000 Wh)" in r["detail"]
    assert r["proposal"]["kub_start"] == 54.4
    assert r["proposal"]["mep_start"] == 52.0, "the MEP is left alone"


def test_a_middling_deficit_picks_the_mep(cfg, night, model):
    r = policy.solo_top_up(cfg, night, model)
    assert r["gen"] == "mep" and "MEP band (deficit ≤ 15,000 Wh)" in r["detail"]


def test_a_large_deficit_takes_both(cfg, night):
    model = StubModel(pair={"gross_w": 31900, "soc_per_h": 30.0})
    night["deficit"] = dict(night["deficit"], deficit_wh=20000)
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["gen"] == "mep+kubota" and r["mode"] == "both"
    assert "both band (above every other)" in r["detail"]
    assert r["proposal"]["mep_start"] == 54.4 and r["proposal"]["kub_start"] == 54.4


def test_a_band_that_cannot_deliver_steps_up(cfg, night):
    """7,000 Wh is the Kubota's band, but at 3 points an hour it cannot make
    the target in two hours, so the MEP takes it."""
    model = StubModel(rates={"kubota": {"gross_w": 4900, "soc_per_h": 3.0},
                             "mep": {"gross_w": 16900, "soc_per_h": 15.0}})
    night["deficit"] = dict(night["deficit"], deficit_wh=7000)
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["gen"] == "mep"
    assert "stepped up past Kubota band" in r["detail"]
    assert "MEP band (deficit ≤ 15,000 Wh)" in r["detail"]


def test_stepping_up_can_reach_both(cfg, night):
    model = StubModel(rates={"kubota": {"gross_w": 4900, "soc_per_h": 3.0},
                             "mep": {"gross_w": 5900, "soc_per_h": 4.0}},
                      pair={"gross_w": 31900, "soc_per_h": 30.0})
    night["deficit"] = dict(night["deficit"], deficit_wh=7000)
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["gen"] == "mep+kubota"
    assert "stepped up past Kubota band" in r["detail"]
    assert "MEP band" in r["detail"]


def test_when_no_band_reaches_it_both_take_what_they_can(cfg, night):
    """The deadlock decision still stands: a target out of reach is a reason
    to ask for less, not to do nothing. Two hours at 12 points an hour takes
    63% to 87%, which is 56.7."""
    model = StubModel(rates={"kubota": {"gross_w": 4900, "soc_per_h": 3.0},
                             "mep": {"gross_w": 5900, "soc_per_h": 4.0}},
                      pair={"gross_w": 13900, "soc_per_h": 12.0})
    night["deficit"] = dict(night["deficit"], deficit_wh=20000)
    r = policy.solo_top_up(cfg, night, model)
    assert r["fires"] and r["gen"] == "mep+kubota"
    assert "no band reaches it" in r["detail"]
    assert "both together to 56.5, the most they can reach" in r["detail"]
    assert r["target"] >= 56.4, "and still clears the start"


def test_nothing_fires_when_even_both_cannot_clear_the_start(cfg, night):
    model = StubModel(rates={"mep": {"gross_w": 2900, "soc_per_h": 1.0}},
                      pair={"gross_w": 3400, "soc_per_h": 1.5})
    night["deficit"] = dict(night["deficit"], deficit_wh=20000)
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"] and "cannot reach 56.4" in r["detail"]


# --- POLICY 4 waits for the day's solar to finish ---------------------------

def daytime(cfg, night, hour=9, minute=36, remaining=8200):
    """The 9:36 am live event: a 14% cloud day with the sun still climbing."""
    night["now"] = ts_at(cfg, "2026-08-27", hour, minute)
    night["remaining_solar_wh"] = remaining
    return night


def test_the_rule_is_held_while_the_sun_is_still_producing(cfg, night, model):
    """It fired at 9:36 am and started the MEP. The day's solar goes in first."""
    r = policy.solo_top_up(cfg, daytime(cfg, night), model)
    assert not r["fires"] and r["held"]
    assert r["detail"] == ("deficit 9,000 Wh; held until 7:24 pm; "
                           "remaining solar today 8.2 kWh")
    assert policy.line(r).startswith("POLICY 4 top-up: held")


def test_the_hold_says_so_when_the_solar_model_is_not_learned(cfg, night, model):
    r = policy.solo_top_up(cfg, daytime(cfg, night, remaining=None), model)
    assert "remaining solar today not learned yet" in r["detail"]


def test_the_window_opens_at_sunset(cfg, night, model):
    assert policy.solo_top_up(cfg, daytime(cfg, night, 19, 23), model)["held"]
    assert policy.solo_top_up(cfg, daytime(cfg, night, 19, 24), model)["fires"]


def test_the_window_can_open_early(cfg, night, model):
    cfg = dict(cfg, topup_earliest="sunset-30")
    assert policy.solo_top_up(cfg, daytime(cfg, night, 18, 53), model)["held"]
    assert policy.solo_top_up(cfg, daytime(cfg, night, 18, 54), model)["fires"]


def test_the_window_can_be_a_clock_time(cfg, night, model):
    cfg = dict(cfg, topup_earliest="20:00")
    held = policy.solo_top_up(cfg, daytime(cfg, night, 19, 59), model)
    assert held["held"] and "held until 8:00 pm" in held["detail"]
    assert policy.solo_top_up(cfg, daytime(cfg, night, 20, 0), model)["fires"]


def test_a_clock_time_window_ignores_the_sunset(cfg, night, model):
    """It is the owner's hour, not the sun's, so no forecast is needed."""
    cfg = dict(cfg, topup_earliest="20:00")
    night = daytime(cfg, night, 20, 30)
    night["sunset_ts"] = None
    assert policy.solo_top_up(cfg, night, model)["fires"]


def test_the_window_closes_at_midnight(cfg, night, model):
    """The evening's decision has been made; the small hours are not the time
    to make another."""
    night["now"] = ts_at(cfg, "2026-08-28", 1, 30)
    night["sunset_ts"] = ts_at(cfg, "2026-08-28", 19, 23)
    r = policy.solo_top_up(cfg, night, model)
    assert not r["fires"] and r["held"]


def test_without_a_sunset_the_rule_is_not_held(cfg, night, model):
    """No forecast is not a reason to sit on a decision all night."""
    night = daytime(cfg, night)
    night["sunset_ts"] = None
    assert policy.solo_top_up(cfg, night, model)["fires"]


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


def test_a_stop_raise_is_never_held_by_daylight(cfg, night, model):
    """Raising a stop does not start anything, so a storm forecast is acted on
    the moment it appears."""
    night = daytime(cfg, night)
    night["tomorrow_cloud"] = 85
    r = policy.storm_stop(cfg, night)
    assert r["fires"] and not r.get("held")
    assert r["proposal"]["mep_stop"] == 57.0


def test_a_pre_charge_start_raise_waits_for_the_window(cfg, night, model):
    """A storm proposal that raised a start would run a generator in daylight,
    and that waits for the same window POLICY 4 waits for."""
    night = daytime(cfg, night)
    night["tomorrow_cloud"] = 85
    night["baseline"] = {"mep_start": 52.0, "mep_stop": 56.0,
                         "kub_start": 52.0, "kub_stop": 56.0}
    cfg = dict(cfg, default_start=54.0)     # the proposal now raises the start
    r = policy.storm_stop(cfg, night)
    assert not r["fires"] and r["held"]
    assert "the pre-charge start raise is held until 7:24 pm" in r["detail"]


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
    fired = policy.firing(rules)[0]
    write = {"applied": True, "now": dict(fired["proposal"])}
    assert policy.misses(rules, "recommend: MEP to 56.4", write) == []


def test_a_guard_refusal_is_not_the_models_miss(cfg, night, model):
    """The model proposed the rule's values; the guard is what said no."""
    rules = policy.evaluate(cfg, night, model)
    fired = policy.firing(rules)[0]
    write = {"applied": False, "refused_by": "guard",
             "would_set": dict(fired["proposal"]),
             "reason": "last write was 30 minutes ago"}
    assert policy.misses(rules, "recommend: MEP to 56.4", write) == []


def test_writing_something_else_entirely_is_still_a_miss(cfg, night, model):
    rules = policy.evaluate(cfg, night, model)
    write = {"applied": True, "now": {"mep_start": 52.0, "mep_stop": 54.5,
                                      "kub_start": 52.0, "kub_stop": 54.5}}
    assert [r["rule"] for r in policy.misses(rules, "recommend: 54.5", write)] == [4]


def test_nothing_firing_can_never_be_a_miss(cfg, night, model):
    night["deficit"] = dict(night["deficit"], deficit_wh=-4000)
    rules = policy.evaluate(cfg, night, model)
    assert policy.firing(rules) == []
    assert policy.misses(rules, "recommend: no change", None) == []


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
