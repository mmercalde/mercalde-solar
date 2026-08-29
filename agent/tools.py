"""Tools exposed to the model (SPEC section 6).

Read tools are always allowed. The single write tool, set_gen_thresholds,
goes through the guard; a refusal comes back to the model as the tool result
so it can adjust or explain instead of failing silently.

The write is the only one the agent has, and it is exactly:

    GET /config?mep.startVoltage=&mep.stopVoltage=&kub.startVoltage=&kub.stopVoltage=

It never sends chargeRate, maxRuntime or cooldown, and it never calls
/setgen, /stopgen, /writereg, /setmpptmode or /autogen.
"""

import json
import logging
import time

import requests

import counters
import history
import loadmodel
import telegram
import weather

log = logging.getLogger(__name__)

# The four threshold parameters, in the order SPEC section 4 writes them.
THRESHOLD_PARAMS = ("mep.startVoltage", "mep.stopVoltage",
                    "kub.startVoltage", "kub.stopVoltage")

# Marks the agent's own writes in the Pi5 access log so agent_watchdog.sh can
# find them without app.py changing (SPEC section 9). app.py ignores unknown
# query parameters, and the header costs nothing if logging is ever extended.
AGENT_MARKER = ("src", "agent")
AGENT_HEADER = {"X-Agent": "solar-agent"}

# Parameters the agent must never send, asserted on every write.
FORBIDDEN_PARAMS = ("mep.chargeRate", "mep.maxRuntime", "mep.cooldown",
                    "kub.chargeRate", "kub.maxRuntime", "kub.cooldown",
                    "autoGenEnabled", "tg.token", "tg.chatId", "tg.enabled",
                    "ramp.stepDelay", "ramp.zeroHoldTime")


def apply_thresholds(cfg, mep_start, mep_stop, kub_start, kub_stop,
                     timeout=15, session=None):
    """Issue the one write the agent is allowed. Returns the live config back.

    Kept separate from the tool so the exact request shape can be tested
    without a guard, a model or a database.
    """
    params = {
        "mep.startVoltage": f"{float(mep_start):.1f}",
        "mep.stopVoltage": f"{float(mep_stop):.1f}",
        "kub.startVoltage": f"{float(kub_start):.1f}",
        "kub.stopVoltage": f"{float(kub_stop):.1f}",
        AGENT_MARKER[0]: AGENT_MARKER[1],
    }
    assert not set(params) & set(FORBIDDEN_PARAMS), "forbidden parameter in write"
    get = (session or requests).get
    r = get(cfg["dashboard_url"] + "/config", params=params,
            headers=AGENT_HEADER, timeout=timeout)
    r.raise_for_status()
    return r.json()["config"]


GEN_LABELS = (("MEP", "mep_start", "mep_stop"), ("Kubota", "kub_start", "kub_stop"))


def describe_write(before, applied, voltage):
    """The change, as cause and effect, in one line each.

    "Thresholds set" with four numbers reads like the Pi5's own 52 V
    auto-start once it is on a phone screen at three in the morning. The
    owner needs to know the agent did this, what it did, and what will happen
    next.
    """
    def moved(was, now, label, what):
        if was is not None and abs(was - now) < 0.05:
            return None
        verb = "raised" if was is None or now > was else "lowered"
        return (f"{verb} {label} {what} {was} → {now}" if was is not None
                else f"set {label} {what} to {now}")

    changes, effects = [], []
    for label, skey, pkey in GEN_LABELS:
        change = moved((before or {}).get(skey), applied[skey], label, "start")
        if change:
            changes.append(change)
            if voltage is not None:
                effects.append(f"{label} will start now" if applied[skey] > voltage
                               else f"{label} will start when the pack falls to "
                                    f"{applied[skey]}")
        change = moved((before or {}).get(pkey), applied[pkey], label, "stop")
        if change:
            changes.append(change)
    return changes, effects


