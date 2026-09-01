"""The replay: a night's samples through the state machine and the guard.

Small synthetic nights rather than the real database, so what it should say
is arithmetic rather than recollection.
"""

from datetime import datetime

import pytest

import history
import replay_topup
import topup


def ts_at(cfg, day, hour, minute=0):
    tz = history.tzinfo(cfg)
    return int(datetime.strptime(day, "%Y-%m-%d")
               .replace(hour=hour, minute=minute, tzinfo=tz).timestamp())


def sample(conn, ts, volts, soc, mep=history.GEN_STOPPED,
           kub=history.GEN_STOPPED, autogen=True):
    history.record_sample(conn, {
        "batteryVoltage": volts, "battSocBM": soc, "battPower": -1400,
        "battCurrent": -26.0, "battMonitorOnline": True,
        "acPower1": 700, "acPower2": 700,
        "mppt80PVPower": 0, "southArrayPVPower": 0, "westArrayPVPower": 0,
        "mep803aAction": mep, "kubotaAction": kub,
        "mep803aMode": 2, "kubotaMode": 2,
        "mepAgsOnline": True, "kubotaAgsOnline": True,
        "autoGenEnabled": autogen, "pollErrors": 0}, ts=ts)


@pytest.fixture
def night(conn, cfg):
    """Two hours of a flat evening from 8 pm, one sample a minute."""
    start = ts_at(cfg, "2026-08-30", 20)
    for i in range(120):
        sample(conn, start + i * 60, 53.8, 88)
    return start


@pytest.fixture
def db(tmp_path, conn, night, cfg, monkeypatch):
    """The in-memory night, on disk where the replay can open it."""
    path = str(tmp_path / "history.sqlite")
    out = history.connect(path)
    conn.backup(out)
    out.close()
    monkeypatch.setattr(replay_topup.weather, "hourly", lambda *a, **k: [])
    monkeypatch.setattr(replay_topup.weather, "summary", lambda *a, **k: {})
    return path


def replay(cfg, db, start, end, tmp_path, **kw):
    return replay_topup.Replay(cfg, db, start, end,
                               workdir=str(tmp_path / "work"), **kw).run()


def test_it_reads_the_window_and_writes_nothing_to_the_night(cfg, db, night,
                                                             tmp_path):
    r = replay(cfg, db, night, night + 7200, tmp_path)
    assert r.live is not None
    # The learning gate is shut on a database this thin, so nothing is
    # permitted - which is itself the right answer and is recorded as one.
    assert all(v is None for _ts, v, *_ in r.writes)


def test_the_opening_thresholds_are_the_config_defaults_without_a_record(
        cfg, db, night, tmp_path):
    r = replay(cfg, db, night, night + 7200, tmp_path)
    assert r.live["mep_start"] == cfg["default_start"]
    assert r.live["kub_stop"] == cfg["default_stop"]


def test_owner_writes_are_applied_at_their_minute(cfg, db, night, tmp_path):
    r = replay(cfg, db, night, night + 7200, tmp_path,
               owner_writes=[{"at": night + 1800, "kub_start": 53.0,
                              "note": "by hand"}])
    assert r.live["kub_start"] == 53.0
    assert len(r.owner_events) == 1
    assert r.owner_events[0][0] == night + 1800


def test_the_machine_is_advanced_every_minute(conn, cfg, tmp_path, monkeypatch,
                                              night):
    """The five minute start timeout has to be five real minutes, so the
    machine sees every sample and not only every tick."""
    start = night
    path = str(tmp_path / "h.sqlite")
    out = history.connect(path)
    conn.backup(out)
    out.close()
    monkeypatch.setattr(replay_topup.weather, "hourly", lambda *a, **k: [])
    monkeypatch.setattr(replay_topup.weather, "summary", lambda *a, **k: {})
    r = replay_topup.Replay(cfg, path, start, start + 7200,
                            workdir=str(tmp_path / "w2"))
    r.live = r.opening_thresholds()
    r.guard.adopt_live(dict(r.live), now=start)
    r.topup.roll(start)
    # Ask for a start the pack is under, then let the minutes run.
    r.topup.request("kubota", 54.0, 56.0, start)
    r.run()
    moves = [m for _ts, m in r.transitions]
    assert any(m["to"] == topup.FAILED_TO_START for m in moves)
    when = next(ts for ts, m in r.transitions
                if m["to"] == topup.FAILED_TO_START)
    assert when - start == topup.START_TIMEOUT_SECONDS


def test_a_run_that_reaches_its_stop_is_replayed_as_done(conn, cfg, tmp_path,
                                                         monkeypatch):
    start = ts_at(cfg, "2026-08-30", 20)
    for i in range(120):
        running = 5 <= i < 60
        sample(conn, start + i * 60, 53.8 + i * 0.02, 88,
               kub=history.GEN_RUNNING if running else history.GEN_STOPPED)
    conn.execute(
        "INSERT INTO gen_runs (gen, start_ts, stop_ts, duration_min, start_v, "
        "stop_v, solo, kind) VALUES ('kubota',?,?,55.0,53.9,56.2,1,'auto')",
        (start + 300, start + 3540))
    conn.commit()
    path = str(tmp_path / "h.sqlite")
    out = history.connect(path)
    conn.backup(out)
    out.close()
    monkeypatch.setattr(replay_topup.weather, "hourly", lambda *a, **k: [])
    monkeypatch.setattr(replay_topup.weather, "summary", lambda *a, **k: {})
    r = replay_topup.Replay(cfg, path, start, start + 7200,
                            workdir=str(tmp_path / "w3"))
    r.live = r.opening_thresholds()
    r.guard.adopt_live(dict(r.live), now=start)
    r.topup.roll(start)
    r.topup.request("kubota", 54.0, 56.0, start)
    r.run()
    moves = [m["to"] for _ts, m in r.transitions]
    assert topup.RUNNING in moves and topup.DONE in moves


def test_the_report_says_where_the_replay_stopped_being_the_night(cfg, db,
                                                                  night,
                                                                  tmp_path):
    import io
    r = replay(cfg, db, night, night + 7200, tmp_path)
    out = io.StringIO()
    r.report(out)
    text = out.getvalue()
    assert "state transitions" in text
    assert "against the night as it happened" in text
