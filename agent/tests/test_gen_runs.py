"""Generator run derivation, with the exercise-tagging rule from SPEC section 5."""

import json
from datetime import datetime, timedelta

import history


def ts_at(cfg, day, hhmm):
    """Unix seconds for a local wall-clock time on a given local date."""
    tz = history.tzinfo(cfg)
    h, m = (int(x) for x in hhmm.split(":"))
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=h, minute=m, tzinfo=tz).timestamp())


def feed(conn, cfg, start_ts, minutes, gen="mep", mode=2, other_running=False,
         v_start=52.0, v_end=55.0, pre=2, post=2, amps=55.0, load_w=1200,
         on_reason=None):
    """Write a minute-resolution run: `pre` stopped samples, the run, `post` stopped."""
    total = pre + minutes + post
    for i in range(total):
        ts = start_ts - pre * 60 + i * 60
        running = pre <= i < pre + minutes
        frac = (i - pre) / max(1, minutes - 1) if running else (0.0 if i < pre else 1.0)
        v = v_start + (v_end - v_start) * min(max(frac, 0.0), 1.0)
        act = history.GEN_RUNNING if running else history.GEN_STOPPED
        other = history.GEN_RUNNING if (running and other_running) else history.GEN_STOPPED
        data = {
            "batteryVoltage": round(v, 2), "battSocBM": 80,
            "battPower": 3000 if running else -1200,
            "battCurrent": amps if running else -22.0,
            "mep803aAction": act if gen == "mep" else other,
            "kubotaAction": act if gen == "kubota" else other,
            "mep803aMode": mode if gen == "mep" else 2,
            "kubotaMode": mode if gen == "kubota" else 2,
            "acPower1": load_w / 2, "acPower2": load_w / 2,
            "mppt80PVPower": 0, "southArrayPVPower": 0, "westArrayPVPower": 0,
            "battMonitorOnline": True, "pollErrors": 0, "autoGenEnabled": True,
            # The AGS's Generator On Reason, as /data spells it. None is the
            # old world: a sample taken before the register was read.
            "mepOnReason": on_reason if gen == "mep" else None,
            "kubotaOnReason": on_reason if gen == "kubota" else None,
        }
        history.record_sample(conn, data, ts=ts)
    return start_ts


def test_exercise_run_is_tagged(conn, cfg):
    """09:00 local, 30 min: the standing exercise, not a signal."""
    start = ts_at(cfg, "2026-08-10", "09:00")
    feed(conn, cfg, start, 30)
    assert history.derive_gen_runs(conn, cfg) == 1
    run = conn.execute("SELECT * FROM gen_runs").fetchone()
    assert run["kind"] == "exercise"
    assert run["gen"] == "mep"
    assert run["duration_min"] == 30


def test_exercise_window_edges(conn, cfg):
    """09:05 still counts; 09:06 does not."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "09:05"), 30)
    feed(conn, cfg, ts_at(cfg, "2026-08-12", "09:06"), 30)
    history.derive_gen_runs(conn, cfg)
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM gen_runs ORDER BY start_ts")]
    assert kinds == ["exercise", "auto"]


def test_long_run_at_0900_is_not_exercise(conn, cfg):
    """Starts in the window but runs 50 min, so it is a real charge run."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "09:00"), 50)
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "auto"


def test_night_run_is_auto(conn, cfg):
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "03:10"), 45)
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "auto"


def test_mode_on_is_manual(conn, cfg):
    """Owner forced the generator on (mode 1) rather than leaving it in auto."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "14:00"), 40, mode=history.MODE_ON)
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "manual"


def test_agent_run_is_tagged(conn, cfg):
    """A run that follows the agent raising this gen's start above default."""
    start = ts_at(cfg, "2026-08-10", "16:00")
    history.record_action(
        conn, "set_gen_thresholds",
        {"mep_start": 55.5, "mep_stop": 57.0, "kub_start": 52.0, "kub_stop": 54.5},
        True, "solo top-up", 55.4, 84, "applied", ts=start - 600)
    feed(conn, cfg, start, 60)
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "agent"


