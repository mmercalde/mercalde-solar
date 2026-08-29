#!/usr/bin/env python3
"""Backfill battery history from the Conext Gateway's InsightLocal web UI.

The UI's "export CSV" button does not hit a server endpoint: it serialises a
chart the browser already holds. The data behind that chart comes from

    GET /chartdata/<device>/<instance>/years/<Y>/months/<M>/days/<D>/minutes

which returns CSV text directly, one day per request at one-minute
resolution. See docs/gateway_api.md for how this was established.

The gateway limits concurrent sessions and answers 429 "Maximum number of
allowed users reached" once they are used up, so every session this module
opens is released in a finally block.

Usage:
    scrape_gateway.py --discover            probe the API and rewrite docs/gateway_api.md
    scrape_gateway.py --backfill            walk backwards until the export runs dry
    scrape_gateway.py --nightly             yesterday only (cron)
    scrape_gateway.py --day 2026-08-27      one specific local day
"""

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests
import urllib3

import config
import history

log = logging.getLogger(__name__)

# The gateway serves a self-signed certificate on the lab segment.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_FACTS = os.path.join(config.DATA_DIR, "gateway_api.json")
DOC_PATH = os.path.join(config.AGENT_DIR, "docs", "gateway_api.md")

# Battery Summary charts for the whole system; confirmed in combox.js as
# batterySummaryService -> getChartData("system", 0, ...).
DEFAULT_DEVICE, DEFAULT_INSTANCE = "system", "0"

# Candidates tried by --discover when looking for per-device charts.
DEVICE_CANDIDATES = ["system", "battery", "xw", "inverter", "mppt",
                     "chargecontroller", "batterymonitor", "gateway"]
INSTANCE_CANDIDATES = ["0", "1", "2", "3"]

# CSV header text -> the quantity we store. Matched case-insensitively on a
# substring so "Volts(V)" and "Volts (V)" both land.
COLUMN_HINTS = [
    ("volt", "v"),
    ("current", "a"),
    ("state of charge", "soc"),
    ("temperature", "temp"),
]

# Stop the backfill after this many consecutive empty days: the export goes
# quiet across gaps as well as at the true start of history.
EMPTY_DAY_TOLERANCE = 3


class GatewayError(RuntimeError):
    pass


class Gateway:
    """One authenticated InsightLocal session. Always use as a context manager."""

    def __init__(self, cfg, timeout=30):
        gw = cfg["gateway"]
        self.base = gw["url"].rstrip("/")
        self.user = gw["user"]
        self.password = gw["password"]
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False
        self.token = None
        self.otk = None

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()
        return False

    def login(self):
        if not self.password:
            raise GatewayError(
                "gateway.password is empty in agent/config.json. "
                "InsightLocal history cannot be scraped without it.")
        r = self.session.post(
            f"{self.base}/auth",
            data=f"username={self.user}&password={self.password}&session=true",
            timeout=self.timeout)
        if r.status_code == 429:
            raise GatewayError(
                "gateway refused the login with 429 'Maximum number of allowed "
                "users reached'. Close an InsightLocal browser tab (or wait for "
                "its session to expire) and try again.")
        if r.status_code != 200:
            raise GatewayError(f"POST /auth returned {r.status_code}: {r.text[:200]}")
        try:
            body = r.json()
        except ValueError:
            raise GatewayError(f"POST /auth returned non-JSON: {r.text[:200]}")
        self.token = body.get("session")
        if not self.token:
            raise GatewayError(f"POST /auth gave no session id: {body}")
        log.info("authenticated to %s as %s", self.base, self.user)
        return self.token

    def logout(self):
        if not self.token:
            return
        try:
            self.session.post(f"{self.base}/logout", data=" ",
                              headers=self._headers(), timeout=self.timeout)
            log.info("released gateway session")
        except requests.RequestException as e:
            log.warning("logout failed, session may linger until it expires: %s", e)
        finally:
            self.token = None

    def _headers(self):
        h = {"authToken": self.token or ""}
        if self.otk:
            h["otk"] = self.otk
        return h

    def sysvars(self, names):
        """POST /vars -> {name: value}. Used by --discover to list devices."""
        r = self.session.post(f"{self.base}/vars", data="name=" + ",".join(names),
                              headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("OTK"):
            self.otk = body["OTK"]
        return {v["name"]: v.get("value") for v in body.get("values", [])}

    def chartdata(self, device, instance, day, resolution="minutes"):
        """Raw CSV text for one local day. Returns '' when the gateway has nothing."""
        path = (f"{self.base}/chartdata/{device}/{instance}"
                f"/years/{day.year}/months/{day.month}/days/{day.day}/{resolution}")
        r = self.session.get(path, headers=self._headers(), timeout=self.timeout)
        if r.status_code == 404:
            return ""
        if r.status_code == 401:
            raise GatewayError("gateway session rejected (401) mid-scrape")
        r.raise_for_status()
        return r.text


# --- CSV parsing ------------------------------------------------------------

def parse_chart_csv(text):
    """(header list, [row lists]) from a chartdata response.

    Lines beginning '#' are comments and blank lines are padding; the first
    surviving line is the header. This mirrors what combox.js does before
    handing the text to its CSV parser.
    """
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], []
    rows = list(csv.reader(io.StringIO("\n".join(lines))))
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]


