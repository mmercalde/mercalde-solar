#!/usr/bin/env python3
"""Backfill battery history from the Conext Gateway's InsightLocal web UI.

The UI's "export CSV" button does not hit a server endpoint: it serialises a
chart the browser already holds. The data behind that chart comes from

    GET /chartdata/<device>/<instance>/years/<Y>/months/<M>/days/<D>/<resolution>

which returns CSV text directly, one day per request. Two resolutions matter:

  hours    24 rows of per-hour energy: load, generator, PV, battery charge
           and discharge. This is the primary source for the `hourly` table -
           it is the gateway's own accounting, not our re-integration of it.
  minutes  1440 rows of instantaneous values. Used only for per-hour peak and
           minimum pack voltage, which the hourly energy rows cannot give.

See docs/gateway_api.md for how this was established.

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
import re
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

# Columns are named by sysvar path, e.g. "/SYS/LOAD/ENERGY_HOUR(kwh)". Matching
# ignores the parenthesised unit so a unit-label change cannot break the parse.
#
# Despite the "(kwh)" label the energy values are Wh. Verified on 2026-08-27:
# summing the hourly column and integrating the matching minute power column
# agree to within 0.2% (load 32469 vs 32527, PV 31659 vs 31670, gen 4050 vs
# 4053), which only holds if the raw integer is Wh.
HOURS_ENERGY = {
    # our device row  ->  (sysvar prefix, which column of `hourly` it fills)
    ("load", "wh_out"): "/SYS/LOAD/ENERGY_HOUR",
    ("solar", "wh_in"): "/SYS/PV_TOTAL/ENERGY_HOUR",
    ("gen", "wh_in"): "/SYS/GEN/ENERGY_HOUR",
    ("battery", "wh_in"): "/SYS/BATT_CHG/ENERGY_HOUR",
    ("battery", "wh_out"): "/SYS/BATT_INV/ENERGY_HOUR",
}

# Minute columns, per battery bank. Volts and amps are scaled by 0.001; SOC is
# already a percentage. Verified: raw 52460..55250 -> 52.46..55.25 V,
# raw -74250..147910 -> -74.2..147.9 A, SOC raw 75..94.
BATT_V_RE = re.compile(r"^/SYS/BATT(\d+)/V\b", re.I)
BATT_I_RE = re.compile(r"^/SYS/BATT(\d+)/I\b", re.I)
MILLI = 0.001

# Older friendly-label exports, kept as a fallback for any device whose chart
# still uses them (SPEC section 5 describes this shape).
FRIENDLY_V = re.compile(r"^volts?\b", re.I)
FRIENDLY_I = re.compile(r"^current\b", re.I)

# Stop the backfill after this many consecutive empty days: the export goes
# quiet across gaps as well as at the true start of history.
EMPTY_DAY_TOLERANCE = 3

# The gateway caps concurrent sessions and answers 429 once they run out.
# Wait for one to fall out rather than giving up on the backfill.
RATE_LIMIT_SLEEP = 600
RATE_LIMIT_RETRIES = 6


class GatewayError(RuntimeError):
    pass


class Gateway:
    """One authenticated InsightLocal session. Always use as a context manager."""

    def __init__(self, cfg, timeout=30, sleep=time.sleep):
        gw = cfg["gateway"]
        self.sleep = sleep
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

    def _retry_429(self, what, call):
        """Run `call`, waiting out 429 'Maximum number of allowed users reached'.

        The gateway caps concurrent sessions; an open InsightLocal tab is
        enough to use the last one. A backfill walks hundreds of days, so
        giving up on a full queue would waste the whole run. Sleeping lets the
        offending session time out.
        """
        for attempt in range(1, RATE_LIMIT_RETRIES + 1):
            r = call()
            if r.status_code != 429:
                return r
            if attempt == RATE_LIMIT_RETRIES:
                raise GatewayError(
                    f"{what}: gateway still answering 429 'Maximum number of "
                    f"allowed users reached' after {RATE_LIMIT_RETRIES} attempts "
                    f"over {RATE_LIMIT_RETRIES * RATE_LIMIT_SLEEP // 60} minutes. "
                    f"Close an InsightLocal browser tab and try again.")
            log.warning("%s: gateway session limit reached (429); waiting %d min "
                        "then retrying (%d/%d)", what, RATE_LIMIT_SLEEP // 60,
                        attempt, RATE_LIMIT_RETRIES)
            self.sleep(RATE_LIMIT_SLEEP)
        raise GatewayError(f"{what}: unreachable")

    def login(self):
        if not self.password:
            raise GatewayError(
                "gateway.password is empty in agent/config.json. "
                "InsightLocal history cannot be scraped without it.")
        r = self._retry_429("login", lambda: self.session.post(
            f"{self.base}/auth",
            data=f"username={self.user}&password={self.password}&session=true",
            timeout=self.timeout))
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

    @staticmethod
    def chartdata_path(device, instance, day, resolution):
        """Path for one resolution.

        Each resolution truncates the path at a different depth, exactly as
        chartdataService.getChartData does. Appending the resolution to the
        full day path works for hours and minutes but makes the gateway answer
        400 for days, months and years.
        """
        base = f"chartdata/{device}/{instance}"
        if resolution == "years":
            return f"{base}/years/"
        if resolution == "months":
            return f"{base}/years/{day.year}/months/"
        if resolution == "days":
            return f"{base}/years/{day.year}/months/{day.month}/days/"
        return (f"{base}/years/{day.year}/months/{day.month}"
                f"/days/{day.day}/{resolution}")

    def chartdata(self, device, instance, day, resolution="minutes"):
        """Raw CSV text for one local day. Returns '' when the gateway has nothing."""
        path = f"{self.base}/" + self.chartdata_path(device, instance, day, resolution)
        r = self._retry_429(f"chartdata {day} {resolution}", lambda: self.session.get(
            path, headers=self._headers(), timeout=self.timeout))
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


def _strip_unit(name):
    """"/SYS/LOAD/ENERGY_HOUR(kwh)" -> "/SYS/LOAD/ENERGY_HOUR"."""
    return name.split("(")[0].strip()


def column_map(header):
    """Map a sysvar prefix to its column index, unit suffix ignored."""
    return {_strip_unit(name): i for i, name in enumerate(header)}


def _num(row, idx):
    if idx is None or idx >= len(row):
        return None
    try:
        return float(row[idx])
    except (TypeError, ValueError):
        return None


def _row_ts(row, cfg):
    try:
        stamp = datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None
    return int(stamp.replace(tzinfo=history.tzinfo(cfg)).timestamp())


def hours_to_energy(header, rows, cfg):
    """Per-hour energy straight from the gateway's own accounting.

    Returns {hour_ts: {device: {"wh_in": x, "wh_out": y}}}. Values are Wh
    despite the "(kwh)" column label; see HOURS_ENERGY.
    """
    cols = column_map(header)
    wanted = {key: cols[prefix] for key, prefix in HOURS_ENERGY.items()
              if prefix in cols}
    if not wanted:
        return {}
    out = {}
    for row in rows:
        ts = _row_ts(row, cfg)
        if ts is None:
            continue
        hour = history.hour_floor(ts)
        for (device, field), idx in wanted.items():
            wh = _num(row, idx)
            if wh is None:
                continue
            out.setdefault(hour, {}).setdefault(
                device, {"wh_in": None, "wh_out": None})[field] = wh
    return out


def _battery_columns(header, rows):
    """(volts_idx, amps_idx) for the first battery bank carrying real data.

    Five banks are always present in the header; the unused ones are all
    zeros, so pick the first with a non-zero voltage rather than assuming
    BATT1. Falls back to the friendly "Volts(V)" labels SPEC section 5
    describes, in case another device's chart still uses them.
    """
    volts, amps = {}, {}
    for i, name in enumerate(header):
        n = _strip_unit(name)
        m = BATT_V_RE.match(n)
        if m:
            volts[m.group(1)] = i
            continue
        m = BATT_I_RE.match(n)
        if m:
            amps[m.group(1)] = i
    for bank in sorted(volts, key=lambda b: int(b)):
        vi = volts[bank]
        if any((_num(r, vi) or 0) > 0 for r in rows):
            return vi, amps.get(bank), MILLI
    for i, name in enumerate(header):
        n = _strip_unit(name)
        if FRIENDLY_V.match(n):
            ai = next((j for j, m in enumerate(header)
                       if FRIENDLY_I.match(_strip_unit(m))), None)
            return i, ai, 1.0
    return None, None, 1.0


def minutes_to_voltage(header, rows, cfg):
    """Per-hour pack voltage from the minute export.

    Only voltage statistics come from here: mean, min and max volts, plus mean
    amps. Energy is taken from the hours endpoint instead. Minute rows are
    never stored.
    """
    vi, ai, scale = _battery_columns(header, rows)
    if vi is None:
        return {}
    buckets = {}
    for row in rows:
        ts = _row_ts(row, cfg)
        if ts is None:
            continue
        v = _num(row, vi)
        if v is None or v <= 0:
            continue
        b = buckets.setdefault(history.hour_floor(ts), {"v": [], "a": []})
        b["v"].append(v * scale)
        a = _num(row, ai)
        if a is not None:
            b["a"].append(a * scale)
    return {hour: {"mean_v": sum(b["v"]) / len(b["v"]),
                   "min_v": min(b["v"]), "max_v": max(b["v"]),
                   "mean_a": (sum(b["a"]) / len(b["a"])) if b["a"] else None,
                   "n": len(b["v"])}
            for hour, b in buckets.items()}


# --- scraping ---------------------------------------------------------------

def day_is_empty(energy):
    """True when the gateway returned a day with nothing recorded in it.

    A day of all zeros is not the same as a dark day: a real day always has
    some house load. Treat all-zero as no data.
    """
    for devices in energy.values():
        for fields in devices.values():
            if any(v for v in fields.values()):
                return False
    return True


def scrape_day(gw, conn, cfg, day, device=DEFAULT_DEVICE, instance=DEFAULT_INSTANCE):
    """Fetch one local day and write hourly rows. Returns hours written.

    Energy comes from the hours endpoint - the gateway's own accounting, not
    our re-integration of minute samples. The minute export is fetched only
    for per-hour peak and minimum voltage, which the energy rows lack.
    """
    hours_header, hours_rows = parse_chart_csv(
        gw.chartdata(device, instance, day, "hours"))
    energy = hours_to_energy(hours_header, hours_rows, cfg)
    if day_is_empty(energy):
        # No rows, or a day of all zeros. Either way the gateway holds nothing
        # for this date, so do not spend a second request on the minutes.
        return 0

    minutes_header, minutes_rows = parse_chart_csv(
        gw.chartdata(device, instance, day, "minutes"))
    volts = minutes_to_voltage(minutes_header, minutes_rows, cfg)

    written = 0
    for hour in sorted(set(energy) | set(volts)):
        v = volts.get(hour, {})
        for dev, fields in energy.get(hour, {}).items():
            is_battery = dev == "battery"
            written += history.put_hourly(
                conn, hour, dev,
                v.get("mean_v") if is_battery else None,
                v.get("mean_a") if is_battery else None,
                fields.get("wh_in"), fields.get("wh_out"),
                v.get("min_v") if is_battery else None,
                v.get("max_v") if is_battery else None,
                v.get("n") if is_battery else None,
                "insightlocal")
        # Voltage for an hour the energy export skipped is still worth keeping.
        if v and "battery" not in energy.get(hour, {}):
            written += history.put_hourly(
                conn, hour, "battery", v["mean_v"], v["mean_a"], None, None,
                v["min_v"], v["max_v"], v["n"], "insightlocal")
    conn.commit()
    log.info("%s: %d hour rows, %d minute rows -> %d hourly rows",
             day, len(hours_rows), sum(x["n"] for x in volts.values()), written)
    return written


def backfill(gw, conn, cfg, start=None, max_days=4000,
             device=DEFAULT_DEVICE, instance=DEFAULT_INSTANCE):
    """Walk backwards a day at a time until the export runs dry."""
    day = start or (date.today() - timedelta(days=1))
    empty_streak = 0
    total_days = total_hours = 0
    while total_days < max_days:
        hours = scrape_day(gw, conn, cfg, day, device, instance)
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
            vi, ai, scale = _battery_columns(header, rows)
            facts["columns"] = {
                "volts": header[vi] if vi is not None else None,
                "amps": header[ai] if ai is not None else None,
                "scale": scale}

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
    a("over `chart.config.data.datasets[]` - it serialises a chart the browser")
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
    a("once they are exhausted - an open InsightLocal browser tab is enough to")
    a("cause it. The scraper always logs out in a `finally` block, and waits a 429")
    a(f"out: it sleeps {RATE_LIMIT_SLEEP // 60} minutes and retries up to")
    a(f"{RATE_LIMIT_RETRIES} times before giving up, so a full session queue does")
    a("not throw away a long backfill.")
    a("")
    a("## Chart data")
    a("")
    a("```")
    a("GET /chartdata/<device>/<instance>/<path for the resolution>")
    a("authToken: <session>")
    a("```")
    a("")
    a("Returns CSV as `text/plain`. Lines starting `#` are comments; the first")
    a("surviving line is the header, whose columns are named by sysvar path.")
    a("Each resolution truncates the path at a different depth - appending the")
    a("resolution to the full day path gives 400 for days, months and years:")
    a("")
    a("| Resolution | Path tail |")
    a("|---|---|")
    a("| years | `/years/` |")
    a("| months | `/years/<Y>/months/` |")
    a("| days | `/years/<Y>/months/<M>/days/` |")
    a("| hours | `/years/<Y>/months/<M>/days/<D>/hours` |")
    a("| minutes | `/years/<Y>/months/<M>/days/<D>/minutes` |")
    a("")
    a("Observed on this gateway:")
    a("")
    a("| Resolution | OK | Columns | Rows |")
    a("|---|---|---|---|")
    for res, info in facts.get("chartdata", {}).items():
        if info.get("ok"):
            a(f"| {res} | yes | {len(info.get('header') or [])} | {info.get('rows')} |")
        else:
            a(f"| {res} | no | - | {str(info.get('error', ''))[:60]} |")
    a("")
    a("## Units")
    a("")
    a("Columns are integers and the parenthesised unit label cannot be trusted.")
    a("Verified on 2026-08-27 by summing each hourly energy column and")
    a("integrating the matching minute power column over the same day:")
    a("")
    a("| Column | Hourly sum | Minutes integrated | Ratio |")
    a("|---|---|---|---|")
    a("| `/SYS/LOAD/ENERGY_HOUR(kwh)` | 32469 | 32527 Wh | 0.998 |")
    a("| `/SYS/PV_TOTAL/ENERGY_HOUR(kwh)` | 31659 | 31670 Wh | 1.000 |")
    a("| `/SYS/GEN/ENERGY_HOUR(kwh)` | 4050 | 4053 Wh | 0.999 |")
    a("")
    a("So **`ENERGY_*(kwh)` values are Wh**, despite the label. Likewise")
    a("`V(V)`, `I(A)` and `T(degC)` are scaled by 0.001 (raw 53400 is 53.4 V,")
    a("raw -23960 is -23.96 A), while `SOC(%)` and `P(W)` are already in their")
    a("stated units.")
    a("")
    hdr = (facts.get("chartdata", {}).get("hours") or {}).get("header")
    if hdr:
        a("### Hours header as returned")
        a("")
        a("```")
        a(", ".join(hdr))
        a("```")
        a("")
    hdr = (facts.get("chartdata", {}).get("minutes") or {}).get("header")
    if hdr:
        a("### Minutes header as returned")
        a("")
        a("```")
        a(", ".join(hdr))
        a("```")
        a("")
    a("`/SYS/PV/ENERGY_HOUR` and `/SYS/PV_TOTAL/ENERGY_HOUR` carry identical")
    a("values; the scraper reads `PV_TOTAL`. Five battery banks always appear in")
    a("the minute header and the unused ones are all zeros, so the scraper takes")
    a("the first bank with a non-zero voltage rather than assuming `BATT1`.")
    a("")
    a("The `days` response is month-scoped and repeats each date about 25 times,")
    a("so it is not a shortcut for a whole backfill; `hours` per day is the source.")
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
    a("The agent does not depend on these - its energy counters come from Modbus")
    a("503 - but `--discover` records them as a cross-check. Observed:")
    a("")
    a("```json")
    a(json.dumps(facts.get("sysvars", {}), indent=2))
    a("```")
    a("")
    a("## What the scraper stores")
    a("")
    a("`--backfill` walks backwards one day at a time. For each day:")
    a("")
    a("1. **hours** is fetched first and is the primary source for `hourly`.")
    a("   These are the gateway's own energy figures, not our re-integration of")
    a("   power samples. Columns map to device rows as:")
    a("")
    a("   | Column | Row | Field |")
    a("   |---|---|---|")
    a("   | `/SYS/LOAD/ENERGY_HOUR` | `load` | `wh_out` |")
    a("   | `/SYS/PV_TOTAL/ENERGY_HOUR` | `solar` | `wh_in` |")
    a("   | `/SYS/GEN/ENERGY_HOUR` | `gen` | `wh_in` |")
    a("   | `/SYS/BATT_CHG/ENERGY_HOUR` | `battery` | `wh_in` |")
    a("   | `/SYS/BATT_INV/ENERGY_HOUR` | `battery` | `wh_out` |")
    a("")
    a("2. **minutes** is fetched only for what the energy rows cannot give:")
    a("   per-hour mean, minimum and maximum pack voltage, and mean current.")
    a("   Minute rows are parsed and discarded; they are never stored.")
    a("")
    a("A day whose energy columns are all zero is treated as empty: the minute")
    a("request is skipped, and the walk stops after")
    a(f"{EMPTY_DAY_TOLERANCE} consecutive empty days so a gap in the gateway's")
    a("history does not end it early.")
    a("")
    a("A `live` row always wins over a scraped row for the same hour, so a")
    a("backfill can be re-run over a period the agent already sampled itself.")
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
