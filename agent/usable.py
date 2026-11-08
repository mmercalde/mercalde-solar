"""Usable battery percentage: the agent's own amp-hour counter.

The Battery Monitor's SOC is a percentage of 1800 Ah that resets to 100 % at
57.4 V. The pack really runs from about 59.8 V, where the charge controllers
taper it, down to 52.0 V, and that span is about 805 Ah. So the monitor reads
69 % at the 52.0 V floor and clips everything above 57.4 V. See
docs/battery_soc_2026-11.md, sections 7 to 9, for the measurement.

This module counts the monitor's current, never its SOC or Ah remaining, on
its own scale:

    100 %  the pack tapered at the top: >= 59.5 V with |I| < 36 A for 4 min
      0 %  the pack reached 52.0 V, the floor

A generator start is not the 0 % anchor. The agent can raise a start
threshold as high as 56 V, and a start at 54 V says nothing about the floor.

Between anchors it integrates the shunt current from `samples`, with charge
multiplied by an efficiency factor. Every anchor is logged in
battery_anchors with the counter's error at that moment, and the window and
the factor are fitted from those rows within fixed bounds.

Display only. policy.py, the guard and the Pi5 control loop do not read it,
and no threshold is decided on it.
"""

import logging
import statistics
import time

import history

log = logging.getLogger(__name__)

FLOOR_V = 52.0
# A floor anchor fires again only after the pack has spent this long above
# FLOOR_REARM_V since the last one. Otherwise one night's slow wander around
# 52.0 V would reset the counter every minute.
FLOOR_REARM_V = 52.3
FLOOR_REARM_S = 2 * 3600
# A load surge drags the terminal voltage down: 180 A sags it about 0.8 V
# (4.4 mOhm). The window was measured to 52.0 V at the night's 25-60 A, so a
# dip under a heavier draw is not the floor.
FLOOR_MAX_DISCHARGE_A = 100

TOP_V = 59.5
TOP_TAPER_A = 36            # the monitor's own tail, 2.0 % of 1800 Ah
TOP_HOLD_S = 240
TOP_REARM_S = 6 * 3600      # one anchor per day's top, not one per minute of it

MAX_GAP_S = 300             # a longer gap between samples is not integrated

# Measured over 18 months of gateway minute data (doc section 8): five full
# top-to-floor cycles, and a replay over 377 anchors at which these two
# values gave a median error of a few Ah.
DEFAULT_WINDOW_AH = 805
DEFAULT_CEF = 0.95
WINDOW_BOUNDS = (700.0, 900.0)
CEF_BOUNDS = (0.90, 1.00)

# Calibration inputs. A pair is only as good as its span: a long one carries
# a lot of top-up charge, and the window it implies leans on the factor.
CEF_MIN_PAIRS = 3
CEF_MIN_IN_AH = 100
CEF_MAX_SPAN_S = 14 * 86400
CEF_LOOKBACK_S = 60 * 86400
WINDOW_MIN_PAIRS = 2
WINDOW_MAX_IN_AH = 1500
WINDOW_LAST_N = 5

UNCERTAIN_AFTER_S = 7 * 86400
UNCERTAIN_GAP_S = 3600


def _clamp(x, bounds):
    return min(max(x, bounds[0]), bounds[1])


# --- detection ---------------------------------------------------------------

def new_state(last=None, window_ah=DEFAULT_WINDOW_AH):
    """Scanner state, starting just after anchor row `last` (or from nothing)."""
    st = {"ah": None, "last_kind": None, "last_ts": None, "last_top_ts": None,
          "armed_s": 0, "top_run_s": 0, "ah_in": 0.0, "ah_out": 0.0, "gap_s": 0,
          "prev": None}
    if last is not None:
        st["last_kind"] = last["kind"]
        st["last_ts"] = last["ts"]
        st["ah"] = float(window_ah) if last["kind"] == "top" else 0.0
        if last["kind"] == "top":
            st["last_top_ts"] = last["ts"]
    return st


