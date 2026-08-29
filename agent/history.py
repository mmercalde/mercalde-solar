"""SQLite history for the solar agent.

Tables (SPEC section 5):
  samples   one row per /data poll, ~60 s apart, purged after 90 days
  hourly    rollup of samples plus scraped InsightLocal history, per device
  daily     rollup of hourly
  counters  Gateway Modbus energy registers, one snapshot per tick
  gen_runs  derived from mep803aAction / kubotaAction transitions
  plans     the plan record, one per tick
  actions   every guard decision, mirroring audit.log

Times are unix seconds (UTC) everywhere except `daily.day`, which is a local
YYYY-MM-DD string because "yesterday" is a local-calendar idea.
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 60
SAMPLE_RETENTION_DAYS = 90
# A gap longer than this is a dropout, not elapsed time: do not integrate energy across it.
MAX_INTEGRATION_GAP = 300

GEN_RUNNING = 9
GEN_STOPPED = 10
MODE_ON = 1

# /data key -> samples column. The only place raw dashboard keys appear.
SAMPLE_FIELDS = [
    ("batteryVoltage", "battery_v"),
    ("battSocBM", "batt_soc"),
    ("battPower", "batt_power"),
    ("battCurrent", "batt_current"),
    ("battAhRemaining", "batt_ah_remaining"),
    ("battMinToDischarge", "batt_min_to_discharge"),
    ("battMonitorOnline", "batt_monitor_online"),
    ("acPower1", "ac_power1"),
    ("acPower2", "ac_power2"),
    ("mppt80PVPower", "mppt80_pv"),
    ("southArrayPVPower", "south_pv"),
    ("westArrayPVPower", "west_pv"),
    ("mep803aAction", "mep_action"),
    ("kubotaAction", "kub_action"),
    ("mep803aMode", "mep_mode"),
    ("kubotaMode", "kub_mode"),
    ("mepAgsOnline", "mep_ags_online"),
    ("kubotaAgsOnline", "kub_ags_online"),
    ("pollErrors", "poll_errors"),
    ("autoGenEnabled", "auto_gen_enabled"),
    ("lastUpdate", "last_update"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER PRIMARY KEY,
  battery_v REAL, batt_soc REAL, batt_power REAL, batt_current REAL,
  batt_ah_remaining REAL, batt_min_to_discharge INTEGER, batt_monitor_online INTEGER,
  ac_power1 REAL, ac_power2 REAL,
  mppt80_pv REAL, south_pv REAL, west_pv REAL,
  mep_action INTEGER, kub_action INTEGER, mep_mode INTEGER, kub_mode INTEGER,
  mep_ags_online INTEGER, kub_ags_online INTEGER,
  poll_errors INTEGER, auto_gen_enabled INTEGER, last_update TEXT
);

CREATE TABLE IF NOT EXISTS hourly (
  hour_ts INTEGER NOT NULL,
  device TEXT NOT NULL,
  mean_v REAL, mean_a REAL, wh_in REAL, wh_out REAL,
  min_v REAL, max_v REAL, n INTEGER,
  source TEXT NOT NULL,
  PRIMARY KEY (hour_ts, device)
);

CREATE TABLE IF NOT EXISTS daily (
  day TEXT PRIMARY KEY,
  solar_wh REAL, load_wh REAL,
  mep_minutes REAL, kub_minutes REAL,
  peak_v REAL, min_v REAL
);

CREATE TABLE IF NOT EXISTS counters (
  ts INTEGER NOT NULL, device TEXT NOT NULL, counter TEXT NOT NULL,
  period TEXT NOT NULL, kwh REAL,
  PRIMARY KEY (ts, device, counter, period)
);

CREATE TABLE IF NOT EXISTS gen_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gen TEXT NOT NULL, start_ts INTEGER NOT NULL, stop_ts INTEGER,
  duration_min REAL, start_v REAL, stop_v REAL,
  rate_v_per_h REAL, rate_a REAL, solo INTEGER, kind TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS gen_runs_key ON gen_runs (gen, start_ts);

-- Voltage-to-SOC observations, as a histogram rather than minute rows.
-- The backfill sees SOC at one-minute resolution but SPEC section 5 forbids
-- storing minute rows, and an hourly mean would blur exactly the curve we are
-- trying to learn. Counting (voltage bin, SOC) pairs keeps the distribution,
-- so the median at a given voltage stays exact, in a few tens of thousands of
-- rows for years of history. `day` is in the key so re-scraping a day
-- replaces its contribution instead of double-counting it.
CREATE TABLE IF NOT EXISTS soc_curve (
  day TEXT NOT NULL,
  v_bin INTEGER NOT NULL,
  soc INTEGER NOT NULL,
  source TEXT NOT NULL,
  n INTEGER NOT NULL,
  PRIMARY KEY (day, v_bin, soc, source)
);
CREATE INDEX IF NOT EXISTS soc_curve_bin ON soc_curve (v_bin);

CREATE TABLE IF NOT EXISTS plans (
  ts INTEGER PRIMARY KEY, text TEXT NOT NULL, data TEXT
);

CREATE TABLE IF NOT EXISTS actions (
  ts INTEGER NOT NULL, tool TEXT, args TEXT, allowed INTEGER,
  reason TEXT, voltage REAL, soc REAL, result TEXT
);
CREATE INDEX IF NOT EXISTS actions_ts ON actions (ts);
"""


