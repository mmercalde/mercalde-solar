"""The fuel model: interpolation, pricing a closed run, and the backfill.

Measurement only. Nothing here should be able to move a threshold, and
test_the_fuel_model_decides_nothing at the end is the test that says so.
"""

from datetime import datetime

import pytest

import fuel
import history
# `night` and `model` are the POLICY 4 fixtures: one evening's facts and the
# stated physics. Reused rather than rebuilt so this file cannot drift from
# what test_policy.py says the rule does.
from test_policy import model, night          # noqa: F401


def ts_at(cfg, day, hour, minute=0):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=tz).timestamp())


KUB = [[0.25, 0.16], [0.50, 0.32], [0.75, 0.51], [1.00, 0.64]]
MEP = [[0.50, 0.55], [1.00, 0.99]]


# --- interpolation ------------------------------------------------------------

def test_a_point_on_the_curve_is_itself():
    for frac, gal in KUB:
        assert fuel.gal_per_hour(KUB, frac) == pytest.approx(gal)


def test_between_two_points_is_linear():
    """Halfway from 0.50 to 0.75 is halfway from 0.32 to 0.51."""
    assert fuel.gal_per_hour(KUB, 0.625) == pytest.approx(0.415)
    assert fuel.gal_per_hour(MEP, 0.75) == pytest.approx(0.77)


def test_a_two_point_curve_interpolates_across_its_whole_span():
    assert fuel.gal_per_hour(MEP, 0.60) == pytest.approx(0.638)
    assert fuel.gal_per_hour(MEP, 0.90) == pytest.approx(0.902)


def test_below_the_first_point_is_flat():
    """A diesel's consumption flattens into its idle draw; it does not fall
    to zero, and extrapolating the first segment down would say it does."""
    assert fuel.gal_per_hour(KUB, 0.10) == 0.16
    assert fuel.gal_per_hour(KUB, 0.0) == 0.16
    assert fuel.gal_per_hour(MEP, 0.20) == 0.55


def test_above_the_last_point_is_flat():
    assert fuel.gal_per_hour(KUB, 1.00) == 0.64
    assert fuel.gal_per_hour(MEP, 1.00) == 0.99


def test_above_full_load_is_allowed_and_stays_flat():
    """load_fraction may exceed 1.0 - gross_w is measured, not capped - and
    the curve is bounded by the engine, not by the last segment's slope."""
    for x in (1.01, 1.4, 3.0):
        assert fuel.gal_per_hour(KUB, x) == 0.64
        assert fuel.gal_per_hour(MEP, x) == 0.99


def test_a_single_point_curve_is_that_value_everywhere():
    """One measurement says one thing, at every load."""
    one = [[0.75, 0.5]]
    for x in (0.0, 0.5, 0.75, 1.0, 2.0):
        assert fuel.gal_per_hour(one, x) == 0.5


def test_an_empty_curve_and_an_unknown_load_say_nothing():
    assert fuel.gal_per_hour([], 0.5) is None
    assert fuel.gal_per_hour(None, 0.5) is None
    assert fuel.gal_per_hour(KUB, None) is None


def test_the_points_need_not_be_given_in_order():
    assert fuel.gal_per_hour(list(reversed(KUB)), 0.625) == pytest.approx(0.415)


# --- one run -------------------------------------------------------------------

def test_a_run_is_priced_at_its_gross_over_rated(cfg):
    """3,500 W out of a 7,000 W Kubota is half load, which the manual puts at
    0.32 gal/h; ninety minutes of it is 0.48 gal."""
    assert fuel.load_fraction(cfg, "kubota", 3500) == pytest.approx(0.5)
    assert fuel.gallons(cfg, "kubota", 3500, 90) == pytest.approx(0.48)


def test_the_mep_is_priced_on_its_own_curve(cfg):
    """10 kW out of a 10 kW MEP is full load: 0.99 gal/h, 40 min is 0.66."""
    assert fuel.gallons(cfg, "mep", 10000, 40) == pytest.approx(0.66)