def test_agent_write_at_default_does_not_claim_the_run(conn, cfg):
    """Returning to defaults is not a cause; the run is the Pi5's own."""
    start = ts_at(cfg, "2026-08-10", "16:00")
    history.record_action(
        conn, "set_gen_thresholds",
        {"mep_start": 52.0, "mep_stop": 54.5, "kub_start": 52.0, "kub_stop": 54.5},
        True, "back to default", 53.0, 70, "applied", ts=start - 600)
    feed(conn, cfg, start, 60)
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "auto"


def test_solo_and_paired(conn, cfg):
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "02:00"), 40, other_running=False)
    feed(conn, cfg, ts_at(cfg, "2026-08-11", "02:00"), 40, other_running=True)
    history.derive_gen_runs(conn, cfg)
    solo = [r["solo"] for r in conn.execute(
        "SELECT solo FROM gen_runs WHERE gen='mep' ORDER BY start_ts")]
    assert solo == [1, 0]


def test_charge_rate_recorded(conn, cfg):
    """2.0 V over 60 min is 2.0 V/h, and mean charging current is kept."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "02:00"), 60, v_start=52.0, v_end=54.0)
    history.derive_gen_runs(conn, cfg)
    run = conn.execute("SELECT * FROM gen_runs").fetchone()
    assert run["rate_v_per_h"] == 2.0
    assert run["rate_a"] == 55.0


def test_the_house_load_during_the_run_is_recorded(conn, cfg):
    """So a run measured through a steam bath can be left out of the rate."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "20:09"), 60, load_w=7000)
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT load_w FROM gen_runs").fetchone()["load_w"] == 7000


def test_the_current_is_the_net_over_the_run(conn, cfg):
    """Charge the pack gave back to a load is charge it did not keep, so the
    mean is over the whole run and not over the charging minutes only."""
    start = ts_at(cfg, "2026-08-10", "02:00")
    feed(conn, cfg, start, 60, amps=100.0)
    # Halve it: one minute in ten went the other way, at the same size.
    conn.execute("UPDATE samples SET batt_current=-100.0 "
                 "WHERE ts >= ? AND ts < ? AND (ts / 60) % 2 = 0", (start, start + 3600))
    conn.commit()
    conn.execute("DELETE FROM gen_runs")
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT rate_a FROM gen_runs").fetchone()["rate_a"] == 0.0


def test_load_w_is_added_to_a_database_that_predates_it(conn, cfg, tmp_path):
    """CREATE TABLE IF NOT EXISTS leaves an existing table alone."""
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE gen_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "gen TEXT NOT NULL, start_ts INTEGER NOT NULL, stop_ts INTEGER, "
                "duration_min REAL, start_v REAL, stop_v REAL, rate_v_per_h REAL, "
                "rate_a REAL, solo INTEGER, kind TEXT)")
    old.execute("INSERT INTO gen_runs (gen, start_ts) VALUES ('mep', 1)")
    old.commit()
    old.close()
    fresh = history.connect(path)
    cols = {r["name"] for r in fresh.execute("PRAGMA table_info(gen_runs)")}
    assert "load_w" in cols
    assert fresh.execute("SELECT load_w FROM gen_runs").fetchone()["load_w"] is None
    fresh.close()


def test_derive_is_idempotent(conn, cfg):
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "02:00"), 40)
    assert history.derive_gen_runs(conn, cfg, since=0) == 1
    assert history.derive_gen_runs(conn, cfg, since=0) == 0
    assert conn.execute("SELECT COUNT(*) c FROM gen_runs").fetchone()["c"] == 1


def test_open_run_is_not_written_until_it_closes(conn, cfg):
    """A generator still running has no stop voltage, so no rate: leave it out."""
    start = ts_at(cfg, "2026-08-10", "02:00")
    feed(conn, cfg, start, 30, post=0)
    assert history.derive_gen_runs(conn, cfg) == 0
    assert conn.execute("SELECT COUNT(*) c FROM gen_runs").fetchone()["c"] == 0


def test_gen_running_hours_excludes_those_hours(conn, cfg):
    start = ts_at(cfg, "2026-08-10", "02:10")
    feed(conn, cfg, start, 40)
    history.derive_gen_runs(conn, cfg)
    hours = history.gen_running_hours(conn, start - 86400, start + 86400)
    assert history.hour_floor(start) in hours


