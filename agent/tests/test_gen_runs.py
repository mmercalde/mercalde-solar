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
         v_start=52.0, v_end=55.0, pre=2, post=2):
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
            "battCurrent": 55.0 if running else -22.0,
            "mep803aAction": act if gen == "mep" else other,
            "kubotaAction": act if gen == "kubota" else other,
            "mep803aMode": mode if gen == "mep" else 2,
            "kubotaMode": mode if gen == "kubota" else 2,
            "acPower1": 600, "acPower2": 600,
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