def test_a_run_with_no_gross_is_not_priced_at_nothing(cfg):
    """It burned fuel. Recording nought would make the totals quietly wrong
    instead of visibly incomplete."""
    assert fuel.gallons(cfg, "kubota", None, 90) is None
    assert fuel.gallons(cfg, "kubota", 3500, None) is None


def test_a_generator_with_no_curve_is_not_priced(cfg):
    assert fuel.gallons(dict(cfg, fuel={}), "kubota", 3500, 90) is None


# --- what it costs -------------------------------------------------------------

def test_with_no_price_the_cost_is_simply_absent(cfg):
    """Unset is normal and must never be an error."""
    assert fuel.price(cfg) is None
    assert fuel.cost(cfg, 1.5) is None
    assert fuel.phrase(cfg, 1.24) == "~1.2 gal"


def test_with_a_price_the_phrase_carries_it(cfg):
    priced = dict(cfg, diesel_price_per_gal=4.10)
    assert fuel.cost(priced, 1.5) == 6.15
    assert fuel.phrase(priced, 1.24) == "~1.2 gal ≈ $5.08"


@pytest.mark.parametrize("bad", [None, "", "abc", 0, -1])
def test_an_unusable_price_is_treated_as_unset(cfg, bad):
    assert fuel.price(dict(cfg, diesel_price_per_gal=bad)) is None


def test_nothing_burned_says_nothing(cfg):
    assert fuel.phrase(cfg, None) is None


# --- whose watts were they -------------------------------------------------------

def test_a_minute_run_alone_is_all_its_own():
    assert fuel.attribute([4000, 4000, 4000], [False] * 3, 0.4) == 4000


def test_a_shared_minute_is_split():
    """9,800 W with both turning, and the Kubota is 40% of the pair."""
    assert fuel.attribute([9800] * 4, [True] * 4, 0.4) == pytest.approx(3920)


def test_a_run_that_was_alone_and_then_shared_is_a_mean_of_both(cfg):
    """The 2026-08-30 shape: the Kubota ran alone, then the MEP joined it.
    Two minutes at 5,000 to itself and two at 10,000 shared four ways gives
    (5000 + 5000 + 4000 + 4000) / 4."""
    got = fuel.attribute([5000, 5000, 10000, 10000],
                         [False, False, True, True], 0.4)
    assert got == pytest.approx(4500)


def test_an_unsplittable_shared_run_is_left_unattributed():
    """Better a run priced on the system figure and known to over-read than
    one split on nothing."""
    assert fuel.attribute([9800] * 4, [True] * 4, None) is None
    assert fuel.attribute([9800] * 4, [True] * 4, 0) is None


def test_a_solo_run_needs_no_share_at_all():
    assert fuel.attribute([4000] * 3, [False] * 3, None) == 4000


def test_nothing_to_average_is_nothing():
    assert fuel.attribute([], [], 0.4) is None
    assert fuel.attribute(None, None, 0.4) is None


def learned_solo(conn, cfg, mep_w=12000, kub_w=6000, n=3):
    """Solo runs long enough and recent enough for charge_rate to use."""
    for i in range(n):
        add_run(conn, "mep", ts_at(cfg, f"2026-08-{10 + i:02d}", 2), 60, mep_w)
        add_run(conn, "kubota", ts_at(cfg, f"2026-08-{10 + i:02d}", 5), 60, kub_w)


def test_the_share_comes_from_learned_solo_rates(conn, cfg):
    """Learned, not rated: what the two actually put out is the better guide
    to how they divided a minute."""
    learned_solo(conn, cfg)
    share, basis = fuel.solo_share(conn, cfg, now=ts_at(cfg, "2026-08-20", 22))
    assert basis == "learned solo gross"
    assert share["mep"] == pytest.approx(2 / 3)
    assert share["kubota"] == pytest.approx(1 / 3)


