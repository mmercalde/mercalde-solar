"""Whole months, and the months that stand out.

The superlatives are the point: a model handed seventeen rows will rank them
itself, and ranking rows is exactly the arithmetic it does confidently and
wrongly. So the ranking is done in Python and these are the tests that say
what it should say.
"""

from datetime import datetime, timedelta

import pytest

import history
import monthly


def ts_at(cfg, day, hour=12):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, tzinfo=tz).timestamp())


def a_day(conn, cfg, day, solar_kwh=30.0, load_kwh=25.0, min_v=53.0,
          peak_v=57.0, hours=24):
    """One day in `daily`, with `hours` of hourly rows standing behind it.

    The hourly rows are what tell a dark day from a short one, so a test can
    make a day that looks bad and is only incomplete.
    """
    conn.execute(
        "INSERT OR REPLACE INTO daily (day, solar_wh, load_wh, mep_minutes, "
        "kub_minutes, peak_v, min_v) VALUES (?,?,?,0,0,?,?)",
        (day, solar_kwh * 1000, load_kwh * 1000, peak_v, min_v))
    for h in range(hours):
        history.put_hourly(conn, ts_at(cfg, day, h), "solar", None, None,
                           solar_kwh * 1000 / max(hours, 1), None, None, None,
                           60, "live")
    conn.commit()


def a_month(conn, cfg, month, days=30, **kw):
    for n in range(1, days + 1):
        a_day(conn, cfg, f"{month}-{n:02d}", **kw)


def add_run(conn, gen, start_ts, minutes, fuel_gal, kind="auto"):
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, gross_w, "
        "fuel_gal, solo, kind) VALUES (?,?,?,?,4000,?,1,?)",
        (gen, start_ts, start_ts + int(minutes * 60), minutes, fuel_gal, kind))
    conn.commit()


# --- the rows ------------------------------------------------------------------

def test_a_month_sums_its_days(conn, cfg):
    a_month(conn, cfg, "2026-04", days=30, solar_kwh=30.0, load_kwh=25.0)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["month"] == "2026-04" and m["days_with_data"] == 30
    assert m["solar_kwh"] == pytest.approx(900.0)
    assert m["load_kwh"] == pytest.approx(750.0)
    assert m["net_kwh"] == pytest.approx(150.0)


def test_net_is_solar_less_load_and_goes_negative(conn, cfg):
    """A December that lives off the generator and the battery."""
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["net_kwh"] == pytest.approx(-558.0)


def test_the_voltage_extremes_are_the_month_s(conn, cfg):
    a_month(conn, cfg, "2026-04", days=10, min_v=53.0, peak_v=57.0)
    a_day(conn, cfg, "2026-04-11", min_v=51.4, peak_v=60.2)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["min_v"] == 51.4 and m["max_v"] == 60.2


def test_the_best_and_worst_solar_day_are_named_with_their_kwh(conn, cfg):
    a_month(conn, cfg, "2026-04", days=28, solar_kwh=30.0)
    a_day(conn, cfg, "2026-04-29", solar_kwh=45.0)
    a_day(conn, cfg, "2026-04-30", solar_kwh=12.0)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["best_solar_day"] == {"date": "2026-04-29", "kwh": 45.0}
    assert m["worst_solar_day"] == {"date": "2026-04-30", "kwh": 12.0}


def test_a_short_day_cannot_be_the_worst_solar_day(conn, cfg):
    """2026-08-28 on the real record is 0.0 kWh over six hours - the
    afternoon live sampling started, not a dark day. The worst day that
    month was 2026-08-24, and this is the filter that finds it."""
    a_month(conn, cfg, "2026-08", days=30, solar_kwh=30.0)
    a_day(conn, cfg, "2026-08-24", solar_kwh=14.0)          # genuinely dark
    a_day(conn, cfg, "2026-08-31", solar_kwh=0.6, hours=6)  # a stub of a day
    out = monthly.monthly_summary(conn, cfg, months=999, detail=True)
    m = out["months"][0]
    assert m["worst_solar_day"] == {"date": "2026-08-24", "kwh": 14.0}
    assert "2026-08-31" in out["partial_days_excluded_from_day_ranking"]
    assert m["days_with_data"] == 31 and m["days_ranked"] == 30


