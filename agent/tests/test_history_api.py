"""history_api.py: the dashboard's read-only History API on the KAMRUI."""

import json
import threading
import urllib.error
import urllib.request

import pytest

import history
import history_api

DAY = "2026-11-05"          # minute samples
OLD = "2026-07-10"          # InsightLocal hourly only
EMPTY = "2026-10-01"        # nothing at all


def sample(conn, ts, **kw):
    row = dict(battery_v=52.0, batt_soc=70, batt_power=-500.0, batt_current=-9.6,
               ac_power1=400.0, ac_power2=100.0, mppt80_pv=0.0, south_pv=0.0,
               west_pv=0.0, mep_action=10, kub_action=10)
    row.update(kw)
    cols = ",".join(["ts"] + list(row))
    conn.execute(f"INSERT INTO samples ({cols}) VALUES ({','.join('?' * (len(row) + 1))})",
                 [ts] + list(row.values()))


@pytest.fixture
def db(conn, cfg):
    lo, hi = history.day_bounds(DAY, cfg)
    # Every minute from 00:00 to 06:00, then a 30-minute dropout, then to 07:00.
    for ts in list(range(lo, lo + 6 * 3600, 60)) + list(range(lo + 6 * 3600 + 1800, lo + 7 * 3600, 60)):
        sample(conn, ts)
    # MEP runs 01:00-02:00, charging the pack at 3,000 W with 500 W of load.
    for ts in range(lo + 3600, lo + 7200, 60):
        conn.execute("UPDATE samples SET mep_action=9, batt_power=3000 WHERE ts=?", (ts,))
    conn.execute("INSERT INTO gen_runs (gen,start_ts,stop_ts,duration_min,kind,on_reason) "
                 "VALUES ('mep',?,?,60,'auto','manual_on')", (lo + 3600, lo + 7200))
    # Two samples in one minute: the later wins.
    sample(conn, lo + 7 * 3600 + 5, battery_v=53.0, south_pv=1000.0)
    sample(conn, lo + 7 * 3600 + 35, battery_v=53.5, south_pv=1200.0, west_pv=300.0)
    for h in range(24):
        ts = lo + h * 3600
        history.put_hourly(conn, ts, "solar", None, None, 1000.0 if 8 <= h < 16 else 0.0,
                           None, None, None, 60, "live")
        history.put_hourly(conn, ts, "load", None, None, None, 500.0, None, None, 60, "live")
        history.put_hourly(conn, ts, "battery", 52.5, -5.0, 800.0, 300.0, 51.8, 56.0, 60, "live")

    olo, _ = history.day_bounds(OLD, cfg)
    for h in range(24):
        ts = olo + h * 3600
        history.put_hourly(conn, ts, "battery", 53.0, 10.0, 600.0, 100.0, 52.0, 55.0, 60,
                           "insightlocal")
        history.put_hourly(conn, ts, "solar", None, None, 2000.0 if 9 <= h < 15 else 0.0,
                           None, None, None, 60, "insightlocal")
        history.put_hourly(conn, ts, "load", None, None, None, 700.0, None, None, 60,
                           "insightlocal")
        history.put_hourly(conn, ts, "gen", None, None, 1500.0 if h == 3 else 0.0, None,
                           None, None, 60, "insightlocal")
    conn.commit()
    return conn



@pytest.fixture
def now(cfg):
    return history.day_bounds(DAY, cfg)[1] + 86400 * 3


def test_minute_day_is_one_point_per_minute_with_gaps(db, cfg, now):
    out = history_api.day(db, cfg, DAY, now=now)
    s = out["series"]
    assert out["resolution"] == "minute"
    assert len(s["t"]) == len(set(s["t"])) <= 1440
    assert all(t % 60 == 0 for t in s["t"])
    # one null row breaks the line across the 30-minute dropout
    lo = out["start"]
    gap = s["t"].index(lo + 6 * 3600 - 60 + 60)
    assert s["v"][gap] is None and s["t"][gap + 1] == lo + 6 * 3600 + 1800
    # the later of two samples in a minute
    i = s["t"].index(lo + 7 * 3600)
    assert s["v"][i] == 53.5 and s["solar"][i] == 1500 and s["south"][i] == 1200.0
    assert s["load"][0] == 500
    assert not any("temperature" in n.lower() for n in out["notes"])


def test_generator_power_is_estimated_only_while_running(db, cfg, now):
    out = history_api.day(db, cfg, DAY, now=now)
    s, lo = out["series"], out["start"]
    run = s["t"].index(lo + 5400)
    assert s["gen_w"][run] == 3000 + 500 - 0
    assert s["gen_w"][0] == 0
    [r] = out["runs"]
    assert (r["gen"], r["start"], r["stop"], r["minutes"]) == ("mep", lo + 3600, lo + 7200, 60)
    assert r["kind"] == "auto" and r["on_reason"] == "manual_on"


def test_a_run_still_going_is_open_and_ends_at_the_last_sample(db, cfg, now):
    lo, _ = history.day_bounds(DAY, cfg)
    db.execute("UPDATE samples SET kub_action=9 WHERE ts >= ?", (lo + 6 * 3600 + 1800,))
    out = history_api.day(db, cfg, DAY, now=now)
    kub = [r for r in out["runs"] if r["gen"] == "kubota"]
    assert len(kub) == 1 and kub[0]["open"] and kub[0]["start"] == lo + 6 * 3600 + 1800
    assert kub[0]["stop"] == lo + 7 * 3600 + 60