def test_without_learned_rates_the_share_falls_back_to_rated_watts(conn, cfg):
    share, basis = fuel.solo_share(conn, cfg)
    assert basis == "rated watts"
    assert share["mep"] == pytest.approx(10000 / 17000)
    assert share["kubota"] == pytest.approx(7000 / 17000)


def test_one_learned_rate_is_not_enough_to_split_on(conn, cfg):
    """A share needs both halves; one engine's runs alone cannot say how the
    pair divided a minute."""
    for i in range(3):
        add_run(conn, "mep", ts_at(cfg, f"2026-08-{10 + i:02d}", 2), 60, 12000)
    _share, basis = fuel.solo_share(conn, cfg, now=ts_at(cfg, "2026-08-20", 22))
    assert basis == "rated watts"


def test_with_neither_basis_there_is_no_share(conn, cfg):
    _share, basis = fuel.solo_share(conn, dict(cfg, fuel={}))
    assert basis.startswith("no learned solo rate")
    assert fuel.solo_share(conn, dict(cfg, fuel={}))[0] is None


def test_pricing_prefers_the_attributed_figure(cfg):
    row = {"gross_w": 9800, "gross_attr_w": 3920}
    assert fuel.gross_for_pricing(_Row(row)) == 3920
    assert fuel.gross_for_pricing(_Row({"gross_w": 4000,
                                        "gross_attr_w": None})) == 4000
    assert fuel.gross_for_pricing(_Row({"gross_w": 4000})) == 4000


class _Row(dict):
    """sqlite3.Row exposes keys(); a dict does not do it the same way."""
    def keys(self):
        return list(dict.keys(self))


# --- the backfill ---------------------------------------------------------------

def add_run(conn, gen, start_ts, minutes, gross_w, fuel_gal=None, kind="auto"):
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, gross_w, "
        "fuel_gal, solo, kind) VALUES (?,?,?,?,?,?,1,?)",
        (gen, start_ts, start_ts + int(minutes * 60), minutes, gross_w,
         fuel_gal, kind))
    conn.commit()


def test_the_backfill_prices_every_run_that_has_the_inputs(conn, cfg):
    add_run(conn, "kubota", 1000, 90, 3500)
    add_run(conn, "mep", 2000, 40, 10000)
    filled, skipped = fuel.backfill(conn, cfg)
    assert (filled, skipped) == (2, 0)
    rows = {r["gen"]: r["fuel_gal"] for r in
            conn.execute("SELECT gen, fuel_gal FROM gen_runs")}
    assert rows["kubota"] == pytest.approx(0.48)
    assert rows["mep"] == pytest.approx(0.66)


def test_running_the_backfill_twice_changes_nothing(conn, cfg):
    """Idempotent: it only ever looks at rows where fuel_gal IS NULL."""
    add_run(conn, "kubota", 1000, 90, 3500)
    add_run(conn, "mep", 2000, 40, 10000)
    assert fuel.backfill(conn, cfg) == (2, 0)
    before = conn.execute("SELECT id, fuel_gal FROM gen_runs "
                          "ORDER BY id").fetchall()
    assert fuel.backfill(conn, cfg) == (0, 0), "nothing left to do"
    after = conn.execute("SELECT id, fuel_gal FROM gen_runs "
                         "ORDER BY id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_the_backfill_leaves_a_run_it_cannot_price_alone(conn, cfg):
    add_run(conn, "kubota", 1000, 90, None)
    assert fuel.backfill(conn, cfg) == (0, 0)
    assert conn.execute("SELECT fuel_gal FROM gen_runs").fetchone()[0] is None


def test_the_backfill_does_not_overwrite_a_figure_already_there(conn, cfg):
    """A tank-fill calibration could put a measured figure in one of these
    rows by hand, and the backfill must not walk over it."""
    add_run(conn, "kubota", 1000, 90, 3500, fuel_gal=0.9)
    assert fuel.backfill(conn, cfg) == (0, 0)
    assert conn.execute("SELECT fuel_gal FROM gen_runs").fetchone()[0] == 0.9


# --- the column ------------------------------------------------------------------

def test_the_column_arrives_on_a_database_that_predates_it(conn, cfg, tmp_path):
    """ALTER TABLE, guarded, so re-running the schema is safe."""
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE gen_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "gen TEXT NOT NULL, start_ts INTEGER NOT NULL, stop_ts INTEGER, "
                "duration_min REAL, start_v REAL, stop_v REAL, rate_v_per_h REAL, "
                "rate_a REAL, solo INTEGER, kind TEXT)")
    old.commit()
    old.close()
    c = history.connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(gen_runs)")}
    assert {"load_w", "gross_w", "fuel_gal"} <= cols
    history.connect(path).close()      # again: the guard makes it a no-op
    c.close()