# --- the peak POLICY 4 asks about is solar's ---------------------------------

def solar_sample(conn, cfg, day, hhmm, v):
    history.record_sample(conn, {
        "batteryVoltage": v, "battSocBM": 70, "battPower": 2000,
        "battCurrent": 37.0, "battMonitorOnline": True,
        "mep803aAction": history.GEN_STOPPED, "kubotaAction": history.GEN_STOPPED,
        "acPower1": 600, "acPower2": 600,
        "mppt80PVPower": 3000, "southArrayPVPower": 3000, "westArrayPVPower": 3000,
    }, ts=ts_at(cfg, day, hhmm))


def test_the_peak_ignores_what_a_generator_did(conn, cfg):
    """"peak today 56.0 V" was this morning's generator run, not the sun."""
    feed(conn, cfg, ts_at(cfg, "2026-08-29", "05:00"), 60,
         v_start=52.0, v_end=56.0)
    history.derive_gen_runs(conn, cfg)
    solar_sample(conn, cfg, "2026-08-29", "13:00", 54.4)
    peak = history.solar_peak(conn, cfg, "2026-08-29",
                             now=ts_at(cfg, "2026-08-29", "21:00"))
    assert peak == 54.4, "the sun got to 54.4; the MEP got to 56.0"


def test_the_peak_restarts_after_a_run_ends(conn, cfg):
    """The pack keeps the generator's surface charge for a while, and that
    voltage is the generator's too."""
    solar_sample(conn, cfg, "2026-08-29", "12:00", 55.9)
    feed(conn, cfg, ts_at(cfg, "2026-08-29", "17:00"), 60,
         v_start=52.0, v_end=56.5)
    history.derive_gen_runs(conn, cfg)
    solar_sample(conn, cfg, "2026-08-29", "19:00", 53.2)
    peak = history.solar_peak(conn, cfg, "2026-08-29",
                             now=ts_at(cfg, "2026-08-29", "21:00"))
    assert peak == 53.2, "only what was measured once the pack had settled"


def test_the_peak_waits_for_the_pack_to_settle_after_a_run(conn, cfg):
    feed(conn, cfg, ts_at(cfg, "2026-08-29", "17:00"), 60,
         v_start=52.0, v_end=56.5)
    history.derive_gen_runs(conn, cfg)
    solar_sample(conn, cfg, "2026-08-29", "18:20", 55.4)   # inside the settle
    solar_sample(conn, cfg, "2026-08-29", "18:40", 53.1)   # after it
    assert history.solar_peak(conn, cfg, "2026-08-29",
                              now=ts_at(cfg, "2026-08-29", "21:00")) == 53.1


def test_a_day_with_no_generator_keeps_the_whole_day(conn, cfg):
    solar_sample(conn, cfg, "2026-08-29", "10:00", 53.0)
    solar_sample(conn, cfg, "2026-08-29", "13:00", 56.8)
    solar_sample(conn, cfg, "2026-08-29", "17:00", 55.0)
    assert history.solar_peak(conn, cfg, "2026-08-29",
                              now=ts_at(cfg, "2026-08-29", "21:00")) == 56.8


def test_a_day_that_is_all_generator_has_no_solar_peak(conn, cfg):
    feed(conn, cfg, ts_at(cfg, "2026-08-29", "05:00"), 60, pre=0, post=0,
         v_start=52.0, v_end=56.0)
    history.derive_gen_runs(conn, cfg)
    assert history.solar_peak(conn, cfg, "2026-08-29",
                              now=ts_at(cfg, "2026-08-29", "06:30")) is None


# --- The AGS's own reason ----------------------------------------------------
#
# On 2026-09-03 the Kubota ran an AGS exercise at 6:49 PM. The classifier
# knew only that exercises start at 09:00, so it filed the run as 'auto' and
# let it into the charge-rate and fuel figures it should have been out of.
# The AGS had the answer in a register the whole time.

import pytest
import logging


