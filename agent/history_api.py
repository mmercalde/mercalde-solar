"""Read-only history API for the dashboard's History section.

Its own process and its own unit (solar-history.service), not part of
solar-agent: shipping or crashing it never restarts the agent, and an agent
restart never takes history down. It opens history.sqlite read-only on every
request and writes nothing anywhere.

  GET /history/api/health                     {"online": true, "latest_sample": ts}
  GET /history/api/day?date=YYYY-MM-DD        one local day, columnar, <= 1 point/min
  GET /history/api/energy?from=D&to=D         daily energy bars, at most 400 days

Bound to the KAMRUI LAN address, like the ask server. The Pi5 proxies
/history/api/* to it; nothing else should call it.

Every time in a response is unix seconds. The browser formats them in
America/Tijuana; `tz` says so in each response.
"""

import argparse
import bisect
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import history
import sun

log = logging.getLogger(__name__)

BIND_HOST = "192.168.3.152"
PORT = 8091
MAX_ENERGY_DAYS = 400
# A gap in the minute samples longer than this is drawn as a gap, not a line.
GAP_SECONDS = 300
# Hours from the InsightLocal backfill, for the days before minute samples.
HOURLY_DEVICES = ("battery", "solar", "load", "gen", "mppt80", "south", "west")
GENS = (("mep", "mep_action"), ("kubota", "kub_action"))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BadRequest(ValueError):
    pass