# --- and it decides nothing --------------------------------------------------------

def test_the_fuel_model_decides_nothing(cfg, night, model):
    """Fuel-optimal generator selection is out of scope for this commit, so
    the figure is reported and never consulted. Taking the whole model away,
    or changing what a gallon costs, must leave the same decision behind.

    If a later commit does want cost to choose a generator, this is the test
    to delete on purpose rather than the one to quietly work around.
    """
    import policy
    decided = ("proposal", "gen", "target", "mode", "start", "deficit_wh")

    with_fuel = policy.solo_top_up(cfg, dict(night), model)
    without = policy.solo_top_up(dict(cfg, fuel={}), dict(night), model)
    dear = policy.solo_top_up(dict(cfg, diesel_price_per_gal=99.0),
                              dict(night), model)

    assert with_fuel["fires"]
    for key in decided:
        assert with_fuel.get(key) == without.get(key), key
        assert with_fuel.get(key) == dear.get(key), key
    # It is reported, though, and only when there is a model to report from.
    assert with_fuel.get("fuel_gal") is not None
    assert "fuel_gal" not in without or without.get("fuel_gal") is None


def test_a_planned_run_carries_its_estimate_into_the_plan_record(cfg, night,
                                                                  model):
    """Labelled an estimate wherever it is printed: the hours come from a
    learned rate and the gallons from a published curve read at a
    shunt-measured fraction, so it is an estimate twice over."""
    import policy
    r = policy.solo_top_up(cfg, dict(night), model)
    assert r["fires"]
    assert "est. ~" in r["detail"] and "of diesel" in r["detail"]
    assert policy.numbers_line([r]).count("est. ~") == 1


# --- and it reaches the three places the owner reads ----------------------------

def test_a_closed_run_is_priced_as_it_is_written(conn, cfg):
    """_close_run computes it once, at close, from the gross that run
    delivered - not later from an average of something else."""
    base = ts_at(cfg, "2026-08-20", 22)
    rows = [{"ts": base + i * 60, "battery_v": 53.0 + i * 0.02,
             "batt_current": 60.0, "ac_power1": 500, "ac_power2": 500,
             "mep_action": history.GEN_STOPPED,
             "kub_action": history.GEN_RUNNING,
             "kub_mode": 2, "mep_mode": 2, "batt_power": 3000}
            for i in range(91)]

    class Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)

    run = {"start_ts": base, "start_v": 53.0, "currents": [60.0], "loads": [1000],
           "gross": [3500.0], "solo": True, "mode": 2}
    history._close_run(conn, cfg, "kubota", run, Row(rows[-1]))
    r = conn.execute("SELECT gross_w, duration_min, fuel_gal "
                     "FROM gen_runs").fetchone()
    assert r["gross_w"] == 3500 and r["duration_min"] == 90
    assert r["fuel_gal"] == pytest.approx(0.48)