def scan(rows, st, window_ah, cef):
    """Walk (ts, volts, amps) rows forward. Returns the anchors found.

    `st` is updated in place, so the caller can carry on from where this
    stopped. Each anchor is a dict ready for battery_anchors.
    """
    found = []
    for ts, v, a in rows:
        if v is None or a is None:
            continue
        prev = st["prev"]
        st["prev"] = (ts, v, a)
        if prev is None:
            continue
        pts, pv, pa = prev
        dt = ts - pts
        if dt <= 0:
            continue
        if dt > MAX_GAP_S:
            st["gap_s"] += dt
            st["top_run_s"] = 0
        else:
            amp_h = (a + pa) / 2.0 * dt / 3600.0
            if amp_h > 0:
                st["ah_in"] += amp_h
                if st["ah"] is not None:
                    st["ah"] += amp_h * cef
            else:
                st["ah_out"] -= amp_h
                if st["ah"] is not None:
                    st["ah"] += amp_h
            if v > FLOOR_REARM_V and pv > FLOOR_REARM_V:
                st["armed_s"] += dt
            if v >= TOP_V and abs(a) < TOP_TAPER_A and pv >= TOP_V and abs(pa) < TOP_TAPER_A:
                st["top_run_s"] += dt
            else:
                st["top_run_s"] = 0

        kind = None
        if (st["top_run_s"] >= TOP_HOLD_S
                and (st["last_top_ts"] is None or ts - st["last_top_ts"] >= TOP_REARM_S)):
            kind, value = "top", float(window_ah)
        elif (v <= FLOOR_V and a >= -FLOOR_MAX_DISCHARGE_A
              and st["armed_s"] >= FLOOR_REARM_S):
            kind, value = "floor", 0.0
        if kind is None:
            continue
        found.append({
            "ts": ts, "kind": kind, "battery_v": v, "battery_a": a,
            "counter_ah": None if st["ah"] is None else round(st["ah"], 1),
            "error_ah": None if st["ah"] is None else round(st["ah"] - value, 1),
            "ah_in": round(st["ah_in"], 1), "ah_out": round(st["ah_out"], 1),
            "gap_s": st["gap_s"],
            "since_s": None if st["last_ts"] is None else ts - st["last_ts"],
            "window_ah": float(window_ah), "cef": cef})
        st.update(ah=value, last_kind=kind, last_ts=ts, ah_in=0.0, ah_out=0.0,
                  gap_s=0)
        if kind == "top":
            st["last_top_ts"] = ts
        else:
            st["armed_s"] = 0
    return found


# --- calibration ---------------------------------------------------------------

def calibrate(anchors, now=None):
    """{"window_ah", "cef", ...} fitted from anchor rows, oldest first.

    Efficiency, from floor-to-floor pairs: the counter should read 0 at the
    second floor, so charge x factor = discharge, and factor = out / in.
    Window, from a top followed directly by a floor: what came out, less
    the charge that went in, times the factor. Both are medians, clamped to
    WINDOW_BOUNDS and CEF_BOUNDS; with too few pairs the measured defaults
    stand.
    """
    now = int(now or time.time())
    rows = [dict(r) for r in anchors]
    cefs = []
    i_prev = None
    for i, r in enumerate(rows):
        if r["kind"] != "floor":
            continue
        if i_prev is not None:
            seg = rows[i_prev + 1:i + 1]
            ah_in = sum(x["ah_in"] or 0 for x in seg)
            ah_out = sum(x["ah_out"] or 0 for x in seg)
            gaps = sum(x["gap_s"] or 0 for x in seg)
            span = r["ts"] - rows[i_prev]["ts"]
            if (gaps == 0 and ah_in >= CEF_MIN_IN_AH and span <= CEF_MAX_SPAN_S
                    and r["ts"] >= now - CEF_LOOKBACK_S):
                cefs.append(ah_out / ah_in)
        i_prev = i
    out = {"cef_pairs": len(cefs)}
    if len(cefs) >= CEF_MIN_PAIRS:
        raw = statistics.median(cefs)
        out.update(cef=round(_clamp(raw, CEF_BOUNDS), 3), cef_fitted=round(raw, 3),
                   cef_source="fitted")
    else:
        out.update(cef=DEFAULT_CEF, cef_fitted=None, cef_source="measured default")

    windows = []
    for prev, r in zip(rows, rows[1:]):
        if (prev["kind"] == "top" and r["kind"] == "floor" and not r["gap_s"]
                and (r["ah_in"] or 0) <= WINDOW_MAX_IN_AH):
            windows.append(r["ah_out"] - out["cef"] * r["ah_in"])
    windows = windows[-WINDOW_LAST_N:]
    out["window_pairs"] = len(windows)
    if len(windows) >= WINDOW_MIN_PAIRS:
        raw = statistics.median(windows)
        out.update(window_ah=round(_clamp(raw, WINDOW_BOUNDS)),
                   window_fitted=round(raw), window_source="fitted")
    else:
        out.update(window_ah=DEFAULT_WINDOW_AH, window_fitted=None,
                   window_source="measured default")
    return out


# --- the table ---------------------------------------------------------------