def connect(path=None):
    conn = sqlite3.connect(path or config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


# A sqlite3 connection may only be used by the thread that made it. The agent
# runs jobs on APScheduler worker threads, a Telegram poll thread and the ask
# server's handler threads, so a single shared connection raises
# ProgrammingError as soon as anything runs off the main thread. Each thread
# gets its own connection instead; WAL mode lets them read while one writes.
_local = threading.local()


def thread_connection(path=None):
    """The calling thread's connection, opened on first use in that thread."""
    key = path or config.DB_PATH
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    conn = cache.get(key)
    if conn is None:
        conn = cache[key] = connect(key)
    return conn


def readonly_connection(path=None):
    """A fresh read-only connection. Caller closes it.

    Used for one-off reads from short-lived threads, where caching a
    connection per thread would leak one per request.
    """
    path = path or config.DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def resolve(conn):
    """Accept either a connection or a zero-argument provider of one.

    Long-lived objects (the load model, the guard, the tool registry) are
    built once on the main thread but used from several others, so they hold
    a provider and resolve it per call.

    Note a sqlite3.Connection is itself callable, so the test is on the type,
    not on callable().
    """
    if isinstance(conn, sqlite3.Connection):
        return conn
    return conn()


# --- time helpers -----------------------------------------------------------

def tzinfo(cfg):
    return ZoneInfo(cfg["tz"])


def local(ts, cfg):
    return datetime.fromtimestamp(ts, tzinfo(cfg))


def local_day(ts, cfg):
    return local(ts, cfg).strftime("%Y-%m-%d")


def day_bounds(day, cfg):
    """Unix-second [start, end) of a local YYYY-MM-DD."""
    tz = tzinfo(cfg)
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def hour_floor(ts):
    return ts - (ts % 3600)


# --- sampling ---------------------------------------------------------------

def fetch_data(cfg, timeout=10):
    r = requests.get(cfg["dashboard_url"] + "/data", timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_config(cfg, timeout=10):
    """Live generator thresholds. Returns the inner 'config' object."""
    r = requests.get(cfg["dashboard_url"] + "/config", timeout=timeout)
    r.raise_for_status()
    return r.json()["config"]


def record_sample(conn, data, ts=None):
    """Insert one /data snapshot. Returns the row's ts, or None if it was a duplicate."""
    ts = int(ts if ts is not None else time.time())
    cols, vals = ["ts"], [ts]
    for key, col in SAMPLE_FIELDS:
        v = data.get(key)
        if isinstance(v, bool):
            v = int(v)
        cols.append(col)
        vals.append(v)
    sql = f"INSERT OR IGNORE INTO samples ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    cur = conn.execute(sql, vals)
    conn.commit()
    return ts if cur.rowcount else None


def purge_samples(conn, now=None):
    cutoff = int(now or time.time()) - SAMPLE_RETENTION_DAYS * 86400
    cur = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# --- generator runs ---------------------------------------------------------

GENS = {"mep": ("mep_action", "kub_action", "mep_mode"),
        "kubota": ("kub_action", "mep_action", "kub_mode")}


def _is_exercise(start_ts, duration_min, cfg):
    """SPEC section 5: starts 09:00-09:05 local and lasts <= 35 min."""
    ex = cfg["exercise"]
    hh, mm = (int(x) for x in ex["start"].split(":"))
    t = local(start_ts, cfg)
    window_start = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    in_window = window_start <= t <= window_start + timedelta(minutes=5)
    return in_window and duration_min <= ex["minutes"] + 5


def _classify(conn, gen, start_ts, duration_min, mode_at_start, cfg):
    if _is_exercise(start_ts, duration_min, cfg):
        return "exercise"
    if mode_at_start == MODE_ON:
        return "manual"
    # An agent run is one the agent caused by raising this gen's start threshold.
    key = "mep_start" if gen == "mep" else "kub_start"
    row = conn.execute(
        "SELECT args FROM actions WHERE tool='set_gen_thresholds' AND allowed=1 "
        "AND ts <= ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
        (start_ts, start_ts - 6 * 3600),
    ).fetchone()
    if row:
        try:
            args = json.loads(row["args"])
        except (json.JSONDecodeError, TypeError):
            args = {}
        if args.get(key) is not None and args[key] > cfg["default_start"]:
            return "agent"
    return "auto"


def derive_gen_runs(conn, cfg, since=None):
    """Walk samples and append any generator run that has closed.

    Idempotent: runs are keyed on (gen, start_ts), and scanning restarts from
    the last recorded stop so a re-run cannot double-count.
    """
    added = 0
    for gen, (col, other_col, mode_col) in GENS.items():
        start_from = since
        if start_from is None:
            row = conn.execute(
                "SELECT MAX(COALESCE(stop_ts, start_ts)) AS t FROM gen_runs WHERE gen=?",
                (gen,)).fetchone()
            start_from = (row["t"] or 0) + 1
        rows = conn.execute(
            f"SELECT ts, {col} AS act, {other_col} AS other, {mode_col} AS mode, "
            "battery_v, batt_current FROM samples WHERE ts >= ? ORDER BY ts",
            (start_from,)).fetchall()

        open_run = None
        for r in rows:
            running = r["act"] == GEN_RUNNING
            if running:
                if open_run is None:
                    open_run = {"start_ts": r["ts"], "start_v": r["battery_v"],
                                "mode": r["mode"], "solo": True, "currents": []}
                if r["other"] == GEN_RUNNING:
                    open_run["solo"] = False
                # Only samples taken while this gen was running describe its charge rate.
                if r["batt_current"] is not None:
                    open_run["currents"].append(r["batt_current"])
            elif open_run is not None:
                added += _close_run(conn, cfg, gen, open_run, r)
                open_run = None
        # An open run is left unwritten; the next pass picks it up once it closes.
    conn.commit()
    return added


def _close_run(conn, cfg, gen, run, stop_row):
    duration_min = (stop_row["ts"] - run["start_ts"]) / 60.0
    if duration_min <= 0:
        return 0
    start_v, stop_v = run["start_v"], stop_row["battery_v"]
    rate_v_per_h = None
    if start_v is not None and stop_v is not None and duration_min > 0:
        rate_v_per_h = (stop_v - start_v) / (duration_min / 60.0)
    rate_a = sum(run["currents"]) / len(run["currents"]) if run["currents"] else None
    kind = _classify(conn, gen, run["start_ts"], duration_min, run["mode"], cfg)
    cur = conn.execute(
        "INSERT OR IGNORE INTO gen_runs "
        "(gen, start_ts, stop_ts, duration_min, start_v, stop_v, rate_v_per_h, rate_a, solo, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (gen, run["start_ts"], stop_row["ts"], round(duration_min, 2), start_v, stop_v,
         rate_v_per_h, rate_a, int(run["solo"]), kind))
    return cur.rowcount


def gen_running_hours(conn, start_ts, end_ts):
    """Hours in [start_ts, end_ts) overlapped by any generator run.

    The load model excludes these: AC output is not house load while a
    generator is feeding the inverters.
    """
    hours = set()
    rows = conn.execute(
        "SELECT start_ts, COALESCE(stop_ts, start_ts) AS stop_ts FROM gen_runs "
        "WHERE COALESCE(stop_ts, start_ts) >= ? AND start_ts < ?",
        (start_ts, end_ts)).fetchall()
    for r in rows:
        h = hour_floor(r["start_ts"])
        while h <= r["stop_ts"]:
            hours.add(h)
            h += 3600
    return hours


# --- rollups ----------------------------------------------------------------

# device -> (power column expression, is_solar)
POWER_DEVICES = [
    ("mppt80", "mppt80_pv"), ("south", "south_pv"), ("west", "west_pv"),
]


def rollup_hourly(conn, cfg, since=None):
    """Aggregate samples into hourly rows. Only 'live' rows are rewritten;
    scraped InsightLocal rows are never clobbered."""
    if since is None:
        row = conn.execute(
            "SELECT MAX(hour_ts) AS h FROM hourly WHERE source='live'").fetchone()
        since = row["h"] or 0
    rows = conn.execute(
        "SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    if not rows:
        return 0

    buckets = {}
    prev_ts = None
    for r in rows:
        dt = 0 if prev_ts is None else min(r["ts"] - prev_ts, MAX_INTEGRATION_GAP)
        prev_ts = r["ts"]
        h = hour_floor(r["ts"])
        b = buckets.setdefault(h, {
            "n": 0, "v": [], "a": [],
            "batt_in": 0.0, "batt_out": 0.0, "load": 0.0,
            "solar": 0.0, "mppt80": 0.0, "south": 0.0, "west": 0.0,
        })
        b["n"] += 1
        if r["battery_v"] is not None:
            b["v"].append(r["battery_v"])
        if r["batt_current"] is not None:
            b["a"].append(r["batt_current"])
        hours = dt / 3600.0
        p = r["batt_power"]
        if p is not None:
            if p >= 0:
                b["batt_in"] += p * hours
            else:
                b["batt_out"] += -p * hours
        ac = (r["ac_power1"] or 0) + (r["ac_power2"] or 0)
        b["load"] += ac * hours
        total_pv = 0.0
        for dev, col in POWER_DEVICES:
            w = r[col] or 0
            b[dev] += w * hours
            total_pv += w
        b["solar"] += total_pv * hours

    written = 0
    for h, b in buckets.items():
        mean_v = sum(b["v"]) / len(b["v"]) if b["v"] else None
        mean_a = sum(b["a"]) / len(b["a"]) if b["a"] else None
        min_v = min(b["v"]) if b["v"] else None
        max_v = max(b["v"]) if b["v"] else None
        written += put_hourly(conn, h, "battery", mean_v, mean_a,
                               b["batt_in"], b["batt_out"], min_v, max_v, b["n"], "live")
        written += put_hourly(conn, h, "load", None, None, None, b["load"],
                               None, None, b["n"], "live")
        written += put_hourly(conn, h, "solar", None, None, b["solar"], None,
                               None, None, b["n"], "live")
        for dev, _ in POWER_DEVICES:
            written += put_hourly(conn, h, dev, None, None, b[dev], None,
                                   None, None, b["n"], "live")
    conn.commit()
    return written


def put_hourly(conn, hour_ts, device, mean_v, mean_a, wh_in, wh_out,
                min_v, max_v, n, source):
    existing = conn.execute(
        "SELECT source FROM hourly WHERE hour_ts=? AND device=?",
        (hour_ts, device)).fetchone()
    if existing and existing["source"] == "live" and source != "live":
        # Live sampling is authoritative: a backfill scrape never overwrites it.
        return 0
    conn.execute(
        "INSERT INTO hourly (hour_ts, device, mean_v, mean_a, wh_in, wh_out, "
        "min_v, max_v, n, source) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(hour_ts, device) DO UPDATE SET "
        "mean_v=excluded.mean_v, mean_a=excluded.mean_a, wh_in=excluded.wh_in, "
        "wh_out=excluded.wh_out, min_v=excluded.min_v, max_v=excluded.max_v, "
        "n=excluded.n, source=excluded.source",
        (hour_ts, device, mean_v, mean_a, wh_in, wh_out, min_v, max_v, n, source))
    return 1


def rollup_daily(conn, cfg, days=None):
    """Rebuild `daily` for the given local days (default: every day present in hourly)."""
    if days is None:
        rows = conn.execute("SELECT MIN(hour_ts) a, MAX(hour_ts) b FROM hourly").fetchone()
        if not rows or rows["a"] is None:
            return 0
        days = []
        d = local(rows["a"], cfg).date()
        end = local(rows["b"], cfg).date()
        while d <= end:
            days.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)

    written = 0
    for day in days:
        lo, hi = day_bounds(day, cfg)
        agg = conn.execute(
            "SELECT "
            " (SELECT SUM(wh_in)  FROM hourly WHERE device='solar' AND hour_ts>=? AND hour_ts<?) solar,"
            " (SELECT SUM(wh_out) FROM hourly WHERE device='load'  AND hour_ts>=? AND hour_ts<?) load,"
            " (SELECT MAX(max_v)  FROM hourly WHERE device='battery' AND hour_ts>=? AND hour_ts<?) peak,"
            " (SELECT MIN(min_v)  FROM hourly WHERE device='battery' AND hour_ts>=? AND hour_ts<?) minv",
            (lo, hi, lo, hi, lo, hi, lo, hi)).fetchone()
        mins = {}
        for gen in GENS:
            rows = conn.execute(
                "SELECT start_ts, COALESCE(stop_ts, start_ts) stop_ts FROM gen_runs "
                "WHERE gen=? AND COALESCE(stop_ts, start_ts) >= ? AND start_ts < ?",
                (gen, lo, hi)).fetchall()
            mins[gen] = sum(max(0, min(r["stop_ts"], hi) - max(r["start_ts"], lo))
                            for r in rows) / 60.0
        conn.execute(
            "INSERT INTO daily (day, solar_wh, load_wh, mep_minutes, kub_minutes, peak_v, min_v) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
            "solar_wh=excluded.solar_wh, load_wh=excluded.load_wh, "
            "mep_minutes=excluded.mep_minutes, kub_minutes=excluded.kub_minutes, "
            "peak_v=excluded.peak_v, min_v=excluded.min_v",
            (day, agg["solar"], agg["load"], mins["mep"], mins["kubota"],
             agg["peak"], agg["minv"]))
        written += 1
    conn.commit()
    return written


# --- reads used by tools and the plan record --------------------------------

def latest_sample(conn):
    return conn.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()


def summary(conn, hours, now=None):
    """min/max/avg V, solar Wh, load Wh and generator minutes over the last N hours."""
    now = int(now or time.time())
    lo = now - hours * 3600
    v = conn.execute(
        "SELECT MIN(battery_v) minv, MAX(battery_v) maxv, AVG(battery_v) avgv, "
        "MIN(batt_soc) minsoc, MAX(batt_soc) maxsoc, COUNT(*) n "
        "FROM samples WHERE ts >= ?", (lo,)).fetchone()
    e = conn.execute(
        "SELECT "
        " (SELECT SUM(wh_in)  FROM hourly WHERE device='solar' AND hour_ts>=?) solar,"
        " (SELECT SUM(wh_out) FROM hourly WHERE device='load'  AND hour_ts>=?) load",
        (hour_floor(lo), hour_floor(lo))).fetchone()
    gen = {}
    for g in GENS:
        rows = conn.execute(
            "SELECT start_ts, COALESCE(stop_ts, start_ts) stop_ts FROM gen_runs "
            "WHERE gen=? AND COALESCE(stop_ts, start_ts) >= ?", (g, lo)).fetchall()
        gen[g] = round(sum(max(0, min(r["stop_ts"], now) - max(r["start_ts"], lo))
                           for r in rows) / 60.0, 1)
    return {
        "hours": hours,
        "samples": v["n"],
        "min_v": _r(v["minv"], 2), "max_v": _r(v["maxv"], 2), "avg_v": _r(v["avgv"], 2),
        "min_soc": v["minsoc"], "max_soc": v["maxsoc"],
        "solar_wh": _r(e["solar"], 0), "load_wh": _r(e["load"], 0),
        "gen_minutes": gen,
    }


def gen_runs(conn, days, now=None, include_exercise=False):
    now = int(now or time.time())
    lo = now - days * 86400
    sql = "SELECT * FROM gen_runs WHERE start_ts >= ?"
    if not include_exercise:
        sql += " AND kind != 'exercise'"
    return conn.execute(sql + " ORDER BY start_ts", (lo,)).fetchall()


def record_plan(conn, text, data=None, ts=None):
    ts = int(ts or time.time())
    conn.execute("INSERT OR REPLACE INTO plans (ts, text, data) VALUES (?,?,?)",
                 (ts, text, json.dumps(data or {})))
    conn.commit()
    return ts


def latest_plan(conn):
    return conn.execute("SELECT * FROM plans ORDER BY ts DESC LIMIT 1").fetchone()


# Voltage bin width for the learned voltage-to-SOC curve, in volts. Shared by
# the scraper (which fills soc_curve) and the load model (which reads it).
SOC_BIN_V = 0.05


def soc_bin(volts):
    return int(round(volts / SOC_BIN_V))


def soc_bin_volts(v_bin):
    return v_bin * SOC_BIN_V


def record_soc_observations(conn, day, counts, source="insightlocal"):
    """Replace one day's contribution to the voltage-to-SOC histogram.

    `counts` maps (v_bin, soc) -> number of observations.
    """
    conn.execute("DELETE FROM soc_curve WHERE day=? AND source=?", (day, source))
    if counts:
        conn.executemany(
            "INSERT INTO soc_curve (day, v_bin, soc, source, n) VALUES (?,?,?,?,?)",
            [(day, v_bin, soc, source, n) for (v_bin, soc), n in counts.items()])
    conn.commit()
    return len(counts)


def soc_histogram(conn, v_bin_lo, v_bin_hi):
    """[(v_bin, soc, n)] from the scraped histogram, over a bin range."""
    return conn.execute(
        "SELECT v_bin, soc, SUM(n) AS n FROM soc_curve "
        "WHERE v_bin BETWEEN ? AND ? GROUP BY v_bin, soc",
        (v_bin_lo, v_bin_hi)).fetchall()


def soc_curve_span(conn):
    """(lowest volts, highest volts, total observations) in the histogram."""
    row = conn.execute(
        "SELECT MIN(v_bin) lo, MAX(v_bin) hi, SUM(n) n FROM soc_curve").fetchone()
    if not row or row["lo"] is None:
        return None
    return soc_bin_volts(row["lo"]), soc_bin_volts(row["hi"]), row["n"]


def record_action(conn, tool, args, allowed, reason, voltage, soc, result, ts=None):
    ts = int(ts or time.time())
    conn.execute(
        "INSERT INTO actions (ts, tool, args, allowed, reason, voltage, soc, result) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts, tool, json.dumps(args, sort_keys=True), int(bool(allowed)),
         reason, voltage, soc, result))
    conn.commit()
    return ts


def _r(v, places):
    return None if v is None else round(v, places)


# --- sampler entry point ----------------------------------------------------

def poll_once(conn, cfg, now=None):
    """One /data poll into samples, then keep gen_runs current."""
    data = fetch_data(cfg)
    ts = record_sample(conn, data, ts=now)
    if ts is not None:
        derive_gen_runs(conn, cfg)
    return ts, data


def main():
    """Standalone sampler: poll /data every 60 s, roll up hourly, purge nightly."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()
    conn = connect()
    last_rollup = 0
    while True:
        started = time.time()
        try:
            ts, _ = poll_once(conn, cfg)
            log.debug("sampled %s", ts)
        except (requests.RequestException, sqlite3.Error) as e:
            log.warning("sample failed: %s", e)
        if started - last_rollup > 900:
            try:
                rollup_hourly(conn, cfg)
                rollup_daily(conn, cfg, days=[local_day(int(started), cfg)])
                purge_samples(conn)
                last_rollup = started
            except sqlite3.Error as e:
                log.warning("rollup failed: %s", e)
        time.sleep(max(1, SAMPLE_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