def test_the_runtime_tool_serves_summed_figures(conn, cfg):
    """Named numbers, already added up. The model has invented a voltage
    from a prompt before now; twenty rows of arithmetic is exactly the kind
    of thing it would do confidently and wrongly."""
    import tools as toolsmod
    now = ts_at(cfg, "2026-08-20", 22)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-20", 2), 90, 3500, 0.48)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-11", 2), 60, 3500, 0.32)
    add_run(conn, "kubota", ts_at(cfg, "2026-07-15", 2), 60, 3500, 0.32)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-20", 9), 30, 3500, 0.16,
            kind="exercise")
    t = toolsmod.Tools(conn, cfg)
    f = t.fuel_totals(now=now)["kubota"]
    assert f["hours_today"] == 1.5 and f["fuel_today_gal"] == pytest.approx(0.48)
    assert f["hours_mtd"] == 2.5 and f["fuel_mtd_gal"] == pytest.approx(0.80)
    assert f["fuel_today_unpriced_runs"] == 0


def test_a_run_with_no_figure_makes_the_total_say_so(conn, cfg):
    now = ts_at(cfg, "2026-08-20", 22)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-20", 2), 90, 3500, 0.48)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-20", 5), 60, None, None)
    import tools as toolsmod
    f = toolsmod.Tools(conn, cfg).fuel_totals(now=now)["kubota"]
    assert f["fuel_today_gal"] == pytest.approx(0.48)
    assert f["fuel_today_unpriced_runs"] == 1, "the total is short and says so"
    assert f["hours_today"] == 2.5, "hours are complete either way"


def test_the_digest_appends_fuel_to_a_run_line(a, cfg, conn, monkeypatch):
    now = ts_at(cfg, "2026-08-20", 7)
    add_run(conn, "kubota", now - 5 * 3600, 84, 3500, 1.24)
    monkeypatch.setattr(a, "gather", lambda *ar, **k: dict(
        __import__("test_agent").base_facts(cfg), now=now))
    monkeypatch.setattr(a, "reference_projection", lambda n: (None, None))
    text = a.digest(evening=False)
    line = [l for l in text.splitlines() if l.startswith("kubota ran")]
    assert len(line) == 1 and "84 min" in line[0] and "~1.2 gal" in line[0]


def test_the_digest_wording_is_unchanged_when_nothing_ran(a, cfg, monkeypatch):
    now = ts_at(cfg, "2026-08-20", 7)
    monkeypatch.setattr(a, "gather", lambda *ar, **k: dict(
        __import__("test_agent").base_facts(cfg), now=now))
    monkeypatch.setattr(a, "reference_projection", lambda n: (None, None))
    assert "no generator runs overnight" in a.digest(evening=False)


def test_the_backfill_splits_a_paired_run_from_its_samples(conn, cfg):
    """The 2026-08-30 shape, in miniature: the Kubota runs alone for ten
    minutes, the MEP joins for ten more, and the pair's 10 kW is divided by
    what each of them delivers on its own."""
    learned_solo(conn, cfg)                       # MEP 12 kW, Kubota 6 kW -> 2:1
    base = ts_at(cfg, "2026-08-20", 20)
    for i in range(21):
        both = i >= 10
        history.record_sample(conn, {
            "batteryVoltage": 54.0, "battSocBM": 80,
            "battPower": (10000 if both else 5000) - 1000,
            "battCurrent": 100.0, "battMonitorOnline": True,
            "acPower1": 500, "acPower2": 500,
            "mep803aAction": history.GEN_RUNNING if both else history.GEN_STOPPED,
            "kubotaAction": history.GEN_RUNNING,
            "mep803aMode": 2, "kubotaMode": 2}, ts=base + i * 60)
    conn.execute("INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, "
                 "gross_w, solo, kind) VALUES ('kubota',?,?,20,7500,0,'auto')",
                 (base, base + 20 * 60))
    conn.commit()
    # The six solo runs learned_solo added have no samples of their own.
    assert fuel.backfill_attribution(conn, cfg) == (1, 6)
    attr = conn.execute("SELECT gross_attr_w FROM gen_runs WHERE gen='kubota' "
                        "AND start_ts=?", (base,)).fetchone()[0]
    # Ten minutes of 5,000 whole, eleven of 10,000 at one third.
    assert attr == pytest.approx((10 * 5000 + 11 * 10000 / 3) / 21, rel=1e-3)
    assert attr < 7500, "less than the system figure it was credited with"


