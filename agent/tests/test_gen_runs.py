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
         v_start=52.0, v_end=55.0, pre=2, post=2, amps=55.0, load_w=1200):
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