def test_sun_and_summary(db, cfg, now):
    out = history_api.day(db, cfg, DAY, now=now)
    assert out["start"] < out["sun"]["sunrise"] < out["sun"]["sunset"] < out["end"]
    assert out["summary"]["max_v"] == 53.5 and out["summary"]["peak_solar_w"] == 1500


def test_older_day_falls_back_to_hourly(db, cfg, now):
    out = history_api.day(db, cfg, OLD, now=now)
    s = out["series"]
    assert out["resolution"] == "hour" and len(s["t"]) == 24
    assert s["v_min"][0] == 52.0 and s["v_max"][0] == 55.0
    assert s["batt_w"][0] == 500 and s["gen_w"][3] == 1500 and s["solar"][10] == 2000
    assert s["soc"][0] is None and s["mppt80"][0] is None
    assert any("Hourly averages only" in n for n in out["notes"])


def test_empty_day_says_none_and_has_no_points(db, cfg, now):
    out = history_api.day(db, cfg, EMPTY, now=now)
    assert out["resolution"] == "none" and out["series"]["t"] == [] and out["runs"] == []
    assert out["summary"] == {} and out["sun"]


def test_energy_range_mixes_hourly_and_counters(db, cfg, now):
    lo, hi = history.day_bounds(DAY, cfg)
    for ts, kwh in ((lo - 1380, 100.0), (lo + 3600, 101.0), (hi - 1380, 104.5)):
        db.execute("INSERT INTO counters VALUES (?, 'system', 'generator', 'lifetime', ?)",
                   (ts, kwh))
    out = history_api.energy(db, cfg, "2026-11-04", "2026-11-06", now=now)
    by = {d["day"]: d for d in out["days"]}
    assert list(by) == ["2026-11-04", "2026-11-05", "2026-11-06"]
    d = by[DAY]
    assert d["solar_kwh"] == 8.0 and d["load_kwh"] == 12.0
    assert d["batt_in_kwh"] == 19.2 and d["batt_out_kwh"] == 7.2
    assert (d["gen_kwh"], d["gen_source"]) == (4.5, "counter")
    assert d["mep_hours"] == 1.0 and d["kub_hours"] == 0.0
    assert d["min_v"] == 51.8 and d["max_v"] == 56.0 and not d["partial"]
    assert by["2026-11-04"]["solar_kwh"] is None


def test_energy_uses_the_backfill_for_generator_when_it_has_it(db, cfg, now):
    [d] = history_api.energy(db, cfg, OLD, OLD, now=now)["days"]
    assert (d["gen_kwh"], d["gen_source"]) == (1.5, "hourly")
    assert d["solar_kwh"] == 12.0


def test_energy_today_is_partial(db, cfg):
    lo, _ = history.day_bounds(DAY, cfg)
    [d] = history_api.energy(db, cfg, DAY, DAY, now=lo + 7200)["days"]
    assert d["partial"]


@pytest.mark.parametrize("a,b", [("2026-11-06", "2026-11-05"),
                                 ("2025-01-01", "2026-11-05")])
def test_energy_range_limits(db, cfg, a, b):
    with pytest.raises(history_api.BadRequest):
        history_api.energy(db, cfg, a, b)


@pytest.mark.parametrize("v", [None, "", "2026-13-01", "2026-02-30", "yesterday",
                               "2026-11-05'; DROP TABLE samples"])
def test_bad_dates(v):
    with pytest.raises(history_api.BadRequest):
        history_api._date(v)


# --- over HTTP ----------------------------------------------------------------

@pytest.fixture
def server(db, cfg, tmp_path):
    path = tmp_path / "h.sqlite"
    disk = history.connect(str(path))
    db.backup(disk)
    disk.close()
    srv = history_api.HistoryServer(("127.0.0.1", 0), cfg, str(path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", path
    srv.shutdown()
    srv.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_day_and_energy(server):
    base, _ = server
    code, body = get(f"{base}/history/api/day?date={DAY}")
    assert code == 200 and body["resolution"] == "minute" and body["query_ms"] >= 0
    code, body = get(f"{base}/history/api/energy?from=2026-11-01&to=2026-11-07")
    assert code == 200 and len(body["days"]) == 7
    code, body = get(f"{base}/history/api/health")
    assert code == 200 and body["online"] and body["latest_sample"]


def test_http_bad_request_and_unknown_path(server):
    base, _ = server
    assert get(f"{base}/history/api/day?date=nope")[0] == 400
    assert get(f"{base}/history/api/energy?from=2026-11-01")[0] == 400
    assert get(f"{base}/history/api/samples")[0] == 404


def test_http_missing_database_is_503_not_a_crash(server):
    base, path = server
    path.unlink()
    code, body = get(f"{base}/history/api/day?date={DAY}")
    assert code == 503 and body["online"] is False


def test_the_api_never_writes(server):
    base, path = server
    before = path.stat().st_mtime_ns
    get(f"{base}/history/api/day?date={DAY}")
    get(f"{base}/history/api/energy?from=2026-11-01&to=2026-11-07")
    assert path.stat().st_mtime_ns == before