@pytest.mark.parametrize("reason,kind", [
    ("exercise", "exercise"),
    ("manual_on", "manual"),
    ("battery_soc_low", "battery_soc_low"),
    ("ac_current_high", "ac_current_high"),
    ("contact_closed", "contact_closed"),
    ("non_quiet_time", "non_quiet_time"),
])
def test_each_reason_code_names_the_run(conn, cfg, reason, kind):
    """Every code but the low-voltage one is the kind, as itself."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "14:00"), 40, on_reason=reason)
    assert history.derive_gen_runs(conn, cfg) == 1
    run = conn.execute("SELECT * FROM gen_runs").fetchone()
    assert run["kind"] == kind
    assert run["on_reason"] == reason


def test_the_evening_exercise_that_started_this(conn, cfg):
    """6:49 PM, well outside the 09:00 window, and still an exercise."""
    feed(conn, cfg, ts_at(cfg, "2026-09-03", "18:49"), 30, gen="kubota",
         v_start=59.4, v_end=59.9, on_reason="exercise")
    history.derive_gen_runs(conn, cfg)
    run = conn.execute("SELECT * FROM gen_runs").fetchone()
    assert run["kind"] == "exercise"
    # And so it stays out of the learning it used to pollute.
    assert history.gen_runs(conn, days=365,
                            now=ts_at(cfg, "2026-09-04", "12:00")) == []


def test_low_voltage_still_separates_the_agent_from_the_pi5(conn, cfg):
    """Reason 1 is not the whole answer: who moved the threshold is."""
    start = ts_at(cfg, "2026-08-10", "22:00")
    feed(conn, cfg, start, 40, on_reason="dc_voltage_low")
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "auto"

    conn.execute("DELETE FROM gen_runs")
    conn.execute("DELETE FROM samples")
    start = ts_at(cfg, "2026-08-12", "22:00")
    history.record_action(conn, "set_gen_thresholds",
                          {"mep_start": 55.5, "mep_stop": 57.0}, 1,
                          "solo top-up", 54.0, None, "allowed", ts=start - 600)
    feed(conn, cfg, start, 40, on_reason="dc_voltage_low")
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "agent"


def test_the_reason_beats_the_clock(conn, cfg):
    """A 09:00 run the AGS says was a low-voltage start is not an exercise."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "09:00"), 30,
         on_reason="dc_voltage_low")
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "auto"


def test_the_reason_beats_the_mode(conn, cfg):
    """Mode ON used to mean manual; the AGS saying 'exercise' outranks it."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "14:00"), 30, mode=history.MODE_ON,
         on_reason="exercise")
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "exercise"


def test_an_unnamed_code_is_kept_rather_than_rounded_to_auto(conn, cfg):
    """pi5 passes through code_N for reasons it has no name for."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "14:00"), 30, on_reason="code_12")
    history.derive_gen_runs(conn, cfg)
    run = conn.execute("SELECT * FROM gen_runs").fetchone()
    assert run["kind"] == "code_12"
    assert run["on_reason"] == "code_12"


def test_not_on_is_not_a_reason(conn, cfg):
    """Reason 0 says the generator is not on, which cannot be why it started."""
    feed(conn, cfg, ts_at(cfg, "2026-08-10", "09:00"), 30, on_reason="not_on")
    history.derive_gen_runs(conn, cfg)
    # Falls through to the heuristic, which at 09:00 says exercise.
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "exercise"


def test_a_missing_reason_falls_back_and_says_so(conn, cfg, caplog):
    """The old behaviour, kept, and audible."""
    with caplog.at_level(logging.INFO, logger="history"):
        feed(conn, cfg, ts_at(cfg, "2026-08-10", "09:00"), 30, on_reason=None)
        history.derive_gen_runs(conn, cfg)
    run = conn.execute("SELECT * FROM gen_runs").fetchone()
    assert run["kind"] == "exercise"
    assert run["on_reason"] is None
    assert any("no AGS on-reason" in r.getMessage() for r in caplog.records)