def column_map(header):
    """Map quantity -> column index, using the first column whose name matches."""
    out = {}
    for i, name in enumerate(header):
        low = name.lower()
        for hint, key in COLUMN_HINTS:
            if hint in low and key not in out:
                out[key] = i
    return out


def _num(row, idx):
    if idx is None or idx >= len(row):
        return None
    try:
        return float(row[idx])
    except (TypeError, ValueError):
        return None


def rows_to_hourly(header, rows, cfg):
    """Downsample minute rows to per-hour aggregates. Minute rows are never stored.

    Returns {hour_ts: {mean_v, mean_a, min_v, max_v, wh_in, wh_out, n}}.
    """
    cols = column_map(header)
    if "v" not in cols:
        return {}
    tz = history.tzinfo(cfg)
    buckets = {}
    for row in rows:
        if not row:
            continue
        try:
            stamp = datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            continue
        ts = int(stamp.replace(tzinfo=tz).timestamp())
        v = _num(row, cols.get("v"))
        if v is None:
            continue
        a = _num(row, cols.get("a"))
        b = buckets.setdefault(history.hour_floor(ts),
                               {"v": [], "a": [], "wh_in": 0.0, "wh_out": 0.0})
        b["v"].append(v)
        if a is not None:
            b["a"].append(a)
            # One row per minute, so each sample covers 1/60 h.
            wh = v * a / 60.0
            if wh >= 0:
                b["wh_in"] += wh
            else:
                b["wh_out"] += -wh
    out = {}
    for hour, b in buckets.items():
        out[hour] = {
            "mean_v": sum(b["v"]) / len(b["v"]),
            "mean_a": (sum(b["a"]) / len(b["a"])) if b["a"] else None,
            "min_v": min(b["v"]), "max_v": max(b["v"]),
            "wh_in": b["wh_in"], "wh_out": b["wh_out"], "n": len(b["v"]),
        }
    return out


# --- scraping ---------------------------------------------------------------

def scrape_day(gw, conn, cfg, day, device=DEFAULT_DEVICE, instance=DEFAULT_INSTANCE):
    """Fetch one local day and write hourly rows. Returns hours written."""
    text = gw.chartdata(device, instance, day)
    header, rows = parse_chart_csv(text)
    if not rows:
        return 0
    hourly = rows_to_hourly(header, rows, cfg)
    written = 0
    for hour, agg in hourly.items():
        written += history.put_hourly(
            conn, hour, "battery", agg["mean_v"], agg["mean_a"],
            agg["wh_in"], agg["wh_out"], agg["min_v"], agg["max_v"],
            agg["n"], "insightlocal")
    conn.commit()
    log.info("%s: %d minute rows -> %d hourly rows", day, len(rows), written)
    return written


def backfill(gw, conn, cfg, start=None, max_days=4000):
    """Walk backwards a day at a time until the export runs dry."""
    day = start or (date.today() - timedelta(days=1))
    empty_streak = 0
    total_days = total_hours = 0
    while total_days < max_days:
        hours = scrape_day(gw, conn, cfg, day)
        if hours:
            empty_streak = 0
            total_days += 1
            total_hours += hours
        else:
            empty_streak += 1
            log.info("%s: empty (%d in a row)", day, empty_streak)
            if empty_streak >= EMPTY_DAY_TOLERANCE:
                break
        day -= timedelta(days=1)
        time.sleep(0.2)  # the gateway is a small embedded box; do not hammer it
    history.rollup_daily(conn, cfg)
    log.info("backfill complete: %d days, %d hourly rows, back to %s",
             total_days, total_hours, day)
    return total_days, total_hours


