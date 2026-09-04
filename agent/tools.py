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
from datetime import datetime, timedelta

import requests

import counters
import fuel as fuelmod
import guard as guardmod
import health as healthmod
import history
import loadmodel
import monthly as monthlymod
import policy as policymod
import sun as sunmod
import system as systemmod
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


class WriteNotApproved(RuntimeError):
    """A write was attempted that the guard had not permitted."""


def apply_thresholds(cfg, mep_start, mep_stop, kub_start, kub_stop, *,
                     approval, timeout=15, session=None):
    """Issue the one write the agent is allowed. Returns the live config back.

    `approval` is the guard's record of the decision that permitted exactly
    these four values, from Guard.approval(). It is required and it is
    checked: the heartbeat used to reach the dashboard without passing
    check(), which is how a stale stored intent was written over the owner's
    thresholds twice in full sun without the daylight hold or the audit log
    ever seeing it. There is no unguarded path now.
    """
    want = {"mep_start": float(mep_start), "mep_stop": float(mep_stop),
            "kub_start": float(kub_start), "kub_stop": float(kub_stop)}
    ok = approval.get("values") if isinstance(approval, dict) else None
    if not ok or any(abs(ok.get(k, -999) - v) > 0.05 for k, v in want.items()):
        raise WriteNotApproved(
            f"the guard has not approved {want}; it approved {ok}")
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


def write_message(applied, reason, before=None, voltage=None,
                  default_start=None, refused=None, numbers=None):
    """What the owner is told about a threshold write.

    "Thresholds set" with four numbers reads like the Pi5's own low-voltage
    auto-start once it is on a phone at three in the morning, so the message
    leads with who did what and says what will happen because of it.
    """
    changes, effects = describe_write(before, applied, voltage)
    head = ("Agent " + ", ".join(changes)) if changes else "Agent set the thresholds"
    lines = [f"⚙️ <b>{telegram.escape(head)}</b>", telegram.escape(reason)]
    # The arithmetic the rule fired on, so the message and the plan record
    # cannot give the owner two different accounts of one decision.
    if numbers:
        lines.append(telegram.escape(numbers))
    if effects:
        tail = "; ".join(effects) + ". This is the agent"
        if default_start is not None:
            tail += f", not the Pi5's {default_start} V auto-start"
        lines.append(telegram.escape(tail + "."))
    for part in (refused or []):
        lines.append(telegram.escape(f"Not done: {part}"))
    lines.append(f"Now MEP {applied['mep_start']} / {applied['mep_stop']}, "
                 f"Kubota {applied['kub_start']} / {applied['kub_stop']}"
                 + (f"; pack {voltage} V" if voltage is not None else ""))
    return "\n".join(lines)


def refusal_message(refusals):
    """What the owner is told when the agent proposed something and was told no."""
    lines = []
    for r in refusals:
        v = r["values"]
        lines.append(f"proposed MEP {v['mep_start']}/{v['mep_stop']}, "
                     f"Kubota {v['kub_start']}/{v['kub_stop']} — "
                     f"refused: {r['reason']}")
    return "\n".join(lines)


# How a point-in-time question is allowed to name its moment. A bare clock
# time means the most recent time it was: "2:47 am" asked at lunchtime is this
# morning, not tomorrow.
WHEN_FORMATS_DATED = ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M",
                      "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
# A month and day with no year: the model reaches for these when the owner
# named a night without one. The year is the most recent that puts the moment
# in the past.
WHEN_FORMATS_MONTHDAY = ("%m-%d %I:%M %p", "%m-%d %I:%M%p", "%m-%d %H:%M",
                         "%m/%d %I:%M %p", "%m/%d %I:%M%p", "%m/%d %H:%M",
                         "%b %d %I:%M %p", "%d %b %I:%M %p")
WHEN_FORMATS_TIME = ("%I:%M %p", "%I:%M%p", "%H:%M")
# A sample further from the moment asked about than this is not an answer to
# the question; the pack can move a long way in five minutes under load.
SAMPLE_WINDOW_SECONDS = 300


# Day words the owner and the model both use around a clock time, and how far
# back each one puts it. "last night" needs no shift: 2:47 am asked about in
# the afternoon is already the most recent 2:47 am there was.
DAY_WORDS = (
    ("the day before yesterday", 2), ("day before yesterday", 2),
    ("yesterday morning", 1), ("yesterday evening", 1),
    ("yesterday night", 1), ("yesterday", 1),
    ("last night", 0), ("overnight", 0), ("tonight", 0),
    ("this morning", 0), ("this afternoon", 0), ("this evening", 0),
    ("early this morning", 0), ("today", 0), ("at", 0), ("around", 0),
    ("exactly", 0), ("about", 0), ("on", 0),
)