def write_message(applied, reason, before=None, voltage=None, default_start=None):
    """What the owner is told about a threshold write.

    "Thresholds set" with four numbers reads like the Pi5's own low-voltage
    auto-start once it is on a phone at three in the morning, so the message
    leads with who did what and says what will happen because of it.
    """
    changes, effects = describe_write(before, applied, voltage)
    head = ("Agent " + ", ".join(changes)) if changes else "Agent set the thresholds"
    lines = [f"⚙️ <b>{telegram.escape(head)}</b>", telegram.escape(reason)]
    if effects:
        tail = "; ".join(effects) + ". This is the agent"
        if default_start is not None:
            tail += f", not the Pi5's {default_start} V auto-start"
        lines.append(telegram.escape(tail + "."))
    lines.append(f"Now MEP {applied['mep_start']} / {applied['mep_stop']}, "
                 f"Kubota {applied['kub_start']} / {applied['kub_stop']}"
                 + (f"; pack {voltage} V" if voltage is not None else ""))
    return "\n".join(lines)


def thresholds_from_config(live):
    """Pull the four values the agent owns out of a /config response."""
    return {
        "mep_start": live["mep803a"]["startVoltage"],
        "mep_stop": live["mep803a"]["stopVoltage"],
        "kub_start": live["kubota"]["startVoltage"],
        "kub_stop": live["kubota"]["stopVoltage"],
    }


SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_status",
        "description": "Current battery, solar, load and generator state, plus "
                       "the live generator thresholds.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_history",
        "description": "Min/max/average voltage, solar Wh, load Wh and generator "
                       "minutes over the last N hours.",
        "parameters": {"type": "object", "properties": {
            "hours": {"type": "integer", "description": "How many hours back, 1 to 720."}},
            "required": ["hours"]}}},
    {"type": "function", "function": {
        "name": "get_load_forecast",
        "description": "Expected house consumption in Wh for the next N hours, "
                       "and the projected time the pack reaches 52.0 V.",
        "parameters": {"type": "object", "properties": {
            "hours": {"type": "integer", "description": "How many hours ahead, 1 to 48."}},
            "required": ["hours"]}}},
    {"type": "function", "function": {
        "name": "get_gen_runtime",
        "description": "Generator runs over the last N days with observed charge "
                       "rates in amps into the pack, per generator totals, and "
                       "solo vs paired rates.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "How many days back, 1 to 365."}},
            "required": ["days"]}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Cloud cover, solar radiation, temperature and sunrise/sunset "
                       "for the next 48 hours, with the estimated solar yield.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_ac_diag",
        "description": "Per-inverter AC voltage, frequency, power and current. "
                       "Use when investigating an AC anomaly.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "send_telegram",
        "description": "Send a message to the owner. Use this instead of acting "
                       "when you are unsure.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "The message."}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "set_gen_thresholds",
        "description": "Set both generators' start and stop voltages. This is the "
                       "only change you can make. The guard may refuse; if it does, "
                       "the refusal explains why.",
        "parameters": {"type": "object", "properties": {
            "mep_start": {"type": "number", "description": "MEP-803A start voltage."},
            "mep_stop": {"type": "number", "description": "MEP-803A stop voltage."},
            "kub_start": {"type": "number", "description": "Kubota start voltage."},
            "kub_stop": {"type": "number", "description": "Kubota stop voltage."},
            "reason": {"type": "string", "description": "One line saying why."}},
            "required": ["mep_start", "mep_stop", "kub_start", "kub_stop", "reason"]}}},
]

READ_TOOLS = {"get_status", "get_history", "get_load_forecast",
              "get_gen_runtime", "get_weather", "get_ac_diag", "send_telegram"}
WRITE_TOOLS = {"set_gen_thresholds"}