def test_a_reason_that_arrived_does_not_log_a_fallback(conn, cfg, caplog):
    with caplog.at_level(logging.INFO, logger="history"):
        feed(conn, cfg, ts_at(cfg, "2026-08-10", "09:00"), 30,
             on_reason="exercise")
        history.derive_gen_runs(conn, cfg)
    assert not any("no AGS on-reason" in r.message % r.args
                   for r in caplog.records)


# --- The schedule the AGS holds ---------------------------------------------

def test_the_live_schedule_replaces_the_manifests_guess(conn, cfg):
    cfg = dict(cfg, exercise=dict(cfg["exercise"]))
    assert history.exercise_window("kubota", cfg) == ("09:00", 30)
    changed = history.apply_exercise_schedule(cfg, {
        "kubotaExercise": {"every_days": 3, "minutes": 30, "start": "18:49"}})
    assert changed["kubota_start"] == ("09:00", "18:49")
    assert history.exercise_window("kubota", cfg) == ("18:49", 30)
    # One generator's schedule is not the other's.
    assert history.exercise_window("mep", cfg) == ("09:00", 30)


def test_the_fallback_window_moves_with_it(conn, cfg):
    """With no reason recorded, 6:49 PM is an exercise once the AGS says so."""
    cfg = dict(cfg, exercise=dict(cfg["exercise"]))
    history.apply_exercise_schedule(cfg, {
        "kubotaExercise": {"every_days": 3, "minutes": 30, "start": "18:49"}})
    feed(conn, cfg, ts_at(cfg, "2026-09-03", "18:49"), 30, gen="kubota")
    history.derive_gen_runs(conn, cfg)
    assert conn.execute("SELECT kind FROM gen_runs").fetchone()["kind"] == "exercise"


def test_a_data_payload_without_the_schedule_changes_nothing(conn, cfg):
    cfg = dict(cfg, exercise=dict(cfg["exercise"]))
    assert history.apply_exercise_schedule(cfg, {}) == {}
    assert history.apply_exercise_schedule(cfg, {"kubotaExercise": None}) == {}
    assert history.exercise_window("kubota", cfg) == ("09:00", 30)


def test_when_the_next_exercise_is_due(conn, cfg):
    cfg = dict(cfg, exercise=dict(cfg["exercise"]))
    history.apply_exercise_schedule(cfg, {
        "kubotaExercise": {"every_days": 3, "minutes": 30, "start": "18:49"}})
    feed(conn, cfg, ts_at(cfg, "2026-09-03", "18:49"), 30, gen="kubota",
         on_reason="exercise")
    history.derive_gen_runs(conn, cfg)
    due = history.next_exercise(conn, "kubota", cfg,
                                now=ts_at(cfg, "2026-09-04", "12:00"))
    assert due["every_days"] == 3 and due["at"] == "18:49"
    assert due["days_until_due"] == 2.3
    assert due["overdue"] is False
    late = history.next_exercise(conn, "kubota", cfg,
                                 now=ts_at(cfg, "2026-09-08", "12:00"))
    assert late["overdue"] is True


def test_no_exercise_on_record_gives_no_due_date(conn, cfg):
    """A period without a last run is not a prediction."""
    due = history.next_exercise(conn, "mep", cfg,
                                now=ts_at(cfg, "2026-09-04", "12:00"))
    assert due["last"] is None
    assert "due" not in due


# --- The run that is happening now ------------------------------------------

def test_current_run_finds_the_open_run(conn, cfg):
    start = ts_at(cfg, "2026-09-03", "18:49")
    # No trailing stopped samples: the run is still going.
    feed(conn, cfg, start, 20, gen="kubota", post=0, on_reason="exercise")
    now = start + 19 * 60
    run = history.current_run(conn, "kubota", now=now)
    assert run["started_at"] == start
    assert run["running_minutes"] == 19.0
    assert run["on_reason"] == "exercise"
    assert run["truncated"] is False
    # And nothing is running on the other engine.
    assert history.current_run(conn, "mep", now=now) is None


def test_current_run_is_none_once_it_has_stopped(conn, cfg):
    start = ts_at(cfg, "2026-09-03", "18:49")
    feed(conn, cfg, start, 20, gen="kubota", on_reason="exercise")
    assert history.current_run(conn, "kubota",
                              now=start + 25 * 60) is None