def parse_when(text, cfg, now=None):
    """(timestamp, None) for a moment the owner named, or (None, why not).

    Tolerant of the way the question was actually asked. The model relays the
    owner's own words - "2:47 am last night" - and a parser that took only a
    bare clock time turned a question with an answer in the database into an
    apology about timestamp formats.
    """
    now = int(now or time.time())
    raw = str(text or "").strip()
    if not raw:
        return None, "no time was given"
    if raw.isdigit():
        return int(raw), None
    tz = history.tzinfo(cfg)
    try:
        parsed = datetime.fromisoformat(raw)
        return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=tz))
                   .timestamp()), None
    except ValueError:
        pass

    cleaned = " ".join(raw.lower().replace(",", " ").replace("(", " ")
                       .replace(")", " ").replace("a.m.", "am")
                       .replace("p.m.", "pm").replace("o'clock", "").split())
    days_back = 0
    for word, back in DAY_WORDS:
        if word in cleaned:
            cleaned = " ".join(cleaned.replace(word, " ").split())
            days_back = max(days_back, back)
    if not cleaned:
        return None, f"no clock time in {raw!r}"
    for fmt in WHEN_FORMATS_DATED:
        try:
            when = datetime.strptime(cleaned, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
        return int((when - timedelta(days=days_back)).timestamp()), None
    for fmt in WHEN_FORMATS_MONTHDAY:
        try:
            t = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        here = history.local(now, cfg)
        when = t.replace(year=here.year, tzinfo=tz)
        if when.timestamp() > now:          # never a date still to come
            when = when.replace(year=here.year - 1)
        return int((when - timedelta(days=days_back)).timestamp()), None
    for fmt in WHEN_FORMATS_TIME:
        try:
            t = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        local = history.local(now, cfg).replace(hour=t.hour, minute=t.minute,
                                                second=0, microsecond=0)
        # The most recent time it was that o'clock, never a future one.
        if local.timestamp() > now:
            local -= timedelta(days=1)
        return int((local - timedelta(days=days_back)).timestamp()), None
    return None, (f"could not read {raw!r} as a time; use 'HH:MM', "
                  f"'H:MM am' or 'YYYY-MM-DD H:MM am'")


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
                       "the live generator thresholds. Each generator carries "
                       "run_reason - the AGS's own reason it is running, e.g. "
                       "\"exercise\", \"dc_voltage_low\", \"manual_on\" - and "
                       "started_at and running_minutes while it runs. This is "
                       "the only source for why a generator started.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_history",
        "description": "Voltage, solar Wh, load Wh, battery Wh out and generator "
                       "minutes over a span. For overnight / last night / since "
                       "sunset use window=\"overnight\"; for today or yesterday "
                       "use window=\"today\" or window=\"yesterday\". hours=N is "
                       "a trailing window ending now and is NOT overnight. "
                       "Example: get_history(window=\"overnight\") for last "
                       "night; get_history(hours=72) for the last three days.",
        "parameters": {"type": "object", "properties": {
            "hours": {"type": "integer", "description": "Trailing hours ending "
                      "now, 1 to 720. Ignored when window is given."},
            "window": {"type": "string", "enum": ["overnight", "today", "yesterday"],
                       "description": "A named span. overnight is sunset to now, "
                       "or the night just ended once the sun is up; today is "
                       "since local midnight; yesterday is the previous local "
                       "day. Preferred whenever the question uses one of these "
                       "words."}},
            "required": []}}},
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
                       "solo vs paired rates. Also `fuel`: hours and estimated "
                       "gallons per generator for today and month to date, "
                       "already summed. Quote those figures; never add up the "
                       "run rows yourself.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "How many days back, 1 to 365."}},
            "required": ["days"]}}},
    {"type": "function", "function": {
        "name": "get_monthly_summary",
        "description": "Every calendar month of the record: solar kWh, load "
                       "kWh, net kWh, generator hours and gallons per "
                       "generator, minimum and maximum pack voltage, the best "
                       "and worst solar day, and how many days of data the "
                       "month has. Plus `superlatives`: worst_solar_month, "
                       "best_solar_month, highest_load_month, "
                       "most_fuel_month, each already worked out. Call this "
                       "for any question about which month was best, worst, "
                       "highest or used the most fuel, and read the answer "
                       "out of the superlative field. Do not rank the months "
                       "yourself. By default it returns the superlatives and "
                       "a compact table of recent months, which is what those "
                       "questions need; ask for detail only when the question "
                       "is about one particular month's days. "
                       "For a question about a specific month, pass month=. "
                       "months=N means the most recent N months and is for "
                       "trends; never use months=1 to get a named month - it "
                       "returns the latest month, not the one you meant. "
                       "For December 2025: {\"month\": \"2025-12\"}. "
                       "For the last six months: {\"months\": 6}.",
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string",
                      "description": "One month by name, and the table is "
                                     "that month alone. \"2025-12\", "
                                     "\"12-2025\", \"Dec 2025\" and "
                                     "\"December 2025\" all work. Use this "
                                     "whenever the question names a month. "
                                     "`months` is ignored when it is set."},
            "months": {"type": "integer",
                       "description": "How many of the MOST RECENT months to "
                                      "table, for looking at a trend. Default "
                                      "12; a large number gives all of them. "
                                      "This cannot reach a named month: "
                                      "months=1 is the latest month, not the "
                                      "one asked about. The superlatives "
                                      "always rank the whole record whatever "
                                      "this is."},
            "detail": {"type": "boolean",
                       "description": "Default false. True adds each month's "
                                      "best and worst solar day, "
                                      "per-generator hours and gallons, and "
                                      "its provenance notes - several times "
                                      "the size. Use it only when asked "
                                      "about a specific month's days."}}}}},
    {"type": "function", "function": {
        "name": "battery_health",
        "description": "Battery longevity and ageing, all computed: amp-hours "
                       "out, equivalent full cycles, cycles a year, mean daily "
                       "depth of discharge, mean resting voltage; a measured "
                       "capacity series by month with a confidence note; and a "
                       "projection of years to 80% by cycling and by calendar "
                       "with its assumptions listed. Call this for any question "
                       "about battery life, health, capacity, wear or ageing, "
                       "and answer from these fields. There is no state of "
                       "charge in it, deliberately.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_voltage_at",
        "description": "Battery voltage, state of charge, house load and "
                       "generator state at one moment in the past, from the "
                       "minute sample nearest it. The ONLY way to answer a "
                       "question about a specific time. get_history returns a "
                       "window's minimum, maximum and average, none of which "
                       "is the reading at a moment.",
        "parameters": {"type": "object", "properties": {
            "timestamp": {"type": "string",
                          "description": "The moment asked about: 'HH:MM', "
                                         "'H:MM am' for the most recent time "
                                         "it was that, or 'YYYY-MM-DD H:MM am'."}},
            "required": ["timestamp"]}}},
    {"type": "function", "function": {
        "name": "get_system_specs",
        "description": "The system manifest: inverters, generators, battery, "
                       "arrays, network and the policy constants, as recorded "
                       "in system.yaml. Use it for what the hardware is.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_mppt_detail",
        "description": "Per-controller solar: live watts, volts and amps, plus "
                       "today's and this month's kWh from the energy counters.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_battery_detail",
        "description": "The Battery Monitor in full: amp-hours remaining, "
                       "minutes to discharge, net current, and the learned "
                       "capacity and voltage/SOC curve.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_guard_state",
        "description": "What the guard will and will not permit right now: the "
                       "owner's baseline, the rate-limit clock, the daylight "
                       "hold, the learning gate and the hard limits.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_recent_actions",
        "description": "The last N entries from the audit log: every write "
                       "attempted, whether it was allowed or refused, and why.",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "How many, 1 to 50."}},
            "required": ["n"]}}},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Cloud cover, solar radiation, temperature and sunrise/sunset "
                       "for the next 48 hours, with the estimated solar yield. "
                       "next_daylight is the day the sun next comes up on, "
                       "named by next_daylight_date; that is the day a stop "
                       "voltage set tonight is charging for.",
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
              "get_gen_runtime", "get_voltage_at", "get_weather", "get_ac_diag",
              "battery_health", "get_monthly_summary",
              "get_system_specs", "get_mppt_detail", "get_battery_detail",
              "get_guard_state", "get_recent_actions", "send_telegram"}