class Tools:
    """Dispatch table for the model.

    `guard` must expose check(**kwargs) -> (allowed: bool, reason: str).
    Passing None refuses every write, which is the safe default.
    """

    def __init__(self, conn, cfg, guard=None, dry_run=False, policy=None):
        self._conn = conn
        self.cfg = cfg
        self.guard = guard
        self.dry_run = dry_run
        # This tick's POLICY evaluation, so the guard can tell a rule-driven
        # change from a drift back to config defaults. None outside a tick.
        self.policy = policy
        self.model = loadmodel.LoadModel(conn, cfg)
        self.calls = []

    @property
    def conn(self):
        return history.resolve(self._conn)

    # --- read ---------------------------------------------------------------

    def get_status(self):
        data = history.fetch_data(self.cfg)
        live = history.fetch_config(self.cfg)
        solar = sum(data.get(k) or 0 for k in
                    ("mppt80PVPower", "southArrayPVPower", "westArrayPVPower"))
        gen_running = (data.get("mep803aAction") == history.GEN_RUNNING
                       or data.get("kubotaAction") == history.GEN_RUNNING)
        return {
            "voltage": data.get("batteryVoltage"),
            "soc_pct": data.get("battSocBM"),
            "battery_w": data.get("battPower"),
            "battery_a": data.get("battCurrent"),
            "battery_monitor_online": data.get("battMonitorOnline"),
            "ah_remaining": data.get("battAhRemaining"),
            "minutes_to_discharge": data.get("battMinToDischarge"),
            "solar_w": solar,
            "solar_by_array_w": {
                "mppt80": data.get("mppt80PVPower"),
                "south": data.get("southArrayPVPower"),
                "west": data.get("westArrayPVPower")},
            # AC output is house load only when no generator is feeding the inverters.
            "load_w": (None if gen_running else
                       (data.get("acPower1") or 0) + (data.get("acPower2") or 0)),
            "generator_running": gen_running,
            "mep": {"running": data.get("mep803aAction") == history.GEN_RUNNING,
                    "mode": data.get("mep803aMode"),
                    "ags_online": data.get("mepAgsOnline"),
                    "start_v": live["mep803a"]["startVoltage"],
                    "stop_v": live["mep803a"]["stopVoltage"],
                    "max_runtime_min": live["mep803a"]["maxRuntime"]},
            "kubota": {"running": data.get("kubotaAction") == history.GEN_RUNNING,
                       "mode": data.get("kubotaMode"),
                       "ags_online": data.get("kubotaAgsOnline"),
                       "start_v": live["kubota"]["startVoltage"],
                       "stop_v": live["kubota"]["stopVoltage"],
                       "max_runtime_min": live["kubota"]["maxRuntime"]},
            "auto_gen_enabled": data.get("autoGenEnabled"),
            "poll_errors": data.get("pollErrors"),
            "last_update": data.get("lastUpdate"),
        }

    def get_history(self, hours):
        hours = max(1, min(int(hours), 720))
        out = history.summary(self.conn, hours)
        today = history.local_day(int(time.time()), self.cfg)
        row = self.conn.execute("SELECT * FROM daily WHERE day=?", (today,)).fetchone()
        if row:
            out["today"] = {"solar_wh": row["solar_wh"], "load_wh": row["load_wh"],
                            "peak_v": row["peak_v"], "min_v": row["min_v"],
                            "mep_minutes": row["mep_minutes"],
                            "kub_minutes": row["kub_minutes"]}
        check = counters.cross_check(self.conn, self.cfg)
        if check:
            out["counter_cross_check"] = check
        return out

    def get_load_forecast(self, hours):
        hours = max(1, min(int(hours), 48))
        out = self.model.load_forecast(hours)
        out["projected_52v"] = self.model.project_voltage(52.0)
        drawdown = self.model.overnight_drawdown()
        if drawdown:
            out["overnight_drawdown"] = drawdown
        return out

    def get_gen_runtime(self, days):
        days = max(1, min(int(days), 365))
        runs = history.gen_runs(self.conn, days)
        totals = {}
        for r in runs:
            totals.setdefault(r["gen"], {"minutes": 0.0, "runs": 0})
            totals[r["gen"]]["minutes"] += r["duration_min"] or 0
            totals[r["gen"]]["runs"] += 1
        return {
            "days": days,
            "totals": {g: {"minutes": round(t["minutes"], 1), "runs": t["runs"]}
                       for g, t in totals.items()},
            "charge_rates": self.model.charge_rates(),
            "runs": [{"gen": r["gen"],
                      "start": history.stamp(r["start_ts"], self.cfg),
                      "minutes": r["duration_min"],
                      "start_v": r["start_v"], "stop_v": r["stop_v"],
                      "amps_into_pack": (round(r["rate_a"], 1)
                                         if r["rate_a"] is not None else None),
                      "house_load_w": (round(r["load_w"])
                                       if r["load_w"] is not None else None),
                      "observed_v_per_h": (round(r["rate_v_per_h"], 2)
                                           if r["rate_v_per_h"] is not None
                                           else None),
                      "solo": bool(r["solo"]), "kind": r["kind"]}
                     for r in runs],
            "note": "Exercise runs are excluded. A charge rate is amps into "
                    "the pack and the state of charge per hour that gives; "
                    "observed_v_per_h is what the terminal voltage did under "
                    "whatever the house was drawing at the time, and is not a "
                    "generator's rate. Do not plan from it.",
        }

    def get_weather(self):
        out = weather.summary(self.cfg)
        for day in ("today", "tomorrow"):
            w = out.get(day)
            if not w:
                continue
            est = self.model.estimate_solar_wh(
                w.get("daylight_cloud_pct") if w.get("daylight_cloud_pct") is not None
                else w["cloud_pct"])
            w["estimated_solar_wh"] = est["wh"] if est else None
            w["clear_day_wh"] = est["clear_day_wh"] if est else None
        return out

    def get_ac_diag(self):
        r = requests.get(self.cfg["dashboard_url"] + "/acdiag", timeout=15)
        r.raise_for_status()
        return r.json()

    def send_telegram(self, text):
        # The model's words are text, not markup.
        if self.dry_run:
            return {"sent": False, "dry_run": True, "text": text}
        return {"sent": telegram.send(self.cfg, telegram.escape(text))}

    # --- write --------------------------------------------------------------

    def set_gen_thresholds(self, mep_start, mep_stop, kub_start, kub_stop, reason):
        args = {"mep_start": float(mep_start), "mep_stop": float(mep_stop),
                "kub_start": float(kub_start), "kub_stop": float(kub_stop),
                "reason": reason}
        if self.guard is None:
            return {"applied": False,
                    "reason": "no guard is attached, so no write is permitted"}
        values = {k: v for k, v in args.items() if k != "reason"}
        allowed, why = self.guard.check(policy=self.policy, **args)
        if not allowed:
            # The values go back too: a refusal is the guard's decision, not a
            # failure to propose, and the plan record scores those separately.
            return {"applied": False, "refused_by": "guard",
                    "would_set": values, "reason": why}
        if self.dry_run:
            return {"applied": False, "dry_run": True,
                    "would_set": values, "reason": why}
        live = apply_thresholds(self.cfg, mep_start, mep_stop, kub_start, kub_stop)
        applied = thresholds_from_config(live)
        self.guard.note_write(applied)
        # Every executed write tells the owner, in Python, with the values the
        # dashboard read back rather than the ones that were asked for. The
        # 04:10 write on the first live night sent nothing, because the model
        # was trusted to call send_telegram itself and did not.
        seen = getattr(self.guard, "last_seen", None) or {}
        notified = telegram.send(self.cfg, write_message(
            applied, reason, before=seen.get("thresholds"),
            voltage=seen.get("voltage"),
            default_start=self.cfg["default_start"]))
        return {"applied": True, "now": applied, "reason": reason,
                "notified": notified}

    # --- dispatch -----------------------------------------------------------

    def call(self, name, args):
        """Run one tool. Returns a JSON string for the model."""
        fn = getattr(self, name, None)
        if fn is None or name not in READ_TOOLS | WRITE_TOOLS:
            return json.dumps({"error": f"no such tool: {name}"})
        try:
            result = fn(**args)
        except TypeError as e:
            result = {"error": f"bad arguments for {name}: {e}"}
        except requests.RequestException as e:
            result = {"error": f"{name} could not reach the dashboard: {e}"}
        except Exception as e:                      # noqa: BLE001 - never kill the tick
            log.exception("tool %s failed", name)
            result = {"error": f"{name} failed: {e}"}
        self.calls.append((name, args, result))
        return json.dumps(result, default=str)