# --- discovery --------------------------------------------------------------

def discover(gw, cfg):
    """Probe the live gateway and record exactly what answers.

    Run once with real credentials; it rewrites docs/gateway_api.md from
    observed responses rather than from assumptions.
    """
    yesterday = date.today() - timedelta(days=1)
    facts = {
        "discovered_at": datetime.now().isoformat(timespec="seconds"),
        "base": gw.base,
        "auth": {"endpoint": "POST /auth",
                 "body": "username=<user>&password=<pw>&session=true",
                 "returns": "{\"session\": \"<authToken>\"}"},
        "chartdata": {},
        "devices": [],
        "columns": {},
        "sysvars": {},
    }

    log.info("probing chartdata resolutions for %s/%s", DEFAULT_DEVICE, DEFAULT_INSTANCE)
    for res in ("minutes", "hours", "days", "months", "years"):
        try:
            text = gw.chartdata(DEFAULT_DEVICE, DEFAULT_INSTANCE, yesterday, res)
        except (requests.RequestException, GatewayError) as e:
            facts["chartdata"][res] = {"ok": False, "error": str(e)[:200]}
            continue
        header, rows = parse_chart_csv(text)
        facts["chartdata"][res] = {"ok": bool(header), "header": header,
                                   "rows": len(rows), "sample": rows[0] if rows else None}
        if res == "minutes" and header:
            facts["columns"] = {k: header[i] for k, i in column_map(header).items()}

    log.info("probing device/instance combinations")
    for dev in DEVICE_CANDIDATES:
        for inst in INSTANCE_CANDIDATES:
            try:
                text = gw.chartdata(dev, inst, yesterday)
            except (requests.RequestException, GatewayError):
                continue
            header, rows = parse_chart_csv(text)
            if header:
                facts["devices"].append({"device": dev, "instance": inst,
                                         "header": header, "rows": len(rows)})
                log.info("  %s/%s -> %d columns, %d rows", dev, inst, len(header), len(rows))

    try:
        facts["sysvars"] = gw.sysvars(
            ["/SYS/PV_TOTAL/ENERGY_DAY", "/SYS/LOAD/ENERGY_DAY",
             "/SYS/BATT_CHG/ENERGY_DAY", "/SYS/BATT_INV/ENERGY_DAY",
             "/SYS/GEN/ENERGY_DAY"])
    except (requests.RequestException, ValueError, KeyError) as e:
        facts["sysvars"] = {"error": str(e)[:200]}

    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(API_FACTS, "w") as f:
        json.dump(facts, f, indent=2)
    write_doc(facts)
    log.info("wrote %s and %s", API_FACTS, DOC_PATH)
    return facts