WRITE_TOOLS = {"set_gen_thresholds"}

# What a tool result may cost before it is worth saying so out loud. Roughly
# 1,500 tokens: enough for any answer here, and well inside what the KAMRUI
# can read and reason over inside the model timeout.
TOOL_RESULT_WARN_CHARS = 6000


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
        # Writes the guard turned down this tick. A refusal makes anything the
        # model then says about changing the thresholds untrue.
        self.refusals = []

    @property
    def conn(self):
        return history.resolve(self._conn)

    # --- read ---------------------------------------------------------------

    @staticmethod
    def _house_load(data, gen_running):
        """(watts, note). The note is the point.

        `load_w` has three ways of not being the house's draw and they all
        used to look alike. On 2026-09-03 the agent reported "no load is
        being drawn" while the Kubota was running and the field was None.
        None is not zero, zero is not always the house, and neither is a
        reading nobody should quote without the sentence beside it.
        """
        ac1, ac2 = data.get("acPower1"), data.get("acPower2")
        if gen_running:
            return None, ("not measurable right now: a generator is feeding "
                          "the inverters, so their AC output is the "
                          "generator's, not the house's. This is not zero "
                          "load - say the house draw cannot be read while a "
                          "generator is running.")
        if ac1 is None and ac2 is None:
            return None, ("not reported in this poll. Say it is not "
                          "reported; do not read it as no load.")
        total = (ac1 or 0) + (ac2 or 0)
        if total == 0:
            return 0, ("the inverters reported 0 W. They read zero for a few "
                       "seconds during an AC transfer, so this is as likely "
                       "to be the reading as the house. Do not state that "
                       "nothing is being drawn on one poll of it.")
        return total, None

    def get_status(self):
        data = history.fetch_data(self.cfg)
        live = history.fetch_config(self.cfg)
        solar = sum(data.get(k) or 0 for k in
                    ("mppt80PVPower", "southArrayPVPower", "westArrayPVPower"))
        gen_running = (data.get("mep803aAction") == history.GEN_RUNNING
                       or data.get("kubotaAction") == history.GEN_RUNNING)
        load_w, load_note = self._house_load(data, gen_running)
        gen_status = {}
        for gen, act_key, reason_key in (
                ("mep", "mep803aAction", "mepOnReason"),
                ("kubota", "kubotaAction", "kubotaOnReason")):
            running = data.get(act_key) == history.GEN_RUNNING
            # The AGS's answer, and the only answer. Where the register did
            # not reach /data the field says so rather than going quiet: a
            # missing reason is a fact, and it is not the same fact as a
            # generator nobody started.
            gen_status[gen] = {
                "run_reason": data.get(reason_key),
                "run_reason_note": (
                    None if data.get(reason_key) else
                    "not recorded: the AGS's Generator On Reason did not "
                    "reach this poll. Say the reason is not recorded. Never "
                    "infer one from voltage or from the thresholds.")}
            if running:
                run = history.current_run(self.conn, gen)
                if run:
                    gen_status[gen]["started_at"] = history.stamp(
                        run["started_at"], self.cfg)
                    gen_status[gen]["running_minutes"] = run["running_minutes"]
                    if run["truncated"]:
                        gen_status[gen]["running_minutes_note"] = (
                            "at least: the run began before the samples "
                            "this looked back through")
                    if run["on_reason"] and not gen_status[gen]["run_reason"]:
                        gen_status[gen]["run_reason"] = run["on_reason"]
                        gen_status[gen]["run_reason_note"] = (
                            "from the sample at the minute it started, not "
                            "from this poll")
        return {
            "voltage": data.get("batteryVoltage"),
            "soc_pct_note": "not reported: the Battery Monitor's state of charge scale is unreliable and nothing may be quoted from it. What the pack holds is watt-hours between two voltages; how it is ageing is the battery_health tool.",
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
            "load_w": load_w,
            "load_w_note": load_note,
            "generator_running": gen_running,
            "mep": {"running": data.get("mep803aAction") == history.GEN_RUNNING,
                    "mode": data.get("mep803aMode"),
                    "ags_online": data.get("mepAgsOnline"),
                    "start_v": live["mep803a"]["startVoltage"],
                    "stop_v": live["mep803a"]["stopVoltage"],
                    "max_runtime_min": live["mep803a"]["maxRuntime"],
                    **gen_status["mep"]},
            "kubota": {"running": data.get("kubotaAction") == history.GEN_RUNNING,
                       "mode": data.get("kubotaMode"),
                       "ags_online": data.get("kubotaAgsOnline"),
                       "start_v": live["kubota"]["startVoltage"],
                       "stop_v": live["kubota"]["stopVoltage"],
                       "max_runtime_min": live["kubota"]["maxRuntime"],
                       **gen_status["kubota"]},
            "auto_gen_enabled": data.get("autoGenEnabled"),
            "poll_errors": data.get("pollErrors"),
            "last_update": data.get("lastUpdate"),
        }

    def get_history(self, hours=24, window=None):
        """A trailing window of `hours`, or a window the owner named.

        `window` wins when both arrive: "overnight" is a span with a sunset
        at one end, and no number of trailing hours is that span.
        """
        if window:
            since, until, label = sunmod.window_span(self.cfg, window,
                                                     int(time.time()))
            if since is None:
                return {"error": label, "windows": list(sunmod.WINDOWS)}
            out = history.summary(self.conn, since=since, until=until,
                                  cfg=self.cfg)
            out["window"] = window
            out["window_label"] = label
            out["window_note"] = (
                f"This is {label} - state that span in the answer. It is not "
                f"the last {out['hours']:.0f} hours and must not be called "
                f"a day's figure.")
            hours = out["hours"]
        else:
            hours = max(1, min(int(hours), 720))
            out = history.summary(self.conn, hours, cfg=self.cfg)
            out["window"] = None
            out["window_note"] = (
                f"A trailing {hours}-hour window ending now. This is NOT "
                f"overnight and NOT a calendar day; for those pass "
                f"window='overnight', 'today' or 'yesterday'.")
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

    def fuel_totals(self, now=None):
        """Hours and gallons per generator, today and month to date.

        Summed here and handed over as named numbers. The model is never
        asked to add up a list of rows: it has invented a voltage from a
        prompt before now, and arithmetic over twenty rows is exactly the
        kind of thing it will do confidently and wrongly. Every figure below
        is Python's.

        `fuel_*_unpriced_runs` is how many runs in that window had no fuel
        figure, so a total that is short says so instead of reading as the
        whole truth.
        """
        now = int(now or time.time())
        tz = history.tzinfo(self.cfg)
        t = datetime.fromtimestamp(now, tz)
        day_start = int(t.replace(hour=0, minute=0, second=0,
                                  microsecond=0).timestamp())
        month_start = int(t.replace(day=1, hour=0, minute=0, second=0,
                                    microsecond=0).timestamp())
        prev_end = month_start - 1
        prev_start = int(datetime.fromtimestamp(prev_end, tz).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        windows = (("today", day_start, now), ("mtd", month_start, now),
                   ("last_month", prev_start, prev_end))
        out = {}
        for gen in history.GENS:
            row = {}
            for label, since, until in windows:
                r = self.conn.execute(
                    "SELECT COUNT(*) runs, COALESCE(SUM(duration_min), 0) mins, "
                    "       SUM(fuel_gal) gal, "
                    "       SUM(fuel_gal IS NULL) unpriced "
                    "FROM gen_runs WHERE gen=? AND kind != 'exercise' "
                    "AND start_ts >= ? AND start_ts <= ?",
                    (gen, since, until)).fetchone()
                runs, gal = int(r["runs"] or 0), r["gal"]
                # A window with no runs burned nothing, and nothing is 0.0.
                # None is reserved for the one case where it means something:
                # runs happened and not one of them could be priced.
                if runs == 0:
                    gal = 0.0
                row[f"runs_{label}"] = runs
                row[f"hours_{label}"] = round((r["mins"] or 0) / 60.0, 2)
                row[f"fuel_{label}_gal"] = (round(gal, 2)
                                            if gal is not None else None)
                row[f"fuel_{label}_unpriced_runs"] = int(r["unpriced"] or 0)
                c = fuelmod.cost(self.cfg, gal)
                if c is not None:
                    row[f"fuel_{label}_cost"] = c
            out[gen] = row
        out["summary"] = self._fuel_summary(out, t, tz, prev_end)
        out["note"] = ("Gallons are modelled from published consumption "
                       "curves, not metered: each generator's curve read at "
                       "the gross output the run actually delivered, with a "
                       "minute both engines were running split between them "
                       "by their learned solo rates. Hours exclude the 09:00 "
                       "exercise runs, which are not the agent's and are not "
                       "a signal. Quote the summary as it stands and say "
                       "modelled.")
        return out

    def _fuel_summary(self, per_gen, t, tz, prev_end):
        """One sentence a person can read, built here rather than by the model.

        Early in a month "no runs this month" is true and useless on its own,
        so the month before is in the same breath.
        """
        def clause(label):
            parts = []
            for gen in sorted(g for g in per_gen if g in history.GENS):
                row = per_gen[gen]
                if not row[f"runs_{label}"]:
                    continue
                gal = row[f"fuel_{label}_gal"]
                parts.append(f"{gen} {row[f'hours_{label}']:.1f} h"
                             + (f" / {gal:.2f} gal" if gal is not None
                                else " / no fuel figure"))
            return ", ".join(parts) if parts else "no generator runs"

        this_month = t.strftime("%B")
        last_month = datetime.fromtimestamp(prev_end, tz).strftime("%B")
        return (f"{this_month} so far: {clause('mtd')}. "
                f"{last_month}: {clause('last_month')}.")

    def get_monthly_summary(self, months=monthlymod.COMPACT_MONTHS,
                            detail=False, month=None):
        """Every calendar month, and which months stand out. All in Python."""
        return monthlymod.monthly_summary(self.conn, self.cfg,
                                          months=int(months),
                                          detail=bool(detail),
                                          month=month)

    def battery_health(self):
        """Longevity, from what the pack has done. All computed in Python."""
        return healthmod.battery_health(self.conn, self.cfg)

    def get_gen_runtime(self, days):
        days = max(1, min(int(days), 365))
        runs = history.gen_runs(self.conn, days)
        totals = {}
        for r in runs:
            totals.setdefault(r["gen"], {"minutes": 0.0, "runs": 0})
            totals[r["gen"]]["minutes"] += r["duration_min"] or 0
            totals[r["gen"]]["runs"] += 1
        now = int(time.time())
        today = history.local_day(now, self.cfg)
        yesterday = history.local_day(now - 86400, self.cfg)

        def day_of(ts):
            day = history.local_day(ts, self.cfg)
            return ("today" if day == today else
                    "yesterday" if day == yesterday else day)

        return {
            "days": days,
            "as_of": history.stamp(now, self.cfg),
            "today": today, "yesterday": yesterday,
            "totals": {g: {"minutes": round(t["minutes"], 1), "runs": t["runs"]}
                       for g, t in totals.items()},
            "fuel": self.fuel_totals(now=now),
            "generators_with_no_runs": sorted(set(history.GENS) - set(totals)),
            "charge_rates": self.model.charge_rates(),
            "runs": [{"gen": r["gen"],
                      "day": day_of(r["start_ts"]),
                      "start": history.stamp(r["start_ts"], self.cfg),
                      "minutes": r["duration_min"],
                      "start_v": r["start_v"], "stop_v": r["stop_v"],
                      "amps_into_pack": (round(r["rate_a"], 1)
                                         if r["rate_a"] is not None else None),
                      "house_load_w": (round(r["load_w"])
                                       if r["load_w"] is not None else None),
                      "fuel_gal": r["fuel_gal"],
                      "gross_w": (round(r["gross_w"])
                                  if r["gross_w"] is not None else None),
                      "gross_attributed_w": (round(r["gross_attr_w"])
                                             if r["gross_attr_w"] is not None
                                             else None),
                      "observed_v_per_h": (round(r["rate_v_per_h"], 2)
                                           if r["rate_v_per_h"] is not None
                                           else None),
                      "solo": bool(r["solo"]), "kind": r["kind"]}
                     for r in runs],
            "note": "Every run carries the day it began on. If the question "
                    "assumes a run happened on a day these rows do not show "
                    "one, say so and give the days the runs were actually on; "
                    "do not answer as though the assumption were true. "
                    "Exercise runs are excluded. A charge rate is amps into "
                    "the pack and the state of charge per hour that gives; "
                    "observed_v_per_h is what the terminal voltage did under "
                    "whatever the house was drawing at the time, and is not a "
                    "generator's rate. Do not plan from it.",
        }

    def get_voltage_at(self, timestamp):
        """What the pack was doing at one moment, from the nearest sample.

        A window's minimum is not a reading at a time. Asked what the voltage
        was at 2:47 am, a model with only get_history reached for the
        24-hour minimum and presented it as the answer, 1.4 V out and six
        hours adrift. This is the only tool that answers the question, and it
        says no rather than approximating when it cannot.
        """
        when, why = parse_when(timestamp, self.cfg)
        if when is None:
            # Worth seeing: a timestamp the parser cannot read is a question
            # the owner asked and did not get an answer to.
            log.warning("get_voltage_at could not read timestamp %r: %s",
                        timestamp, why)
            return {"error": why}
        row = self.conn.execute(
            "SELECT * FROM samples "
            "WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) LIMIT 1",
            (when - SAMPLE_WINDOW_SECONDS, when + SAMPLE_WINDOW_SECONDS,
             when)).fetchone()
        asked = history.stamp(when, self.cfg)
        if row is None:
            near = self.conn.execute(
                "SELECT ts FROM samples ORDER BY ABS(ts - ?) LIMIT 1",
                (when,)).fetchone()
            return {"error": f"no sample within "
                             f"{SAMPLE_WINDOW_SECONDS // 60} minutes of {asked}",
                    "asked_for": asked,
                    "nearest_sample": (history.stamp(near["ts"], self.cfg)
                                       if near else None)}
        running = (row["mep_action"] == history.GEN_RUNNING
                   or row["kub_action"] == history.GEN_RUNNING)
        return {
            "asked_for": asked,
            "sample_at": history.stamp(row["ts"], self.cfg),
            "seconds_from_asked": abs(row["ts"] - when),
            "voltage": row["battery_v"],
            "soc_pct_note": "not reported: the Battery Monitor's state of charge scale is unreliable and nothing may be quoted from it. What the pack holds is watt-hours between two voltages; how it is ageing is the battery_health tool.",
            "battery_w": row["batt_power"], "battery_a": row["batt_current"],
            "load_w": (None if running
                       else (row["ac_power1"] or 0) + (row["ac_power2"] or 0)),
            "generator_running": running,
            "mep_running": row["mep_action"] == history.GEN_RUNNING,
            "kubota_running": row["kub_action"] == history.GEN_RUNNING,
        }

    def get_weather(self):
        """The forecast, with the coming daylight named by its date.

        `next_daylight` is the day the sun next comes up on: tomorrow in the
        evening, today between midnight and sunrise, tomorrow again once the
        sun is up. `tomorrow` is the same dict under its old name, for one
        release. The calendar day after now is not always the day the night
        in progress is charging for.
        """
        out = weather.summary(self.cfg)
        for key in ("today", "next_daylight"):
            w = out.get(key)
            if not w:
                continue
            day = (out.get("next_daylight_date") if key == "next_daylight"
                   else out.get("today_date"))
            est = self.model.estimate_solar_wh(weather.cloud_of(w), day=day)
            w["estimated_solar_wh"] = est["wh"] if est else None
            w["clear_day_wh"] = est["clear_day_wh"] if est else None
        return out

    def get_system_specs(self):
        """The manifest. What the hardware is, from the one file that says so."""
        return systemmod.load()

    def get_mppt_detail(self):
        """Each controller on its own: what it is making now, and today."""
        data = history.fetch_data(self.cfg)
        manifest = systemmod.load()
        today = history.local_day(int(time.time()), self.cfg)
        out = []
        for a in manifest["arrays"]["controllers"]:
            key = a["data_key"].replace("PVPower", "")
            row = {"name": a["name"], "slave": a["slave"],
                   "controller": a["controller"],
                   "watts": data.get(a["data_key"]),
                   "pv_volts": data.get(key + "PVVoltage"),
                   "pv_amps": data.get(key + "PVCurrent"),
                   "charge_status": data.get(key + "ChargeStatus"),
                   "observed_peak_w": a.get("observed_peak_w")}
            for period in ("today", "month"):
                c = self.conn.execute(
                    "SELECT kwh FROM counters WHERE device=? AND counter=? "
                    "AND period=? ORDER BY ts DESC LIMIT 1",
                    (a["name"], "energy_from_pv", period)).fetchone()
                row[f"kwh_{period}"] = c["kwh"] if c else None
            out.append(row)
        return {"as_of": history.stamp(int(time.time()), self.cfg),
                "day": today, "controllers": out,
                "total_w": sum(r["watts"] or 0 for r in out),
                "total_kwh_today": round(sum(r["kwh_today"] or 0 for r in out), 3),
                "note": "kWh come from the controllers' own energy counters, "
                        "read over Modbus; watts are the live reading."}

    def get_battery_detail(self):
        """The Battery Monitor in full, and what the agent has learned of the pack."""
        data = history.fetch_data(self.cfg)
        b = systemmod.load()["battery"]
        curve = self.model.soc_curve_status()
        return {
            "as_of": history.stamp(int(time.time()), self.cfg),
            "monitor": {"model": b["monitor"]["model"],
                        "slave": b["monitor"]["slave"],
                        "online": data.get("battMonitorOnline")},
            "voltage": data.get("batteryVoltage"),
            "soc_pct_note": "not reported: the Battery Monitor's state of charge scale is unreliable and nothing may be quoted from it. What the pack holds is watt-hours between two voltages; how it is ageing is the battery_health tool.",
            "ah_remaining": data.get("battAhRemaining"),
            "minutes_to_discharge": data.get("battMinToDischarge"),
            "net_current_a": data.get("battCurrent"),
            "net_power_w": data.get("battPower"),
            "temperature_c": None,
            "temperature_note": "the Battery Monitor publishes no temperature "
                                "over Modbus; there is no cell or pack "
                                "temperature to report",
            "nominal": {"capacity_kwh": b["capacity_kwh_nominal"],
                        "capacity_ah": b["capacity_ah_nominal"],
                        "chemistry": b["chemistry"],
                        "configuration": b["configuration"]},
            "learned": {"capacity_wh": self.model.capacity_wh(),
                        "capacity_ah": self.model.capacity_ah(),
                        "curve_points": curve["points"],
                        "curve_volts": [curve["volts_low"], curve["volts_high"]],
                        "soc_at_start_threshold": curve["soc_at_start_threshold"]},
            "limits": {"floor_v": b["floor_v"], "ceiling_v": b["ceiling_v"],
                       "full_v": b["full_v"]},
        }

    def get_guard_state(self):
        """What the guard will permit right now, and what it will not."""
        if self.guard is None:
            return {"error": "no guard is attached"}
        now = int(time.time())
        g = self.guard
        gate = self.model.learning_status(now=now)
        last_write = g.state.get("last_write_ts") or 0
        since = now - last_write if last_write else None
        daylight = g._daylight(now)
        return {
            "as_of": history.stamp(now, self.cfg),
            "hard_limits": {"start_floor_v": guardmod.HARD_START_FLOOR,
                            "stop_ceiling_v": guardmod.HARD_STOP_CEILING,
                            "note": "code constants; no rule or config widens them"},
            "baseline": g.baseline(),
            "owner_baseline": g.owner_baseline(),
            "intended": g.intended(),
            "raised_starts": g.raised_starts(),
            "rate_limit": {
                "seconds": guardmod.RATE_LIMIT_SECONDS,
                "last_write": (history.stamp(last_write, self.cfg)
                               if last_write else None),
                "minutes_since": round(since / 60) if since is not None else None,
                "in_force": bool(last_write and since < guardmod.RATE_LIMIT_SECONDS),
                "note": "a write that only moves values back toward the "
                        "baseline is exempt"},
            "daylight_hold": {
                "in_daylight": bool(daylight),
                "sunrise": history.clock(daylight[0], self.cfg) if daylight else None,
                "sunset": history.clock(daylight[1], self.cfg) if daylight else None,
                "note": "while the sun is up no write may raise a start above "
                        "the baseline"},
            "owner_stand_down": {
                "until": (history.stamp(g.state["override_until"], self.cfg)
                          if now < g.state.get("override_until", 0) else None)},
            "learning_gate": gate,
        }

    def get_recent_actions(self, n):
        """The audit log: every write attempted, and what the guard said."""
        n = max(1, min(int(n), 50))
        rows = history.recent_actions(self.conn, limit=n)
        return {"count": len(rows),
                "actions": [{"at": history.stamp(r["ts"], self.cfg),
                             "tool": r["tool"], "result": r["result"],
                             "allowed": bool(r["allowed"]),
                             "reason": r["reason"],
                             "voltage": r["voltage"], "soc": r["soc"],
                             "args": r["args"]} for r in rows]}

    def get_ac_diag(self):
        r = requests.get(self.cfg["dashboard_url"] + "/acdiag", timeout=15)
        r.raise_for_status()
        return r.json()

    def send_telegram(self, text):
        """Send the model's message - unless it would be narrating a write.

        At 12:17 am the model told the owner "Adjusted generator thresholds to
        52.0/54.5" after the guard had refused exactly that write. Nothing had
        been adjusted. A message is the model's to compose only while it is
        not describing a change: what changed is announced by the write
        itself, and what did not is announced here, in Python's words.
        """
        if self.refusals:
            text = refusal_message(self.refusals)
            log.warning("a write was refused this tick; sending the refusal "
                        "rather than the model's message")
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
        decided = getattr(self.guard, "last_check", None) or {}
        # The guard may have kept part of the proposal and dropped the rest.
        write = decided.get("values") or values
        refused = decided.get("refused") or []
        if not allowed:
            # The values go back too: a refusal is the guard's decision, not a
            # failure to propose, and the plan record scores those separately.
            self.refusals.append({"values": values, "reason": why})
            return {"applied": False, "refused_by": "guard",
                    "would_set": values, "reason": why}
        if self.dry_run:
            return {"applied": False, "dry_run": True,
                    "would_set": values, "reason": why}
        live = apply_thresholds(self.cfg, write["mep_start"], write["mep_stop"],
                                write["kub_start"], write["kub_stop"],
                                approval=self.guard.approval())
        applied = thresholds_from_config(live)
        self.guard.note_write(applied)
        # Every executed write tells the owner, in Python, with the values the
        # dashboard read back rather than the ones that were asked for. The
        # 04:10 write on the first live night sent nothing, because the model
        # was trusted to call send_telegram itself and did not.
        seen = getattr(self.guard, "last_seen", None) or {}
        notified = telegram.send(self.cfg, write_message(
            applied, reason, before=seen.get("thresholds"),
            voltage=seen.get("voltage"), refused=refused,
            default_start=self.cfg["default_start"],
            numbers=policymod.numbers_line(self.policy)))
        return {"applied": True, "now": applied, "reason": reason,
                "requested": values, "refused_parts": refused,
                "notified": notified}

    # --- dispatch -----------------------------------------------------------

    def call(self, name, args):
        """Run one tool. Returns a JSON string for the model.

        Every call is journaled with the size of what it returned. Two
        reasons. Tool calls were not in the journal at all, so a tick could
        only be reconstructed from what the model said afterwards. And the
        size is the thing that broke: get_monthly_summary answered with
        17,630 characters, about 4,400 tokens, and on the KAMRUI's integrated
        GPU the answer carrying it went past the 180 s model timeout and the
        owner got "the agent is not answering". A payload has a cost and the
        cost belongs in the log where it can be seen growing.
        """
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
        payload = json.dumps(result, default=str)
        self._journal(name, args, len(payload))
        return payload

    @staticmethod
    def _journal(name, args, size):
        shown = ", ".join(f"{k}={v!r}" for k, v in sorted((args or {}).items()))
        if size > TOOL_RESULT_WARN_CHARS:
            log.warning("tool %s(%s) -> %d chars, over the %d a slow model "
                        "should be asked to read", name, shown, size,
                        TOOL_RESULT_WARN_CHARS)
        else:
            log.info("tool %s(%s) -> %d chars", name, shown, size)