def test_generator_hours_and_gallons_come_from_the_runs(conn, cfg):
    a_month(conn, cfg, "2026-08", days=31)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-10", 2), 90, 0.48)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-20", 2), 60, 0.32)
    add_run(conn, "mep", ts_at(cfg, "2026-08-15", 2), 120, 1.98)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["gen_hours_recorded"] == {"mep": 2.0, "kubota": 2.5}
    assert m["gen_hours"] == pytest.approx(4.5)
    assert m["fuel_gal_modelled"]["kubota"] == pytest.approx(0.80)
    assert m["fuel_gal_modelled"]["mep"] == pytest.approx(1.98)
    assert m["fuel_gal_total"] == pytest.approx(2.78)
    assert m["gen_runs"] == {"mep": 1, "kubota": 2}
    assert "modelled from the runs" in m["fuel_basis"]


def test_a_month_with_no_runs_reports_nought_not_nothing(conn, cfg):
    a_month(conn, cfg, "2026-04", days=30)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["gen_hours_recorded"] == {"mep": 0.0, "kubota": 0.0}
    assert m["gen_hours"] == 0.0
    assert m["fuel_gal_modelled"] == {"mep": 0.0, "kubota": 0.0}
    assert m["fuel_gal_total"] == 0.0
    assert m["fuel_basis"] == "no generator activity"


def test_the_exercise_runs_are_left_out(conn, cfg):
    """Thirty minutes at nine in the morning is not the agent's and is not a
    signal, here as everywhere else."""
    a_month(conn, cfg, "2026-08", days=31)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-10", 9), 30, 0.16,
            kind="exercise")
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["gen_hours_recorded"]["kubota"] == 0.0
    assert m["gen_runs"]["kubota"] == 0