def write_doc(facts):
    """Rewrite docs/gateway_api.md from what --discover actually observed."""
    L = []
    a = L.append
    a("# Conext Gateway (InsightLocal) HTTP API")
    a("")
    a(f"Discovered against `{facts['base']}` on {facts['discovered_at']} by")
    a("`scrape_gateway.py --discover`. Everything below is an observed response,")
    a("not a guess. Re-run that command to refresh this file.")
    a("")
    a("## How this was found")
    a("")
    a("The InsightLocal UI is an AngularJS app served as one bundle, `/combox.js`.")
    a("Its \"export CSV\" button calls `csvService.saveCsv('chart_data.csv', ...)`")
    a("over `chart.config.data.datasets[]` — it serialises a chart the browser")
    a("already holds, so there is no server-side CSV endpoint to call. The chart")
    a("itself is filled by `chartdataService.getChartData(device, instance, date)`,")
    a("and `batterySummaryService` calls it as `getChartData(\"system\", 0, ...)`.")
    a("")
    a("## Authentication")
    a("")
    a("```")
    a("POST /auth")
    a("Content-Type: application/x-www-form-urlencoded")
    a("")
    a("username=<user>&password=<password>&session=true")
    a("```")
    a("")
    a("Returns `{\"session\": \"<authToken>\"}`. Send that back as an `authToken`")
    a("**header** (not a cookie) on every later request. `POST /logout` releases it.")
    a("")
    a("The gateway caps concurrent sessions and answers")
    a("`429 {\"status\": 429, \"description\": \"Maximum number of allowed users reached\"}`")
    a("once they are exhausted — an open InsightLocal browser tab is enough to")
    a("cause it. `scrape_gateway.py` always logs out in a `finally` block.")
    a("")
    a("## Chart data (the history source)")
    a("")
    a("```")
    a("GET /chartdata/<device>/<instance>/years/<Y>/months/<M>/days/<D>/minutes")
    a("authToken: <session>")
    a("```")
    a("")
    a("Returns CSV as `text/plain`, one local day per request. Lines starting `#`")
    a("are comments; the first surviving line is the header. Other resolutions")
    a("replace the last segment:")
    a("")
    a("| Resolution | Path tail |")
    a("|---|---|")
    a("| minutes | `/years/<Y>/months/<M>/days/<D>/minutes` |")
    a("| hours | `/years/<Y>/months/<M>/days/<D>/hours` |")
    a("| days | `/years/<Y>/months/<M>/days/` |")
    a("| months | `/years/<Y>/months/` |")
    a("| years | `/years/` |")
    a("")
    a("Observed on this gateway:")
    a("")
    a("| Resolution | OK | Columns | Rows |")
    a("|---|---|---|---|")
    for res, info in facts.get("chartdata", {}).items():
        if info.get("ok"):
            a(f"| {res} | yes | {len(info.get('header') or [])} | {info.get('rows')} |")
        else:
            a(f"| {res} | no | — | {info.get('error', '')[:60]} |")
    a("")
    if facts.get("columns"):
        a("### Minute-resolution columns used by the scraper")
        a("")
        a("| Quantity | Column |")
        a("|---|---|")
        for k, v in facts["columns"].items():
            a(f"| {k} | `{v}` |")
        a("")
    hdr = (facts.get("chartdata", {}).get("minutes") or {}).get("header")
    if hdr:
        a("Full minute header as returned:")
        a("")
        a("```")
        a(", ".join(hdr))
        a("```")
        a("")
    a("## Devices that return charts")
    a("")
    if facts.get("devices"):
        a("| device | instance | columns | rows (yesterday) |")
        a("|---|---|---|---|")
        for d in facts["devices"]:
            a(f"| `{d['device']}` | `{d['instance']}` | {len(d['header'])} | {d['rows']} |")
    else:
        a("Only `system/0` answered during discovery.")
    a("")
    a("## Sysvars")
    a("")
    a("```")
    a("POST /vars")
    a("authToken: <session>")
    a("otk: <one-time key from the previous response's OTK field>")
    a("")
    a("name=/SYS/PV_TOTAL/ENERGY_DAY,/SYS/LOAD/ENERGY_DAY")
    a("```")
    a("")
    a("Returns `{\"values\": [{\"name\", \"value\", \"quality\"}], \"OTK\": \"<next>\"}`.")
    a("The agent does not depend on these — its energy counters come from Modbus")
    a("503 — but `--discover` records them as a cross-check. Observed:")
    a("")
    a("```json")
    a(json.dumps(facts.get("sysvars", {}), indent=2))
    a("```")
    a("")
    a("## What the scraper stores")
    a("")
    a("Minute rows are parsed and discarded. Only per-hour aggregates reach")
    a("`hourly` (device `battery`, source `insightlocal`): mean/min/max volts,")
    a("mean amps, and Wh in/out integrated as `V * A / 60` per minute row. A live")
    a("`live` row for the same hour always wins over a scraped one.")
    a("")
    os.makedirs(os.path.dirname(DOC_PATH), exist_ok=True)
    with open(DOC_PATH, "w") as f:
        f.write("\n".join(L) + "\n")


# --- entry point ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--discover", action="store_true",
                   help="probe the API once and rewrite docs/gateway_api.md")
    g.add_argument("--backfill", action="store_true",
                   help="walk backwards until the export runs dry")
    g.add_argument("--nightly", action="store_true", help="yesterday only")
    g.add_argument("--day", help="one local day, YYYY-MM-DD")
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--instance", default=DEFAULT_INSTANCE)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()

    try:
        with Gateway(cfg) as gw:
            if args.discover:
                discover(gw, cfg)
                return 0
            conn = history.connect()
            if args.backfill:
                backfill(gw, conn, cfg)
            elif args.nightly:
                day = date.today() - timedelta(days=1)
                scrape_day(gw, conn, cfg, day, args.device, args.instance)
                history.rollup_daily(conn, cfg, days=[day.strftime("%Y-%m-%d")])
            else:
                day = datetime.strptime(args.day, "%Y-%m-%d").date()
                scrape_day(gw, conn, cfg, day, args.device, args.instance)
                history.rollup_daily(conn, cfg, days=[args.day])
    except GatewayError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