def test_the_attribution_backfill_is_idempotent(conn, cfg):
    base = ts_at(cfg, "2026-08-20", 20)
    for i in range(11):
        history.record_sample(conn, {
            "batteryVoltage": 54.0, "battSocBM": 80, "battPower": 4000,
            "battCurrent": 75.0, "battMonitorOnline": True,
            "acPower1": 500, "acPower2": 500,
            "mep803aAction": history.GEN_STOPPED,
            "kubotaAction": history.GEN_RUNNING,
            "mep803aMode": 2, "kubotaMode": 2}, ts=base + i * 60)
    conn.execute("INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, "
                 "gross_w, solo, kind) VALUES ('kubota',?,?,10,5000,1,'auto')",
                 (base, base + 10 * 60))
    conn.commit()
    assert fuel.backfill_attribution(conn, cfg)[0] > 0
    before = [tuple(r) for r in conn.execute(
        "SELECT id, gross_attr_w, fuel_gal FROM gen_runs ORDER BY id")]
    assert fuel.backfill_attribution(conn, cfg) == (0, 0)
    after = [tuple(r) for r in conn.execute(
        "SELECT id, gross_attr_w, fuel_gal FROM gen_runs ORDER BY id")]
    assert before == after


def test_a_run_whose_samples_are_gone_stays_unattributed(conn, cfg):
    """Purged at 90 days. Nothing to split, so nothing is written, and the
    price falls back to the system figure."""
    add_run(conn, "kubota", ts_at(cfg, "2026-08-10", 2), 60, 6000)
    assert fuel.backfill_attribution(conn, cfg) == (0, 1)
    r = conn.execute("SELECT gross_attr_w FROM gen_runs").fetchone()
    assert r["gross_attr_w"] is None
    # And it is looked at again next time, still writing nothing.
    assert fuel.backfill_attribution(conn, cfg) == (0, 1)


def test_a_closed_paired_run_is_split_as_it_is_written(conn, cfg):
    """Not only in the backfill: the split happens at close, from the minutes
    derive_gen_runs already had in hand."""
    learned_solo(conn, cfg)
    base = ts_at(cfg, "2026-08-20", 20)

    class Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)

    run = {"start_ts": base, "start_v": 53.0, "currents": [100.0],
           "loads": [1000], "gross": [5000] * 10 + [10000] * 10,
           "gross_paired": [False] * 10 + [True] * 10,
           "solo": False, "mode": 2}
    history._close_run(conn, cfg, "kubota", run,
                       Row({"ts": base + 20 * 60, "battery_v": 55.0}))
    r = conn.execute("SELECT gross_w, gross_attr_w, fuel_gal FROM gen_runs "
                     "WHERE start_ts=?", (base,)).fetchone()
    assert r["gross_w"] == 7500, "the system measurement is untouched"
    assert r["gross_attr_w"] == pytest.approx((10 * 5000 + 10 * 10000 / 3) / 20,
                                             abs=0.1)
    # 4,167 W of a 7,000 W Kubota is 0.60 rated, not 1.07.
    assert r["fuel_gal"] == pytest.approx(
        fuel.gallons(cfg, "kubota", r["gross_attr_w"], 20))
    assert r["fuel_gal"] < fuel.gallons(cfg, "kubota", r["gross_w"], 20)