def test_a_run_belongs_to_the_month_it_began_in(conn, cfg):
    a_month(conn, cfg, "2026-07", days=31)
    a_month(conn, cfg, "2026-08", days=31)
    add_run(conn, "kubota", ts_at(cfg, "2026-07-31", 23), 120, 0.64)
    out = {m["month"]: m for m in monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"]}
    assert out["2026-07"]["gen_hours_recorded"]["kubota"] == 2.0
    assert out["2026-08"]["gen_hours_recorded"]["kubota"] == 0.0


# --- the superlatives -------------------------------------------------------------

@pytest.fixture
def a_year(conn, cfg):
    """Four months that disagree about everything, so each superlative has a
    different answer and none of them can be right by accident.

    Totals, not daily rates: April's 30 days at 35 kWh is less load than
    July's 31 at 30, which is the sort of thing the ranking exists to get
    right and a reader does not.
    """
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    a_month(conn, cfg, "2026-01", days=31, solar_kwh=20.0, load_kwh=36.0)
    a_month(conn, cfg, "2026-04", days=30, solar_kwh=38.0, load_kwh=35.0)
    a_month(conn, cfg, "2026-07", days=31, solar_kwh=33.0, load_kwh=30.0)
    add_run(conn, "kubota", ts_at(cfg, "2026-01-10", 2), 60, 0.32)
    add_run(conn, "mep", ts_at(cfg, "2026-07-10", 2), 300, 4.95)
    return conn


def test_the_superlatives_name_a_month_and_a_value(a_year, cfg):
    s = monthly.monthly_summary(a_year, cfg, months=999)["superlatives"]
    assert s["worst_solar_month"]["month"] == "2025-12"
    assert s["worst_solar_month"]["value"] == pytest.approx(527.0)
    assert s["best_solar_month"]["month"] == "2026-04"
    assert s["best_solar_month"]["value"] == pytest.approx(1140.0)
    assert s["highest_load_month"]["month"] == "2026-01"
    assert s["lowest_load_month"]["month"] == "2026-07"
    assert s["most_fuel_month"]["month"] == "2026-07"
    assert s["most_fuel_month"]["value"] == pytest.approx(4.95)


def test_every_superlative_carries_its_units(a_year, cfg):
    s = monthly.monthly_summary(a_year, cfg, months=999)["superlatives"]
    for key in ("worst_solar_month", "best_solar_month", "highest_load_month",
                "most_fuel_month"):
        assert s[key]["units"], key


def test_a_month_too_short_to_rank_cannot_win(conn, cfg):
    """September 2026 is two days old and would otherwise be the worst month
    for solar in the record by a factor of eight."""
    a_month(conn, cfg, "2026-04", days=30, solar_kwh=38.0)
    a_month(conn, cfg, "2026-08", days=31, solar_kwh=30.0)
    a_month(conn, cfg, "2026-09", days=2, solar_kwh=37.0)
    s = monthly.monthly_summary(conn, cfg, months=999)["superlatives"]
    assert s["worst_solar_month"]["month"] == "2026-08"
    assert s["months_ranked"] == 2
    assert s["months_excluded"] == ["2026-09 (2 days)"]
    # Each superlative carries how many months it ranked over.
    assert ">=20 days each" in s["worst_solar_month"]["basis"]


def test_the_short_month_is_still_in_the_series(conn, cfg):
    """Excluded from the ranking, not hidden: the owner may well be asking
    about this month."""
    a_month(conn, cfg, "2026-08", days=31)
    a_month(conn, cfg, "2026-09", days=2)
    out = monthly.monthly_summary(conn, cfg, months=999, detail=True)
    assert [m["month"] for m in out["months"]] == ["2026-08", "2026-09"]
    assert out["last_month"] == "2026-09"


def test_with_no_generator_month_the_fuel_superlative_says_so(conn, cfg):
    a_month(conn, cfg, "2026-04", days=30)
    s = monthly.monthly_summary(conn, cfg, months=999)["superlatives"]
    assert s["most_fuel_month"]["month"] is None
    assert s["most_fuel_month"]["value"] == 0.0
    assert "no generator activity" in s["most_fuel_month"]["basis"]


def test_with_no_month_long_enough_nothing_is_ranked(conn, cfg):
    a_month(conn, cfg, "2026-09", days=2)
    s = monthly.monthly_summary(conn, cfg, months=999)["superlatives"]
    assert s["months_ranked"] == 0
    assert "worst_solar_month" not in s
    assert "20 days" in s["note"]


def test_an_empty_history_says_so(conn, cfg):
    out = monthly.monthly_summary(conn, cfg)
    assert out["months"] == [] and out["superlatives"] == {}
    assert "no daily rollup" in out["note"]


# --- the tool and the prompt ---------------------------------------------------------

def test_the_tool_returns_the_summary(a_year, cfg):
    import tools as toolsmod
    out = toolsmod.Tools(a_year, cfg).get_monthly_summary()
    assert out["superlatives"]["best_solar_month"]["month"] == "2026-04"
    assert len(out["months"]) == 4


def test_the_tool_is_registered_with_its_two_arguments():
    import tools as toolsmod
    assert "get_monthly_summary" in toolsmod.READ_TOOLS
    schema = next(s for s in toolsmod.SCHEMAS
                  if s["function"]["name"] == "get_monthly_summary")
    props = schema["function"]["parameters"]["properties"]
    assert set(props) == {"months", "detail"}
    assert "superlative" in schema["function"]["description"]
    assert "detail only when" in schema["function"]["description"]


def test_the_ask_prompt_sends_a_which_month_question_to_the_tool():
    import prompts
    p = prompts.ask_prompt()
    assert "get_monthly_summary" in p
    assert "worst_solar_month" in p and "most_fuel_month" in p
    assert "Never rank months yourself" in p


def test_the_monthly_summary_decides_nothing(cfg, conn):
    """Measurement only. If a later commit wants a month's shape to move a
    threshold, this is the test to delete on purpose."""
    import guard
    import policy
    src = open(policy.__file__).read() + open(guard.__file__).read()
    for word in ("monthly", "superlative", "best_solar", "worst_solar"):
        assert word not in src, word


# --- the two eras, and the gap between them ------------------------------------

def gen_hour(conn, cfg, day, hour, kwh):
    """One hour the scraped counter says the generator produced in."""
    history.put_hourly(conn, ts_at(cfg, day, hour), "gen", None, None,
                       kwh * 1000, None, None, None, 60, "live")
    conn.commit()


def batt_hour(conn, cfg, day, hour, kwh_in=0.0, kwh_out=0.0):
    history.put_hourly(conn, ts_at(cfg, day, hour), "battery", 54.0, None,
                       kwh_in * 1000, kwh_out * 1000, 53.0, 55.0, 60, "live")
    conn.commit()


def test_the_scraped_counter_carries_the_months_before_the_agent(conn, cfg):
    """gen_runs begins when the agent did. daily.mep_minutes is computed from
    the same table and inherits the same gap, which is why December 2025 read
    as 554 kWh in deficit with zero generator hours."""
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    for d in range(1, 6):
        for h in (2, 3, 4):
            gen_hour(conn, cfg, f"2025-12-{d:02d}", h, 4.5)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["gen_runs"] == {"mep": 0, "kubota": 0}, "no runs recorded then"
    assert m["gen_hours_recorded"] == {"mep": 0.0, "kubota": 0.0}
    assert m["gen_kwh_metered"] == pytest.approx(67.5)
    assert m["gen_hours_metered"] == 15
    assert m["gen_hours"] == 15.0
    assert "scraped counter" in m["gen_hours_basis"]
    assert "upper bound" in m["gen_hours_basis"]


def test_metered_energy_is_priced_at_full_load_gallons_per_kwh(conn, cfg):
    """The Kubota gives 10.9 kWh a gallon at full load and the MEP 10.1, so
    a month of metered energy can be priced without knowing which ran."""
    per_gen, mean = monthly.kwh_per_gallon(cfg)
    assert per_gen["kubota"] == pytest.approx(7.0 / 0.64, rel=1e-3)
    assert per_gen["mep"] == pytest.approx(10.0 / 0.99, rel=1e-3)
    assert 10.0 < mean < 11.0

    a_month(conn, cfg, "2025-12", days=31)
    for d in range(1, 11):
        gen_hour(conn, cfg, f"2025-12-{d:02d}", 3, 10.5)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["gen_kwh_metered"] == pytest.approx(105.0)
    assert m["fuel_gal_estimated"] == pytest.approx(105.0 / mean, rel=1e-3)
    assert m["fuel_gal_total"] == m["fuel_gal_estimated"]
    assert "estimated from metered generator energy" in m["fuel_basis"]


def test_the_hours_route_is_reported_and_runs_high(conn, cfg):
    """A twenty-minute run fills an hour bucket, so pricing the buckets
    roughly doubles the answer. It is shown so the gap is visible, and the
    energy route is what the total uses."""
    a_month(conn, cfg, "2025-12", days=31)
    for d in range(1, 11):
        gen_hour(conn, cfg, f"2025-12-{d:02d}", 3, 4.0)   # a partial hour
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["fuel_gal_estimated_from_hours"] > m["fuel_gal_estimated"]
    assert m["fuel_gal_total"] == m["fuel_gal_estimated"]


def test_a_month_with_both_sources_adds_them(conn, cfg):
    """August 2026 is scraped to the 27th and has recorded runs from the
    28th: two parts of one month, not two accounts of it."""
    a_month(conn, cfg, "2026-08", days=31)
    gen_hour(conn, cfg, "2026-08-10", 3, 21.0)          # scraped era
    add_run(conn, "kubota", ts_at(cfg, "2026-08-29", 2), 90, 0.48)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["fuel_gal_estimated"] > 0 and m["fuel_gal_modelled"]["kubota"] == 0.48
    assert m["fuel_gal_total"] == pytest.approx(
        m["fuel_gal_estimated"] + 0.48, abs=0.01)
    assert "modelled" in m["fuel_basis"] and "estimated" in m["fuel_basis"]
    assert m["gen_hours"] == pytest.approx(1 + 1.5)


# --- the energy balance ----------------------------------------------------------

def test_the_implied_generator_energy_is_what_the_month_had_to_import(conn, cfg):
    """Load less solar less what the pack gave up. December 2025: 1096 out,
    542 in, the battery 47 up over the month, so 508 kWh came from
    somewhere else."""
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    batt_hour(conn, cfg, "2025-12-15", 3, kwh_in=100.0, kwh_out=90.0)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["battery_net_kwh"] == pytest.approx(10.0)
    assert m["gen_kwh_implied"] == pytest.approx(558.0 - 10.0, abs=0.5)


def test_a_month_in_surplus_implies_no_import(conn, cfg):
    a_month(conn, cfg, "2026-04", days=30, solar_kwh=38.0, load_kwh=35.0)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["gen_kwh_implied"] == 0.0


def test_a_deficit_with_no_generator_hours_is_visibly_inconsistent(conn, cfg):
    """The whole point of the field. The record being incomplete must look
    like the record being incomplete, not like a house that ran on nothing."""
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    m = monthly.monthly_summary(conn, cfg, months=999, detail=True)["months"][0]
    assert m["gen_kwh_implied"] > 500
    assert m["gen_hours"] == 0.0 and m["gen_kwh_metered"] == 0.0
    assert m["fuel_basis"] == "no generator activity"


# --- and the ranking uses the complete series ---------------------------------------

def test_most_fuel_ranks_on_the_complete_series_not_the_runs(conn, cfg):
    """Ranked on recorded runs alone, the answer is whichever month the agent
    happened to be alive for. December burned ten times as much."""
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    a_month(conn, cfg, "2026-08", days=31, solar_kwh=30.0, load_kwh=34.0)
    for d in range(1, 16):
        for h in (2, 3, 4):
            gen_hour(conn, cfg, f"2025-12-{d:02d}", h, 15.0)
    add_run(conn, "kubota", ts_at(cfg, "2026-08-29", 2), 90, 0.48)
    s = monthly.monthly_summary(conn, cfg, months=999)["superlatives"]
    assert s["most_fuel_month"]["month"] == "2025-12"
    assert s["most_fuel_month"]["value"] > 50
    assert "not the runs alone" in s["most_fuel_month"]["basis"]


# --- what it costs to read -----------------------------------------------------
#
# The full form ran to 17,630 characters over seventeen months, about 4,400
# tokens, and on the KAMRUI's integrated GPU an answer carrying it went past
# the 180 s model timeout: the owner asked which month was worst and got "the
# agent is not answering". The payload is the thing that has to be small.

@pytest.fixture
def seventeen_months(conn, cfg):
    """A year and a half, the shape of the real record."""
    for i in range(17):
        year, month = divmod(4 + i, 12)
        year, month = 2025 + year, month + 1
        a_month(conn, cfg, f"{year}-{month:02d}", days=28,
                solar_kwh=20.0 + i, load_kwh=30.0)
        gen_hour(conn, cfg, f"{year}-{month:02d}-05", 3, 10.0 + i)
    return conn


def test_the_default_payload_is_small_enough_to_read(seventeen_months, cfg):
    """Under 2,500 characters over seventeen months. The full form is seven
    times that and is what timed out."""
    import json
    import tools as toolsmod
    out = toolsmod.Tools(seventeen_months, cfg).get_monthly_summary()
    payload = json.dumps(out, default=str)
    assert len(payload) < 2500, f"{len(payload)} chars"
    assert out["months_shown"] == 12 and out["months_on_record"] == 17


def test_the_default_still_answers_the_question_it_is_for(seventeen_months, cfg):
    """Small is only useful if the superlatives survive, and they rank the
    whole record rather than the twelve months printed."""
    import tools as toolsmod
    out = toolsmod.Tools(seventeen_months, cfg).get_monthly_summary()
    s = out["superlatives"]
    assert s["worst_solar_month"]["month"] == "2025-05"   # the first, dimmest
    assert s["best_solar_month"]["month"] == "2026-09"
    assert s["months_ranked"] == 17, "ranked over all of them, not the table"
    for key in ("worst_solar_month", "best_solar_month", "highest_load_month",
                "lowest_load_month", "most_fuel_month"):
        assert set(s[key]) >= {"month", "value", "basis"}, key


def test_the_default_table_is_rows_under_named_columns(seventeen_months, cfg):
    import tools as toolsmod
    out = toolsmod.Tools(seventeen_months, cfg).get_monthly_summary()
    assert out["columns"] == ["month", "solar_kwh", "load_kwh", "gen_kwh",
                              "shortfall_kwh", "fuel_gal", "min_v", "max_v"]
    row = out["months"][0]
    assert len(row) == len(out["columns"])
    assert all(isinstance(row[i], int) for i in range(1, 6))
    assert "basis" in out and len(out["basis"]) < 400


def test_the_default_carries_no_day_names_or_per_generator_dicts(
        seventeen_months, cfg):
    import json
    import tools as toolsmod
    payload = json.dumps(toolsmod.Tools(seventeen_months,
                                        cfg).get_monthly_summary())
    for word in ("best_solar_day", "worst_solar_day", "gen_hours_recorded",
                 "fuel_gal_modelled", "fuel_basis", "gen_hours_basis"):
        assert word not in payload, word


def test_detail_gives_back_everything(seventeen_months, cfg):
    """The day fields are still there for a question about one month."""
    import tools as toolsmod
    out = toolsmod.Tools(seventeen_months, cfg).get_monthly_summary(
        months=999, detail=True)
    assert out["months_shown"] == 17
    m = out["months"][0]
    assert m["best_solar_day"]["date"] and m["worst_solar_day"]["date"]
    assert set(m["gen_hours_recorded"]) == set(history.GENS)
    assert set(m["fuel_gal_modelled"]) == set(history.GENS)
    assert m["fuel_basis"] and m["gen_hours_basis"]


def test_asking_for_fewer_months_shortens_the_table_only(seventeen_months, cfg):
    import tools as toolsmod
    out = toolsmod.Tools(seventeen_months, cfg).get_monthly_summary(months=3)
    assert out["months_shown"] == 3
    assert out["superlatives"]["months_ranked"] == 17
    assert out["months"][-1][0] == "2026-09", "the most recent months"


def test_a_tool_result_is_journaled_with_its_size(seventeen_months, cfg, caplog):
    """Tool calls were not in the journal at all, and the size is the thing
    that broke."""
    import tools as toolsmod
    t = toolsmod.Tools(seventeen_months, cfg)
    with caplog.at_level("INFO", logger="tools"):
        t.call("get_monthly_summary", {})
    line = [r.getMessage() for r in caplog.records
            if "get_monthly_summary" in r.getMessage()]
    assert len(line) == 1
    assert "chars" in line[0]


def test_an_oversized_result_is_warned_about(seventeen_months, cfg, caplog,
                                             monkeypatch):
    import tools as toolsmod
    monkeypatch.setattr(toolsmod, "TOOL_RESULT_WARN_CHARS", 100)
    t = toolsmod.Tools(seventeen_months, cfg)
    with caplog.at_level("INFO", logger="tools"):
        t.call("get_monthly_summary", {})
    rec = [r for r in caplog.records if "get_monthly_summary" in r.getMessage()]
    assert len(rec) == 1 and rec[0].levelname == "WARNING"
    assert "over the 100" in rec[0].getMessage()


def test_the_arguments_are_journaled_too(seventeen_months, cfg, caplog):
    import tools as toolsmod
    t = toolsmod.Tools(seventeen_months, cfg)
    with caplog.at_level("INFO", logger="tools"):
        t.call("get_monthly_summary", {"months": 3, "detail": False})
    line = next(r.getMessage() for r in caplog.records
                if "get_monthly_summary" in r.getMessage())
    assert "detail=False" in line and "months=3" in line


# --- how short the month was -----------------------------------------------------

def test_the_shortfall_column_is_load_less_solar(seventeen_months, cfg):
    """The plain question. gen_kwh beside it is what covered it."""
    import tools as toolsmod
    out = toolsmod.Tools(seventeen_months, cfg).get_monthly_summary()
    assert out["columns"] == ["month", "solar_kwh", "load_kwh", "gen_kwh",
                              "shortfall_kwh", "fuel_gal", "min_v", "max_v"]
    i = out["columns"].index("shortfall_kwh")
    for row in out["months"]:
        solar, load = row[1], row[2]
        assert row[i] == max(0, load - solar)
        assert isinstance(row[i], int)


def test_a_month_the_sun_covered_is_short_by_nothing(conn, cfg):
    """Floored at zero: a surplus month is not short by a negative amount."""
    a_month(conn, cfg, "2026-04", days=30, solar_kwh=38.0, load_kwh=30.0)
    import tools as toolsmod
    out = toolsmod.Tools(conn, cfg).get_monthly_summary()
    i = out["columns"].index("shortfall_kwh")
    assert out["months"][0][i] == 0


def test_a_deep_month_is_short_by_what_it_had_to_import(conn, cfg):
    """December 2025's shape: 542 kWh of sun against 1,096 of load."""
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    import tools as toolsmod
    out = toolsmod.Tools(conn, cfg).get_monthly_summary()
    row = out["months"][0]
    i = out["columns"].index("shortfall_kwh")
    assert row[i] == 558, "31 days short by 18 kWh each"
    assert row[i] == row[2] - row[1]


def test_the_shortfall_and_what_covered_it_sit_side_by_side(conn, cfg):
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    for d in range(1, 16):
        for h in (2, 3, 4):
            gen_hour(conn, cfg, f"2025-12-{d:02d}", h, 12.0)
    import tools as toolsmod
    out = toolsmod.Tools(conn, cfg).get_monthly_summary()
    row = out["months"][0]
    short = row[out["columns"].index("shortfall_kwh")]
    gen = row[out["columns"].index("gen_kwh")]
    assert short == 558 and gen == 540
    assert "shortfall_kwh is load_kwh less solar_kwh" in out["basis"]
    assert "gen_kwh is what covered it" in out["basis"]


def test_the_detail_form_carries_the_same_figure(conn, cfg):
    """And is not the same as gen_kwh_implied, which also nets off what the
    pack gave up over the month."""
    a_month(conn, cfg, "2025-12", days=31, solar_kwh=17.0, load_kwh=35.0)
    batt_hour(conn, cfg, "2025-12-15", 3, kwh_in=100.0, kwh_out=90.0)
    m = monthly.monthly_summary(conn, cfg, months=999,
                                detail=True)["months"][0]
    assert m["shortfall_kwh"] == pytest.approx(558.0)
    assert m["gen_kwh_implied"] == pytest.approx(548.0, abs=0.5)
    assert m["shortfall_kwh"] > m["gen_kwh_implied"]


def test_the_extra_column_keeps_the_payload_small(seventeen_months, cfg):
    import json
    import tools as toolsmod
    payload = json.dumps(toolsmod.Tools(seventeen_months,
                                        cfg).get_monthly_summary(),
                         default=str)
    assert len(payload) < 2500, f"{len(payload)} chars"