def anchors(conn, since=None):
    q = "SELECT * FROM battery_anchors"
    args = ()
    if since is not None:
        q += " WHERE ts >= ?"
        args = (since,)
    return [dict(r) for r in conn.execute(q + " ORDER BY ts", args)]


def _last_anchor(conn):
    r = conn.execute("SELECT * FROM battery_anchors ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _samples_after(conn, ts):
    q = "SELECT ts, battery_v, batt_current FROM samples"
    if ts is None:
        return conn.execute(q + " ORDER BY ts").fetchall()
    # From the anchor's own row, so the first step integrates from it.
    return conn.execute(q + " WHERE ts >= ? ORDER BY ts", (ts,)).fetchall()


def update(conn, now=None):
    """Find anchors in samples since the last one, log them. Returns them.

    Each new anchor is logged with the calibration in force when it fired,
    and calibration is refitted before the next, so a run that crosses
    several anchors (the first, which replays all of history) behaves as the
    live counter would have.
    """
    last = _last_anchor(conn)
    cal = calibrate(anchors(conn), now=now)
    st = new_state(last, cal["window_ah"])
    rows = [(r[0], r[1], r[2]) for r in _samples_after(conn, last and last["ts"])]
    logged = []
    i = 0
    while i < len(rows):
        found = scan(rows[i:i + 1], st, cal["window_ah"], cal["cef"])
        i += 1
        if last is not None and found and found[0]["ts"] <= last["ts"]:
            continue
        for a in found:
            conn.execute(
                "INSERT OR IGNORE INTO battery_anchors (ts, kind, battery_v, battery_a, "
                "counter_ah, error_ah, ah_in, ah_out, gap_s, since_s, window_ah, cef) "
                "VALUES (:ts, :kind, :battery_v, :battery_a, :counter_ah, :error_ah, "
                ":ah_in, :ah_out, :gap_s, :since_s, :window_ah, :cef)", a)
            conn.commit()
            log.info("battery anchor %s at %s: %.2f V %.0f A, counter error %s Ah",
                     a["kind"], a["ts"], a["battery_v"], a["battery_a"],
                     a["error_ah"])
            logged.append(a)
            cal = calibrate(anchors(conn), now=now)
            if a["kind"] == "top":
                st["ah"] = float(cal["window_ah"])
    return logged


# --- the value ---------------------------------------------------------------

def _anchor_view(r, cfg):
    if not r:
        return None
    return {"kind": r["kind"], "ts": r["ts"], "at": history.stamp(r["ts"], cfg),
            "voltage": r["battery_v"], "current_a": r["battery_a"],
            "error_ah": r["error_ah"]}


def current(conn, cfg, now=None, monitor_soc=None):
    """The usable percentage now, with what it rests on.

    Always returns a dict. usable_pct is None before the first anchor.
    """
    now = int(now or time.time())
    rows = anchors(conn)
    cal = calibrate(rows, now=now)
    last = rows[-1] if rows else None
    latest = conn.execute(
        "SELECT ts, battery_v, batt_current, batt_soc FROM samples "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    if monitor_soc is None and latest is not None:
        monitor_soc = latest["batt_soc"]
    out = {
        "usable_pct": None, "ah_above_floor": None,
        "window_ah": cal["window_ah"], "window_source": cal["window_source"],
        "cef": cal["cef"], "cef_source": cal["cef_source"],
        "monitor_soc_pct": None if monitor_soc is None else round(monitor_soc),
        "floor_v": FLOOR_V, "top_v": TOP_V,
        "as_of": history.stamp(latest["ts"], cfg) if latest else None,
        "last_anchor": _anchor_view(last, cfg),
        "last_top": _anchor_view(next((r for r in reversed(rows) if r["kind"] == "top"), None), cfg),
        "last_floor": _anchor_view(next((r for r in reversed(rows) if r["kind"] == "floor"), None), cfg),
        "recent_anchors": [_anchor_view(r, cfg) for r in rows[-5:]],
        "uncertain": True, "uncertain_reason": "no anchor yet",
        "days_since_anchor": None,
        "note": ("Display only. 100 % is the pack tapered at >= 59.5 V, 0 % is "
                 "52.0 V. Counted from the Battery Monitor's current, not its "
                 "SOC; nothing is decided on it."),
    }
    if last is None:
        return out
    st = new_state(last, cal["window_ah"])
    scan([(r[0], r[1], r[2]) for r in _samples_after(conn, last["ts"])
          if r[0] <= now], st, cal["window_ah"], cal["cef"])
    # Anchors logged by update() run the counter; a newer one not yet logged
    # (update runs once a minute) is picked up next time.
    ah = st["ah"]
    age = now - last["ts"]
    reasons = []
    if age >= UNCERTAIN_AFTER_S:
        reasons.append(f"no anchor for {age // 86400} days")
    if st["gap_s"] > UNCERTAIN_GAP_S:
        reasons.append(f"{st['gap_s'] // 60} min of samples missing since the last anchor")
    out.update(
        usable_pct=round(100.0 * ah / cal["window_ah"]),
        ah_above_floor=round(ah),
        uncertain=bool(reasons), uncertain_reason="; ".join(reasons) or None,
        days_since_anchor=round(age / 86400.0, 1))
    return out


# --- words -------------------------------------------------------------------

def phrase(u, lang="en", spoken=False):
    """"Battery 41% usable (monitor 82%)", or its Spanish, or its spoken form.

    `u` is current()'s dict. Before the first anchor only the monitor's
    figure is known, and it is labelled as the monitor's.
    """
    es = lang == "es"
    usable, monitor = (u or {}).get("usable_pct"), (u or {}).get("monitor_soc_pct")
    uncertain = (u or {}).get("uncertain") and usable is not None
    if spoken:
        pct = "por ciento" if es else "percent"
        mon = f"monitor {monitor} {pct}" if monitor is not None else None
        if usable is None:
            return (f"Batería: {mon}" if es else f"Battery {mon}") if mon else (
                "Batería sin dato" if es else "Battery unknown")
        head = (f"Batería al {usable} por ciento útil" if es
                else f"Battery {usable} percent usable")
        if uncertain:
            head += ", incierto" if es else ", uncertain"
        return f"{head} ({mon})" if mon else head
    sep = " %" if es else "%"
    mon = f"monitor {monitor}{sep}" if monitor is not None else None
    if usable is None:
        return (f"Batería: {mon}" if es else f"Battery {mon}") if mon else (
            "Batería sin dato" if es else "Battery unknown")
    head = f"Batería {usable}{sep} útil" if es else f"Battery {usable}% usable"
    if uncertain:
        head += ", incierto" if es else ", uncertain"
    return f"{head} ({mon})" if mon else head


# --- a day, for the dashboard's chart ------------------------------------------

SERIES_STEPS_MIN = (1, 5)


def day_series(conn, cfg, day, step_min=5, now=None):
    """The usable figure through one local day, and the anchors in it.

    One point per `step_min` minutes (1 or 5), each the last sample in its
    step, with the time as the owner reads it (12-hour, cfg["tz"]). The
    counter is replayed from the last anchor before the day, with the
    calibration in force now, so a point can differ by a few Ah from what was
    shown live before a later anchor refitted the window or the factor.
    Points before the first anchor ever have usable_pct None.
    """
    if step_min not in SERIES_STEPS_MIN:
        raise ValueError(f"step must be one of {SERIES_STEPS_MIN} minutes")
    start, end = history.day_bounds(day, cfg)
    if now is not None:
        end = min(end, int(now))
    cal = calibrate(anchors(conn), now=now)
    before = conn.execute("SELECT * FROM battery_anchors WHERE ts < ? "
                          "ORDER BY ts DESC LIMIT 1", (start,)).fetchone()
    before = dict(before) if before else None
    st = new_state(before, cal["window_ah"])
    q = ("SELECT ts, battery_v, batt_current, batt_soc FROM samples "
         "WHERE ts >= ? AND ts < ? ORDER BY ts")
    lead_from = before["ts"] if before else start
    points, bucket = [], None
    step = step_min * 60
    for r in conn.execute(q, (lead_from, end)):
        found = scan([(r[0], r[1], r[2])], st, cal["window_ah"], cal["cef"])
        if found and found[0]["kind"] == "top":
            st["ah"] = float(cal["window_ah"])
        if r[0] < start:
            continue
        b = (r[0] - start) // step
        ah = st["ah"]
        pt = {"ts": r[0], "time": history.clock(r[0], cfg),
              "usable_pct": None if ah is None else round(100.0 * ah / cal["window_ah"], 1),
              "ah_above_floor": None if ah is None else round(ah, 1),
              "voltage": r[1], "monitor_soc_pct": r[3]}
        if b == bucket:
            points[-1] = pt
        else:
            points.append(pt)
            bucket = b
    events = [dict(_anchor_view(a, cfg), time=history.clock(a["ts"], cfg),
                   label=("100% (tapered top)" if a["kind"] == "top"
                          else "0% (52.0 V floor)"))
              for a in anchors(conn, since=start) if a["ts"] < end]
    return {"date": day, "tz": cfg["tz"], "step_min": step_min,
            "window_ah": cal["window_ah"], "cef": cal["cef"],
            "points": points, "anchors": events}