def _date(value, name="date"):
    if not value or not DATE_RE.match(value):
        raise BadRequest(f"{name} must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise BadRequest(f"{name} is not a real date: {value}")
    return value


def _r(x, n=0):
    if x is None:
        return None
    return round(x, n) if n else int(round(x))


# --- day ---------------------------------------------------------------------

def _minute_series(rows, lo, hi):
    """Samples -> columnar arrays, one point per minute at most.

    Samples are ~60 s apart; if two land in the same minute the later wins.
    A null row is inserted into any gap longer than GAP_SECONDS so the chart
    breaks the line instead of drawing a straight one across a dropout.
    """
    by_min = {}
    for r in rows:
        by_min[r["ts"] - r["ts"] % 60] = r
    cols = {k: [] for k in ("t", "v", "a", "batt_w", "soc", "mppt80", "south",
                            "west", "solar", "load", "gen_w", "mep", "kub")}

    def blank(t):
        cols["t"].append(t)
        for k in cols:
            if k != "t":
                cols[k].append(None)

    prev = None
    for t in sorted(by_min):
        r = by_min[t]
        if prev is not None and t - prev > GAP_SECONDS:
            blank(prev + 60)
        prev = t
        pv = [r["mppt80_pv"], r["south_pv"], r["west_pv"]]
        solar = sum(p or 0 for p in pv) if any(p is not None for p in pv) else None
        load = None
        if r["ac_power1"] is not None or r["ac_power2"] is not None:
            load = (r["ac_power1"] or 0) + (r["ac_power2"] or 0)
        mep = r["mep_action"] == history.GEN_RUNNING
        kub = r["kub_action"] == history.GEN_RUNNING
        # Not sampled. Estimated the way gen_runs.gross_w is: what went into
        # the pack plus what the house took, less what the sun supplied.
        gen_w = None
        if (mep or kub) and r["batt_power"] is not None and load is not None:
            gen_w = max(0, int(round(r["batt_power"] + load - (solar or 0))))
        elif not (mep or kub):
            gen_w = 0
        cols["t"].append(t)
        cols["v"].append(r["battery_v"])
        cols["a"].append(r["batt_current"])
        cols["batt_w"].append(r["batt_power"])
        cols["soc"].append(r["batt_soc"])
        cols["mppt80"].append(r["mppt80_pv"])
        cols["south"].append(r["south_pv"])
        cols["west"].append(r["west_pv"])
        cols["solar"].append(solar)
        cols["load"].append(load)
        cols["gen_w"].append(gen_w)
        cols["mep"].append(1 if mep else 0)
        cols["kub"].append(1 if kub else 0)
    return cols


def _hourly_series(conn, lo, hi):
    """The InsightLocal backfill's hours, as mean watts, for older days."""
    rows = conn.execute(
        "SELECT * FROM hourly WHERE hour_ts >= ? AND hour_ts < ? ORDER BY hour_ts",
        (lo, hi)).fetchall()
    hours = {}
    for r in rows:
        hours.setdefault(r["hour_ts"], {})[r["device"]] = r
    cols = {k: [] for k in ("t", "v", "v_min", "v_max", "a", "batt_w", "soc",
                            "mppt80", "south", "west", "solar", "load", "gen_w",
                            "mep", "kub")}
    for h in sorted(hours):
        d = hours[h]
        b = d.get("battery")

        def wh(dev, field):
            row = d.get(dev)
            return None if row is None or row[field] is None else _r(row[field])

        cols["t"].append(h)
        cols["v"].append(b["mean_v"] if b else None)
        cols["v_min"].append(b["min_v"] if b else None)
        cols["v_max"].append(b["max_v"] if b else None)
        cols["a"].append(_r(b["mean_a"], 1) if b and b["mean_a"] is not None else None)
        bw = None
        if b and (b["wh_in"] is not None or b["wh_out"] is not None):
            bw = _r((b["wh_in"] or 0) - (b["wh_out"] or 0))
        cols["batt_w"].append(bw)
        cols["soc"].append(None)
        for dev in ("mppt80", "south", "west"):
            cols[dev].append(wh(dev, "wh_in"))
        cols["solar"].append(wh("solar", "wh_in"))
        cols["load"].append(wh("load", "wh_out"))
        cols["gen_w"].append(wh("gen", "wh_in"))
        cols["mep"].append(None)
        cols["kub"].append(None)
    return cols


def _runs(conn, lo, hi, cols, now):
    """Generator runs overlapping the day, clipped to it.

    From the minute samples where there are any, so a run still going is
    there before gen_runs has closed it, and annotated from gen_runs (kind,
    on-reason) where a recorded run overlaps. Older days have gen_runs only.
    """
    recorded = conn.execute(
        "SELECT gen, start_ts, stop_ts, kind, on_reason, duration_min, start_v, stop_v "
        "FROM gen_runs WHERE COALESCE(stop_ts, ?) >= ? AND start_ts < ? "
        "ORDER BY start_ts", (now, lo, hi)).fetchall()
    out = []
    if cols["t"] and cols.get("mep") and cols["mep"][0] is not None:
        for gen, key in (("mep", "mep"), ("kubota", "kub")):
            start = None
            for i, t in enumerate(cols["t"]):
                on = cols[key][i] == 1
                if on and start is None:
                    start = t
                elif not on and start is not None and cols[key][i] is not None:
                    out.append({"gen": gen, "start": start, "stop": t})
                    start = None
            if start is not None:
                out.append({"gen": gen, "start": start,
                            "stop": min(cols["t"][-1] + 60, hi), "open": True})
    else:
        for r in recorded:
            out.append({"gen": r["gen"], "start": max(r["start_ts"], lo),
                        "stop": min(r["stop_ts"] or now, hi)})
    for run in out:
        best = None
        for r in recorded:
            if r["gen"] != run["gen"]:
                continue
            overlap = min(r["stop_ts"] or now, run["stop"]) - max(r["start_ts"], run["start"])
            if overlap > -120 and (best is None or overlap > best[0]):
                best = (overlap, r)
        if best:
            r = best[1]
            run.update(kind=r["kind"], on_reason=r["on_reason"],
                       start_v=r["start_v"], stop_v=r["stop_v"])
        run["minutes"] = round((run["stop"] - run["start"]) / 60.0)
    out.sort(key=lambda r: r["start"])
    return out


def day(conn, cfg, date, now=None):
    now = int(now or time.time())
    lo, hi = history.day_bounds(date, cfg)
    rows = conn.execute(
        "SELECT * FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts", (lo, hi)).fetchall()
    notes = []
    if rows:
        resolution = "minute"
        cols = _minute_series(rows, lo, hi)
        notes.append("Generator power is estimated: battery + load - solar, "
                     "on minutes a generator was running.")
    else:
        cols = _hourly_series(conn, lo, hi)
        resolution = "hour" if cols["t"] else "none"
        if resolution == "hour":
            notes.append("Hourly averages only: minute samples start "
                         "2026-08-28 and are kept for 90 days.")
    notes.append("Battery temperature is not recorded.")
    st = sun.sun_times(cfg["lat"], cfg["lon"], date, history.tzinfo(cfg))

    summary = {}
    vs = [v for v in cols["v"] if v is not None]
    if vs:
        summary["min_v"], summary["max_v"] = min(vs), max(vs)
    if cols["solar"]:
        pts = [(w, t) for w, t in zip(cols["solar"], cols["t"]) if w is not None]
        if pts:
            w, t = max(pts)
            summary["peak_solar_w"], summary["peak_solar_at"] = w, t
    socs = [s for s in cols["soc"] if s is not None]
    if socs:
        summary["min_soc"], summary["max_soc"] = min(socs), max(socs)

    return {
        "date": date, "tz": cfg["tz"], "start": lo, "end": hi,
        "resolution": resolution,
        "sun": {"sunrise": st[0], "sunset": st[1]} if st else None,
        "series": cols,
        "runs": _runs(conn, lo, hi, cols, now),
        "summary": summary,
        "notes": notes,
    }


# --- energy ------------------------------------------------------------------

def _days(start, end):
    d = datetime.strptime(start, "%Y-%m-%d").date()
    stop = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= stop:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def _lifetime_before(conn, counter, ts):
    row = conn.execute(
        "SELECT kwh FROM counters WHERE device='system' AND counter=? "
        "AND period='lifetime' AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (counter, ts)).fetchone()
    return None if row is None else row["kwh"]


def energy(conn, cfg, start, end, now=None):
    now = int(now or time.time())
    days = list(_days(start, end))
    if not days:
        raise BadRequest("from is after to")
    if len(days) > MAX_ENERGY_DAYS:
        raise BadRequest(f"at most {MAX_ENERGY_DAYS} days")
    lo = history.day_bounds(days[0], cfg)[0]
    hi = history.day_bounds(days[-1], cfg)[1]
    rows = conn.execute(
        "SELECT hour_ts, device, wh_in, wh_out, min_v, max_v FROM hourly "
        "WHERE hour_ts >= ? AND hour_ts < ? AND device IN "
        "('solar','load','gen','battery')", (lo, hi)).fetchall()
    runs = conn.execute(
        "SELECT gen, start_ts, COALESCE(stop_ts, ?) stop_ts FROM gen_runs "
        "WHERE COALESCE(stop_ts, ?) >= ? AND start_ts < ?",
        (now, now, lo, hi)).fetchall()

    bounds = [history.day_bounds(d, cfg) for d in days]
    starts = [b[0] for b in bounds]
    by_day = [[] for _ in days]
    for r in rows:
        by_day[bisect.bisect_right(starts, r["hour_ts"]) - 1].append(r)

    out = []
    for i, d in enumerate(days):
        dlo, dhi = bounds[i]
        acc = {"solar": None, "load": None, "gen": None, "batt_in": None,
               "batt_out": None, "min_v": None, "max_v": None}

        def add(k, x):
            if x is not None:
                acc[k] = (acc[k] or 0) + x
        for r in by_day[i]:
            dev = r["device"]
            if dev == "solar":
                add("solar", r["wh_in"])
            elif dev == "load":
                add("load", r["wh_out"])
            elif dev == "gen":
                add("gen", r["wh_in"])
            elif dev == "battery":
                add("batt_in", r["wh_in"])
                add("batt_out", r["wh_out"])
                if r["min_v"] is not None:
                    acc["min_v"] = r["min_v"] if acc["min_v"] is None else min(acc["min_v"], r["min_v"])
                if r["max_v"] is not None:
                    acc["max_v"] = r["max_v"] if acc["max_v"] is None else max(acc["max_v"], r["max_v"])
        gen_src = "hourly" if acc["gen"] is not None else None
        # Live sampling has no generator Wh. The gateway's lifetime counter,
        # read hourly at :37, differenced across the day: 11:37 pm to 11:37 pm.
        if acc["gen"] is None:
            a = _lifetime_before(conn, "generator", dlo)
            b = _lifetime_before(conn, "generator", min(dhi, now))
            if a is not None and b is not None and b >= a:
                acc["gen"] = (b - a) * 1000.0
                gen_src = "counter"
        hours = {}
        for g in ("mep", "kubota"):
            secs = sum(max(0, min(r["stop_ts"], dhi) - max(r["start_ts"], dlo))
                       for r in runs if r["gen"] == g)
            hours[g] = round(secs / 3600.0, 2)
        kwh = lambda x: None if x is None else round(x / 1000.0, 2)  # noqa: E731
        out.append({
            "day": d,
            "solar_kwh": kwh(acc["solar"]), "load_kwh": kwh(acc["load"]),
            "gen_kwh": kwh(acc["gen"]), "gen_source": gen_src,
            "batt_in_kwh": kwh(acc["batt_in"]), "batt_out_kwh": kwh(acc["batt_out"]),
            "mep_hours": hours["mep"], "kub_hours": hours["kubota"],
            "min_v": acc["min_v"], "max_v": acc["max_v"],
            "partial": dhi > now,
        })
    return {"from": days[0], "to": days[-1], "tz": cfg["tz"], "days": out}


def health(conn):
    row = conn.execute("SELECT MAX(ts) t FROM samples").fetchone()
    return {"online": True, "latest_sample": row["t"] if row else None}


# --- HTTP --------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "solar-history"

    def log_message(self, fmt, *args):
        log.debug("history_api %s - %s", self.address_string(), fmt % args)

    def _send(self, code, obj):
        payload = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path.rstrip("/")
        t0 = time.perf_counter()
        conn = None
        try:
            conn = history.readonly_connection(self.server.db_path)
            cfg = self.server.cfg
            if route == "/history/api/health":
                body = health(conn)
            elif route == "/history/api/day":
                body = day(conn, cfg, _date(q.get("date")))
            elif route == "/history/api/energy":
                body = energy(conn, cfg, _date(q.get("from"), "from"),
                              _date(q.get("to"), "to"))
            else:
                self._send(404, {"error": "not found"})
                return
        except BadRequest as e:
            self._send(400, {"error": str(e)})
            return
        except sqlite3.Error as e:
            log.warning("history database unavailable: %s", e)
            self._send(503, {"online": False, "error": f"database unavailable: {e}"})
            return
        except Exception:                            # noqa: BLE001
            log.exception("history request failed: %s", self.path)
            self._send(500, {"error": "history request failed"})
            return
        finally:
            if conn is not None:
                conn.close()
        body["query_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._send(200, body)


class HistoryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg, db_path=None):
        super().__init__(addr, _Handler)
        self.cfg = cfg
        self.db_path = db_path


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default=BIND_HOST)
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--db", default=None)
    p.add_argument("--config", default=None,
                   help="config.json path; a missing one means the shipped example")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = HistoryServer((args.host, args.port), config.load(args.config), args.db)
    log.info("history API listening on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
