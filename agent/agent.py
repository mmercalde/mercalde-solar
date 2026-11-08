#!/usr/bin/env python3
"""The agent loop (SPEC section 8).

A tick every 15 minutes, day and night. Python computes the facts; the model
gets them, may call up to 4 tools, and finishes with a recommendation. The
guard decides whether anything is written. The Pi5 executes.

  --dry-run   one tick against the live dashboard, print the plan record and
              what the model would do, write nothing
  --once      one real tick, then exit
"""

import argparse
import json
import logging
import re
import signal
import sqlite3
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta

import requests
from apscheduler.schedulers.background import BackgroundScheduler

import ask_server
import config
import counters
import fuel
import guard as guardmod
import history
import loadmodel
import policy as policymod
import prompts
import sun as sunmod
import telegram
import tools as toolsmod
import topup as topupmod
import usable as usablemod
import weather
from llm import LLM, LLMError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
ANOMALY_COOLDOWN = 1800
# The warm-up question. Short, answerable without a tool, and thrown away: it
# is sent for the prefix it carries and not for the reply.
WARMUP_QUESTION = "Say OK."
WARMUP_MAX_TOKENS = 4
# llama-server may still be loading its weights when the agent starts, so the
# warm-up gets a few tries before it gives up. It is a daemon thread and the
# waits are on stop_event, so none of this delays a shutdown.
WARMUP_ATTEMPTS = 3
WARMUP_RETRY_SECONDS = 20
# Anomalies that are worth saying less often than every half hour. A shunt
# does not drift back by itself, so once the owner has been told, telling
# them again before tomorrow adds nothing.
ANOMALY_COOLDOWNS = {"soc_drift": 24 * 3600}
# How far the Battery Monitor's state of charge may imply more energy above
# the floor than the learned Wh-vs-V curve before the owner hears about it.
SOC_DRIFT_EXCESS = 0.25
POLL_ERROR_JUMP = 10
ARRAY_IMBALANCE_RATIO = 0.30
ARRAY_IMBALANCE_SECONDS = 1800
# The healthy arrays must be making real power before a quiet one means
# anything. At dawn one group faces the sun minutes before the others, so
# 0 W against an average of 188 W is the sun coming up, not a fault: that is
# what fired at 07:31 on the first live day.
ARRAY_MIN_OTHERS_AVG_W = 1000

# The gateway's energy counters are read on their own hour, at :37 — clear of
# the tick, which is a 15-minute interval from process start and so lands on
# every quarter phase over a run of restarts, and clear of the 07:00 and 19:00
# digests. And not at all while the Pi5's own poll is losing reads.
COUNTERS_MINUTE = 37
COUNTERS_QUIET_SECONDS = 120

# Extra direction for the model when it is woken by an anomaly, so it looks
# where the fault can actually be.
ANOMALY_HINTS = {
    "array_": "Diagnose this on the PV side: shading or soiling on that "
              "group, its array breaker or fuses, the string wiring, and the "
              "MPPT's own state and mode. The inverters and the AC side are "
              "not implicated by one array being down, so do not reach for "
              "get_ac_diag.",
}
RECOMMEND_RE = re.compile(r"^\s*recommend:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# Asking for the plan is asking for the plan record. The model paraphrasing it
# is a worse answer than the record itself and can be wrong about it: asked
# "what is tonight's plan" it improvised from the thresholds and left out both
# the 9:48 pm projection and the top-up held until sunset.
PLAN_QUESTION_RE = re.compile(
    r"^(?:what(?:'?s| is| was)\s+)?"
    r"(?:the\s+|your\s+|our\s+)?"
    r"(?:tonight'?s?\s+|today'?s?\s+|current\s+|latest\s+|last\s+)?"
    r"plan"
    r"(?:\s+for\s+(?:tonight|today|the\s+night)|\s+tonight|\s+today)?$"
    # Alexa's slot hands over "what the plan is tonight".
    r"|^what\s+(?:the\s+|your\s+|our\s+)?plan\s+is"
    r"(?:\s+for\s+(?:tonight|today|the\s+night)|\s+tonight|\s+today)?$"
    r"|^/plan$"
    r"|^(?:cual\s+es\s+)?(?:el\s+)?plan"
    r"(?:\s+(?:de\s+|para\s+)?(?:esta\s+noche|hoy))?$",
    re.IGNORECASE)


def is_plan_question(text):
    """Is this a request for the plan record itself? Accents don't matter."""
    plain = "".join(c for c in unicodedata.normalize("NFD", str(text or ""))
                    if not unicodedata.combining(c))
    cleaned = " ".join(plain.strip().strip("?!.¿ ").lower().split())
    return bool(PLAN_QUESTION_RE.match(cleaned))



# "Will a generator run tonight?" on its own, answered by Python from the plan's
# POLICY 4 deficit and its before-sunrise projection. On 2026-11-06 the model
# answered it twice with a crossing the walk had found the next evening. Anchored
# at both ends: "Will a generator run tonight, and what's the shortfall?" asks
# for more than this answers and stays the model's, as an exam case.
_GEN = r"(?:generators?|gen|genny|mep|kubota)"
_WHO = r"(?:a|the|any|either|our)\s+"
_TONIGHT = r"(?:\s+tonight|\s+tonite|\s+overnight|\s+this\s+evening)"
_ES_GEN = r"(?:el|algun|un|ningun)\s+generador"
_ES_VERB = r"(?:arrancar|encender(?:se)?|prender(?:se)?|funcionar|correr|trabajar)"
_ES_FUT = r"(?:arrancara|se\s+encendera|se\s+prendera|funcionara|correra|trabajara)"
_ES_NOCHE = r"(?:\s+esta\s+noche|\s+hoy\s+en\s+la\s+noche)"
GEN_TONIGHT_EN_RE = re.compile(
    rf"^(?:will|does|is|are)\s+{_WHO}{_GEN}\s+"
    rf"(?:(?:be\s+)?(?:going\s+to|gonna)\s+|(?:need\s+to|have\s+to)\s+)?"
    rf"(?:run|start|come\s+on|kick\s+on){_TONIGHT}?$"
    rf"|^(?:is|are)\s+{_WHO}{_GEN}\s+(?:running|starting){_TONIGHT}$"
    rf"|^(?:will|is)\s+there\s+(?:be\s+)?(?:a|any)\s+{_GEN}\s+run"
    rf"(?:\s+expected)?{_TONIGHT}?$"
    rf"|^(?:is|are)\s+(?:a|any)\s+{_GEN}\s+runs?\s+expected{_TONIGHT}?$"
    rf"|^(?:will|do)\s+(?:we|i)\s+need\s+{_WHO}{_GEN}{_TONIGHT}$",
    re.IGNORECASE)
GEN_TONIGHT_ES_RE = re.compile(
    rf"^(?:se\s+)?va\s+a\s+{_ES_VERB}\s+{_ES_GEN}{_ES_NOCHE}?$"
    rf"|^{_ES_GEN}\s+(?:se\s+)?va\s+a\s+{_ES_VERB}{_ES_NOCHE}?$"
    rf"|^{_ES_FUT}\s+{_ES_GEN}{_ES_NOCHE}?$"
    rf"|^{_ES_GEN}\s+{_ES_FUT}{_ES_NOCHE}?$"
    rf"|^(?:hay|habra)\s+que\s+(?:arrancar|prender|encender)\s+"
    rf"(?:el\s+)?generador{_ES_NOCHE}$",
    re.IGNORECASE)


def _plain(text):
    plain = "".join(c for c in unicodedata.normalize("NFD", str(text or ""))
                    if not unicodedata.combining(c))
    return " ".join(plain.strip().strip("?!.¿¡ ").lower().split())


def gen_tonight_language(text):
    """"en" or "es" if this is "will a generator run tonight?", else None."""
    cleaned = _plain(text)
    if GEN_TONIGHT_EN_RE.match(cleaned):
        return "en"
    if GEN_TONIGHT_ES_RE.match(cleaned):
        return "es"
    return None


def is_gen_tonight_question(text):
    return gen_tonight_language(text) is not None


# What Alexa says for the plan. The record runs to 180 words of shorthand, which
# is for reading, so the phone gets that and the voice gets one sentence.
PLAN_SPOKEN = {
    "en": {"tonight": "The pack is projected to reach the {v:.1f} volt start at {when}",
           "not_tonight": "The pack is not projected to reach the {v:.1f} volt start tonight",
           "already": "The pack is already at or below the {v:.1f} volt start",
           "unknown": "I can't say when the pack will reach the {v:.1f} volt start",
           "phone": "; the full plan is on your phone."},
    "es": {"tonight": "Se proyecta que la batería llegue al arranque de {v:.1f} voltios a las {when}",
           "not_tonight": "No se proyecta que la batería llegue al arranque de {v:.1f} voltios esta noche",
           "already": "La batería ya está en el arranque de {v:.1f} voltios o por debajo",
           "unknown": "No puedo decir cuándo llegará la batería al arranque de {v:.1f} voltios",
           "phone": "; el plan completo está en tu teléfono."},
}


def _fmt(v, places=1, dash="?"):
    return dash if v is None else f"{v:.{places}f}"


def _kwh(wh):
    return "?" if wh is None else f"{wh / 1000.0:.1f}"


def _peak_label(facts):
    """"peak today", or the day itself when that is not today.

    Between midnight and sunrise the peak on the page belongs to yesterday's
    sun, and calling it today's told the owner the wrong thing about the
    night they were in.
    """
    day = facts.get("peak_day")
    if not day or day == facts.get("today"):
        return "peak today"
    return f"peak {history.day_label(day)}"


def _month_of(day, now, cfg):
    """The month name a clear-day reference belongs to.

    The day being forecast names it, not the instant the plan is written: on
    the last night of a month those are two different months and the solar
    fit is per month.
    """
    if day:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%b")
    return history.local(now, cfg).strftime("%b")


def _battery_text(facts, lang="en"):
    """"Battery 41% usable (monitor 82%)" for a fact dict.

    A fact dict built before the usable figure existed (a replay, a stored
    record) still has the monitor's SOC, and says only that.
    """
    u = facts.get("usable")
    if not u:
        soc = facts.get("soc")
        u = {"monitor_soc_pct": None if soc is None else round(soc)}
    return usablemod.phrase(u, lang)

class Agent:
    def __init__(self, cfg, dry_run=False, db_path=None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.db_path = db_path or config.DB_PATH
        # Create the schema once here, then let every thread resolve its own
        # connection: scheduler jobs, the Telegram poll and the ask server all
        # run off the main thread, and sqlite3 forbids sharing a connection.
        history.connect(self.db_path).close()
        self.model = loadmodel.LoadModel(self.connection, cfg)
        self.topup = topupmod.TopUp(cfg)
        self.guard = guardmod.Guard(self.connection, cfg, model=self.model,
                                    topup=self.topup)
        self.llm = LLM(cfg)
        self.lock = threading.Lock()          # one model conversation at a time
        self.anomaly_last = {}
        self.array_low_since = {}
        self.last_poll_errors = None
        self.telegram_offset = None
        self.stop_event = threading.Event()

    def connection(self):
        """This thread's database connection."""
        return history.thread_connection(self.db_path)

    @property
    def conn(self):
        return self.connection()

    def tools(self, policy=None):
        return toolsmod.Tools(self.connection, self.cfg, guard=self.guard,
                              dry_run=self.dry_run, policy=policy)

    # --- facts the plan record is built from --------------------------------

    def gather(self, now=None):
        """Everything Python knows, before the model sees anything."""
        now = int(now or time.time())
        data = history.fetch_data(self.cfg)
        live = history.fetch_config(self.cfg)
        # Every tick, not only the first. adopt_live compares what is in
        # force with what this agent last wrote: the same values are its own
        # and nothing happens, different ones were put there by the owner and
        # are adopted as the baseline with the six hour stand-down. Asking
        # once at startup meant an owner who moved the thresholds mid-evening
        # was not noticed until the agent next tried to write - which, on a
        # night where no rule fires, is never.
        self.guard.adopt_live(toolsmod.thresholds_from_config(live), now=now)
        today = history.local_day(now, self.cfg)

        # Before anything derives a run: the exercise window is what the
        # fallback classifier measures against, and the AGS's own schedule
        # beats the manifest's guess at it.
        history.apply_exercise_schedule(self.cfg, data)

        solar_w = sum(data.get(k) or 0 for k in
                      ("mppt80PVPower", "southArrayPVPower", "westArrayPVPower"))
        gen_running = (data.get("mep803aAction") == history.GEN_RUNNING
                       or data.get("kubotaAction") == history.GEN_RUNNING)

        # Solar's peak, not a generator's: the question is whether the day's
        # sun reached 57.0. The day is the one the night in progress is
        # living off - the day of the last sunrise - and not the calendar
        # day, which between midnight and sunrise has had no sun in it yet.
        # At 12:13 am "peak today" was the live voltage of a day that had
        # not started.
        st = sunmod.times(self.cfg, today)
        sun_up = bool(st and st[0] <= now <= st[1])
        peak_day = (history.local_day(now - 86400, self.cfg)
                    if st and now < st[0] else today)
        peak_today = history.solar_peak(self.conn, self.cfg, peak_day, now=now)
        # The live reading counts only while the sun is still able to raise
        # it. A voltage read at 2 am is the night, not the day's peak, and
        # nothing running is not the same as the sun being up.
        if sun_up and not gen_running and (
                peak_today is None
                or (data.get("batteryVoltage") or 0) > peak_today):
            peak_today = data.get("batteryVoltage")

        wx = weather.summary(self.cfg, now=now)
        sunrise_ts = wx.get("next_sunrise_ts")
        sunset_ts = wx.get("sunset_ts")
        remaining_solar_wh = self.model.remaining_solar_wh(now=now)
        hours_to_sunrise = (max(1, int((sunrise_ts - now) / 3600) + 1)
                            if sunrise_ts else 12)
        forecast = self.model.load_forecast(min(hours_to_sunrise, 24), now=now)
        projection = self.model.project_voltage(52.0, now=now)
        deficit = self.model.overnight_deficit(sunrise_ts, now=now)
        drawdown = self.model.overnight_drawdown(now=now)
        overhead = self.model.system_overhead(now=now)
        gate = self.model.learning_status(now=now)
        soc_curve = self.model.soc_curve_status()

        # The forecast that matters is the coming daylight's, which after
        # midnight is today's and not the calendar day after now. weather.py
        # names the day; everything downstream carries the name with the
        # number so no reader has to work out which day "tomorrow" was.
        next_daylight_date = wx.get("next_daylight_date")
        next_daylight_cloud = None
        est_solar = None
        if wx.get("next_daylight"):
            next_daylight_cloud = weather.cloud_of(wx["next_daylight"])
            # The month of the day being estimated, not of this instant: the
            # solar fit is per month and a night at the end of one estimates
            # a day in the next.
            est_solar = self.model.estimate_solar_wh(
                next_daylight_cloud, day=next_daylight_date, now=now)

        # POLICY 4 and 5 ask the load model whether a target is reachable; the
        # windows are the only part of that question the dashboard owns.
        run_window_h = {}
        for gen, cfg_key in (("mep", "mep803a"), ("kubota", "kubota")):
            try:
                run_window_h[gen] = min(live[cfg_key]["maxRuntime"] / 60.0,
                                        self.cfg["ags_max_run_hours"][gen])
            except (KeyError, TypeError):
                run_window_h[gen] = self.cfg["ags_max_run_hours"][gen]

        facts = {
            "now": now, "today": today, "data": data, "config": live,
            "voltage": data.get("batteryVoltage"),
            # Display only, everywhere it appears. What the pack holds is
            # answered by loadmodel's Wh-vs-V curve, learned from what the
            # house actually took out between two voltages, and no rule and
            # no guard check reads this.
            "soc": data.get("battSocBM"),
            # The agent's own amp-hour count on the measured 52.0-59.8 V
            # window, which the owner reads instead of the monitor's 1800 Ah
            # scale. Display only too: no rule and no guard check reads it.
            "usable": self.usable_now(data, now),
            "load_w": None if gen_running else (data.get("acPower1") or 0)
                                              + (data.get("acPower2") or 0),
            "solar_w": solar_w, "gen_running": gen_running,
            "peak_today": peak_today, "peak_day": peak_day,
            "weather": wx, "sunrise_ts": sunrise_ts,
            "sunset_ts": sunset_ts, "remaining_solar_wh": remaining_solar_wh,
            "forecast": forecast, "projection": projection,
            "deficit": deficit,
            "drawdown": drawdown, "overhead": overhead,
            "gate": gate, "soc_curve": soc_curve,
            "next_daylight_cloud": next_daylight_cloud,
            "next_daylight_date": next_daylight_date,
            "next_daylight_label": wx.get("next_daylight_label"),
            # Alias, for one release, so a replay or a stored fact dict built
            # before the rename still reads. Goes with weather.summary's.
            "tomorrow_cloud": next_daylight_cloud,
            "est_solar": est_solar,
            "summary_24h": history.summary(self.conn, 24, now=now,
                                           cfg=self.cfg),
            "thresholds": toolsmod.thresholds_from_config(live),
            "intended": self.guard.intended(),
            "owner_baseline": self.guard.owner_baseline(),
            "baseline": self.guard.baseline(),
            "run_window_h": run_window_h,
            "charge_rates": self.model.charge_rates(now=now),
            # Why each generator says it is running, straight from the AGS,
            # and when each is next due to exercise. Nothing decides on
            # these; they are here so the plan record and the digest can say
            # what is happening instead of inferring it.
            "run_reason": {"mep": data.get("mepOnReason"),
                           "kubota": data.get("kubotaOnReason")},
            "exercise": {g: history.next_exercise(self.conn, g, self.cfg,
                                                  now=now)
                         for g in history.GENS},
        }
        # The state machine sees the tick before the rules do: POLICY 4 asks
        # what state each generator is in, and a generator that started three
        # minutes ago has to be `running` by the time it is asked.
        facts["topup_moves"] = self.topup.advance(
            self.topup_observations(facts), now)
        facts["topup"] = self.topup.snapshot()
        facts["policy"] = policymod.evaluate(self.cfg, facts, self.model)
        return facts

    def topup_observations(self, facts):
        """What the state machine needs to know about each generator now."""
        data, live = facts["data"], facts["config"]
        out = {}
        for gen, cfg_key, act, mode, online in (
                ("mep", "mep803a", "mep803aAction", "mep803aMode", "mepAgsOnline"),
                ("kubota", "kubota", "kubotaAction", "kubotaMode",
                 "kubotaAgsOnline")):
            try:
                cap = min(live[cfg_key]["maxRuntime"],
                          self.cfg["ags_max_run_hours"][gen] * 60)
            except (KeyError, TypeError):
                cap = self.cfg["ags_max_run_hours"][gen] * 60
            out[gen] = {
                "action": data.get(act), "mode": data.get(mode),
                "ags_online": data.get(online),
                "auto_gen_enabled": data.get("autoGenEnabled"),
                "voltage": facts["voltage"],
                "stop_v": (live.get(cfg_key) or {}).get("stopVoltage"),
                "cap_minutes": cap,
                "run": self.latest_run(gen, facts["now"]),
            }
        return out

    def latest_run(self, gen, now):
        """The newest finished run for this generator, as a plain dict."""
        row = self.conn.execute(
            "SELECT * FROM gen_runs WHERE gen=? AND stop_ts IS NOT NULL "
            "ORDER BY stop_ts DESC LIMIT 1", (gen,)).fetchone()
        return dict(row) if row is not None else None

    # --- the plan record ----------------------------------------------------

    def plan_record(self, facts, recommend, applied):
        """The SPEC section 8 record. Python computes every line but the
        recommendation; `applied` is the observed outcome, not the model's
        claim about it."""
        tz = history.tzinfo(self.cfg)
        t = datetime.fromtimestamp(facts["now"], tz)
        lines = []

        load_kw = ("gen running" if facts["load_w"] is None
                   else f"{facts['load_w'] / 1000.0:.1f} kW")
        lines.append(f"{history.stamp(facts['now'], self.cfg)}  "
                     f"V {_fmt(facts['voltage'], 1)}  "
                     f"{_battery_text(facts)}  "
                     f"load {load_kw}")

        peak = facts["peak_today"]
        label = _peak_label(facts)
        thresh = self.cfg["solo_peak_threshold"]
        if peak is None:
            lines.append(f"{label}: ?  (threshold {thresh})")
        elif peak < thresh:
            lines.append(f"{label}: {peak:.1f} V  "
                         f"(threshold {thresh} -> solar shortfall)")
        else:
            lines.append(f"{label}: {peak:.1f} V  (threshold {thresh} -> reached)")

        if facts["drawdown"]:
            d = facts["drawdown"]
            lines.append(f"overnight Wh out of the pack: {d['wh']:,} — from "
                         f"{d.get('source') or 'the load profile'} "
                         f"({d['nights']} night{'' if d['nights'] == 1 else 's'})")
        else:
            lines.append("overnight Wh out of the pack: not learned yet")

        # What the house never receives. Nothing computes with it - the curve
        # and the drawdown are both measured on the pack's side already - but
        # the owner should be able to see it.
        o = facts.get("overhead")
        if o:
            lines.append(f"system overhead: {o['ratio']:.3f}x "
                         f"(pack out ÷ house in, {o['min']:.3f}-{o['max']:.3f} "
                         f"over {o['nights']} night"
                         f"{'' if o['nights'] == 1 else 's'}, {o['source']})")
        else:
            lines.append("system overhead: not learned yet")

        proj = facts["projection"]
        sunrise = (history.clock(facts["sunrise_ts"], self.cfg)
                   if facts["sunrise_ts"] else "?")
        crossing = toolsmod.tonight_crossing(self.cfg, proj, facts["now"])
        why = (proj or {}).get("reason") or ""
        if crossing["daylight"] and not crossing["already"] and (
                crossing["before_sunrise"] or crossing.get("after_sunrise_ts")
                or why.startswith("not reached")):
            # Display only. By day the walk credits none of the sun still to
            # come, so the 9:00 am plan on 2026-11-07 printed "4:26 am" off a
            # day that had 31 kWh of it left. facts["projection"] is untouched:
            # POLICY 3 and 4 read it, and decide exactly as before. A reason
            # the walk could not run at all is still printed below.
            left = facts.get("remaining_solar_wh")
            sun_left = (f"about {_kwh(left)} kWh of sun still to come today"
                        if left is not None else "today's sun still to come")
            lines.append(f"projected 52.0 V at: too early to tell ({sun_left})"
                         f"   sunrise {sunrise}")
        elif proj and proj.get("reached") and crossing["before_sunrise"]:
            lines.append(f"projected 52.0 V at: "
                         f"{self.model.projection_label(proj, facts['now'])}   "
                         f"sunrise {sunrise}")
        elif proj and proj.get("reached"):
            # The walk credits no solar surplus, so a crossing it finds past
            # sunrise ignores the day's recharge and is not a prediction.
            # facts["projection"] itself is untouched; POLICY 3 reads it.
            lines.append(f"projected 52.0 V at: not before sunrise   "
                         f"sunrise {sunrise}")
        else:
            why = (proj or {}).get("reason", "unknown")
            lines.append(f"projected 52.0 V at: not projected ({why})   "
                         f"sunrise {sunrise}")

        # Named, not "tomorrow". The line is read at 6:59 pm and again at
        # 12:13 am, when "tomorrow" means two different days; the one this
        # forecast is about is the day the sun next comes up on.
        cloud, day = policymod.next_daylight(facts)
        if cloud is None:
            lines.append(f"{day}: forecast unavailable")
        elif facts["est_solar"]:
            e = facts["est_solar"]
            month = _month_of(facts.get("next_daylight_date"), facts["now"],
                              self.cfg)
            lines.append(f"{day}: {cloud}% cloud, "
                         f"est. solar {_kwh(e['wh'])} kWh "
                         f"({month} clear-day {_kwh(e['clear_day_wh'])})")
        else:
            lines.append(f"{day}: {cloud}% cloud, est. solar not learned yet")

        # What is running and why, in the AGS's words. A generator turning is
        # the loudest thing on the system and the record used to say only
        # that it was turning; on 2026-09-03 that left "why is it running"
        # to be answered from the voltage, which said 52.0 V about a 59.4 V
        # evening exercise.
        for gen, label in (("mep", "MEP"), ("kubota", "Kubota")):
            reason = (facts.get("run_reason") or {}).get(gen)
            act = facts["data"].get("mep803aAction" if gen == "mep"
                                    else "kubotaAction")
            if act == history.GEN_RUNNING:
                why = (reason.replace("_", " ") if reason
                       else "reason not reported by the AGS")
                lines.append(f"{label} running: {why}")

        due = [(g, e) for g, e in (facts.get("exercise") or {}).items() if e]
        for gen, e in sorted(due):
            if e.get("days_until_due") is None:
                continue
            when = ("overdue" if e["overdue"] else
                    f"in {e['days_until_due']:.0f} d")
            lines.append(f"{gen} exercise: every {e['every_days']} d at "
                         f"{e['at']}, last run {e['last']}, next {e['due']} "
                         f"({when})")

        # Every numeric rule, with its arithmetic shown. The model may not
        # claim "no change" past a rule that fires without overruling it.
        lines += policymod.lines(facts.get("policy") or [])

        lines.append(f"recommend: {recommend}")
        lines.append(f"applied: {applied}")
        return "\n".join(lines)

    def applied_line(self, facts, write_result):
        """What actually happened, in Python's words."""
        if not facts["gate"]["open"]:
            return "no (learning phase)"
        if self.dry_run:
            return "no (dry run)"
        if write_result is None:
            return "no change"
        if write_result.get("applied"):
            n = write_result["now"]
            return (f"yes - MEP {n['mep_start']}/{n['mep_stop']}, "
                    f"Kubota {n['kub_start']}/{n['kub_stop']}")
        return f"no ({write_result.get('reason', 'refused')})"

    # --- talking to the model ----------------------------------------------

    def tick_prompt(self, facts):
        f = facts
        tz = history.tzinfo(self.cfg)
        t = datetime.fromtimestamp(f["now"], tz)
        s = f["summary_24h"]
        th, iv = f["thresholds"], f["intended"]
        wx = f["weather"]
        proj = f["projection"] or {}

        parts = [
            f"Time: {history.stamp(f['now'], self.cfg)} {t.strftime('%Z')} "
            f"({'weekend' if t.weekday() >= 5 else 'weekday'})",
            "",
            "NOW",
            f"  battery {_fmt(f['voltage'], 2)} V, {_battery_text(f)} "
            f"(display only - nothing is decided on either), "
            f"monitor {'online' if f['data'].get('battMonitorOnline') else 'OFFLINE'}",
            f"  solar {f['solar_w']} W, house load "
            f"{'unknown (generator running)' if f['load_w'] is None else str(f['load_w']) + ' W'}",
            f"  MEP {'RUNNING' if f['data'].get('mep803aAction') == history.GEN_RUNNING else 'stopped'}"
            f", AGS {'online' if f['data'].get('mepAgsOnline') else 'OFFLINE'}"
            f"; Kubota {'RUNNING' if f['data'].get('kubotaAction') == history.GEN_RUNNING else 'stopped'}"
            f", AGS {'online' if f['data'].get('kubotaAgsOnline') else 'OFFLINE'}",
            f"  {_peak_label(f)} {_fmt(f['peak_today'], 2)} V",
            "",
            "LIVE THRESHOLDS",
            f"  MEP start {th['mep_start']} stop {th['mep_stop']}"
            f" (max runtime {f['config']['mep803a']['maxRuntime']} min)",
            f"  Kubota start {th['kub_start']} stop {th['kub_stop']}"
            f" (max runtime {f['config']['kubota']['maxRuntime']} min)",
            f"  defaults are {self.cfg['default_start']} / {self.cfg['default_stop']}",
            *([f"  The owner set MEP {ob['mep_start']}/{ob['mep_stop']}, Kubota "
               f"{ob['kub_start']}/{ob['kub_stop']} by hand. Those are the "
               f"baseline and the values to return to, not the config "
               f"defaults. Only a POLICY rule that fires may move them."]
              if (ob := f.get("owner_baseline")) else []),
            "",
            "LAST 24 HOURS",
            f"  voltage min {_fmt(s['min_v'], 2)} max {_fmt(s['max_v'], 2)} "
            f"avg {_fmt(s['avg_v'], 2)}",
            f"  solar {_kwh(s['solar_wh'])} kWh, load {_kwh(s['load_wh'])} kWh",
            f"  generator minutes: MEP {s['gen_minutes'].get('mep', 0)}, "
            f"Kubota {s['gen_minutes'].get('kubota', 0)}",
            "",
            "FORECAST",
        ]
        if f["forecast"]["learned"]:
            parts.append(f"  expected load next {f['forecast']['hours']} h: "
                         f"{f['forecast']['total_wh']:,} Wh")
        else:
            parts.append("  expected load: not learned yet")
        if proj.get("reached"):
            parts.append(f"  pack reaches 52.0 V at "
                         f"{self.model.projection_label(proj, f['now'])} "
                         f"(in {proj.get('hours')} h)")
        else:
            parts.append(f"  pack reaches 52.0 V: not projected "
                         f"({proj.get('reason', 'unknown')})")
        rates = f.get("charge_rates") or {}
        for key, label in (("mep_solo", "MEP alone"), ("kubota_solo", "Kubota alone"),
                           ("both_running", "both together")):
            r = rates.get(key)
            if r:
                parts.append(f"  observed charge rate, {label}: "
                             f"{loadmodel.rate_phrase(r)} over {r['runs']} runs"
                             + (f", {r['excluded_load_spikes']} left out for "
                                f"an exceptional house load"
                                if r.get("excluded_load_spikes") else ""))

        sc = f["soc_curve"]
        if sc.get("soc_at_start_threshold") is not None:
            parts.append(f"  learned: {sc['start_threshold_v']} V is about "
                         f"{sc['soc_at_start_threshold']}% SOC")
        cloud, day = policymod.next_daylight(f)
        if wx.get("next_daylight"):
            parts.append(f"  {day}: {cloud}% daylight cloud, "
                         f"max {wx['next_daylight']['max_temp_c']} C")
        if f["est_solar"]:
            parts.append(f"  estimated solar on {day}: "
                         f"{_kwh(f['est_solar']['wh'])} kWh "
                         f"(clear day {_kwh(f['est_solar']['clear_day_wh'])} kWh)")
        parts.append(f"  sunrise {wx.get('next_sunrise', '?')}, "
                     f"sunset {wx.get('sunset', '?')}")

        rules = f.get("policy") or []
        if rules:
            parts += ["", "POLICY EVALUATION (computed in Python from the facts "
                      "above; the arithmetic is already done, do not redo it)"]
            parts += ["  " + ln for ln in policymod.lines(rules)]
            fired = policymod.firing(rules)
            if fired:
                parts += ["  " + c for c in policymod.call_to_action(fired)]
                parts.append('  Set exactly those four values with '
                             'set_gen_thresholds, or overrule the rule on its '
                             'own line: "overrule POLICY <n>: <reason>". '
                             'Saying "no change" without that line is a policy '
                             'miss. Any other values are also a miss.')
            else:
                parts.append("  No rule fires.")

        gate = f["gate"]
        parts += ["", "STATUS"]
        if gate["open"]:
            parts.append("  Learning gate is open. A write will be applied if the "
                         "guard permits it.")
        else:
            parts.append("  LEARNING PHASE: the guard will refuse every write. Still "
                         "give your recommendation; it will be recorded, not applied.")
        if iv != th:
            parts.append(f"  Note: I last intended MEP {iv['mep_start']}/{iv['mep_stop']}, "
                         f"Kubota {iv['kub_start']}/{iv['kub_stop']}.")
        return "\n".join(parts)

    def run_model(self, system, user, tools, max_rounds=MAX_TOOL_ROUNDS):
        """Run the tool loop. Returns (final_text, write_result)."""
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        write_result = None
        for _ in range(max_rounds):
            msg = self.llm.chat(messages, tools=toolsmod.SCHEMAS)
            calls = LLM.tool_calls(msg)
            if not calls:
                return msg.get("content", ""), write_result
            messages.append(msg)
            for call_id, name, args in calls:
                result = tools.call(name, args)
                if name == "set_gen_thresholds":
                    try:
                        write_result = json.loads(result)
                    except json.JSONDecodeError:
                        write_result = None
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "name": name, "content": result})
        # Out of tool budget: ask for the conclusion with no tools offered.
        messages.append({"role": "user",
                         "content": "Tool budget spent. Give your final answer now."})
        msg = self.llm.chat(messages)
        return msg.get("content", ""), write_result

    @staticmethod
    def extract_recommendation(text):
        m = RECOMMEND_RE.search(text or "")
        if m:
            return m.group(1).strip()
        # The model finished without the contracted line; keep its own words.
        cleaned = " ".join((text or "").split())
        return cleaned[:200] if cleaned else "no recommendation returned"

    # --- the tick -----------------------------------------------------------

    def tick(self, now=None):
        with self.lock:
            return self._tick(now)

    def _tick(self, now=None):
        started = time.time()
        try:
            facts = self.gather(now)
        except requests.RequestException as e:
            log.error("tick aborted, dashboard unreachable: %s", e)
            return None

        # Keep the stores current before the model reads them.
        try:
            history.record_sample(self.conn, facts["data"], ts=facts["now"])
            history.derive_gen_runs(self.conn, self.cfg)
            history.rollup_hourly(self.conn, self.cfg)
            history.rollup_daily(self.conn, self.cfg, days=[facts["today"]])
        except Exception as e:                       # noqa: BLE001
            log.warning("store update failed, continuing: %s", e)
        self.update_usable()

        # What the state machine owes the owner, before anything else is said
        # about the tick.
        try:
            self.notify_topup(facts)
        except Exception as e:                       # noqa: BLE001
            log.warning("could not send a top-up notice: %s", e)

        # Python's own writes, before the model is asked anything: a start the
        # agent raised comes back the moment the generator is confirmed
        # running, and a stop it lowered comes back when the night that
        # justified it is over.
        try:
            if self.return_raised_starts(facts):
                facts["config"] = history.fetch_config(self.cfg)
                facts["thresholds"] = toolsmod.thresholds_from_config(facts["config"])
        except Exception as e:                       # noqa: BLE001
            log.warning("returning a raised start failed: %s", e)
        try:
            if self.return_lowered_stops(facts):
                facts["config"] = history.fetch_config(self.cfg)
                facts["thresholds"] = toolsmod.thresholds_from_config(facts["config"])
        except Exception as e:                       # noqa: BLE001
            log.warning("returning a lowered stop failed: %s", e)

        tools = self.tools(policy=facts["policy"])
        prompt = self.tick_prompt(facts)
        try:
            text, write_result = self.run_model(
                prompts.system_prompt(), prompt, tools)
        except LLMError as e:
            log.error("model unavailable: %s", e)
            text, write_result = "", None

        recommend = self.extract_recommendation(text)
        applied = self.applied_line(facts, write_result)
        record = self.plan_record(facts, recommend, applied)

        # A rule that fired and was neither set nor overruled is the failure
        # mode of the first live night, so it is recorded as one.
        missed = policymod.misses(facts["policy"], text, write_result)
        if missed:
            self.guard.record_policy_miss(missed, recommend, facts["voltage"],
                                          facts["soc"], now=facts["now"])

        history.record_plan(self.conn, record, {
            "voltage": facts["voltage"], "soc": facts["soc"],
            "usable_pct": (facts.get("usable") or {}).get("usable_pct"),
            "usable_uncertain": (facts.get("usable") or {}).get("uncertain"),
            "peak_today": facts["peak_today"],
            "peak_day": facts.get("peak_day"),
            # Which day's forecast this plan acted on. "tomorrow" in a stored
            # record could not be resolved back to a date once the night had
            # rolled over; the date can.
            "next_daylight_date": facts.get("next_daylight_date"),
            "next_daylight_cloud": facts.get("next_daylight_cloud"),
            "thresholds": facts["thresholds"],
            "gate_open": facts["gate"]["open"],
            "projection": facts["projection"],
            # POLICY 4's deficit to sunrise, so "will a generator run
            # tonight?" is answered from the number the rule decided on.
            "deficit": facts.get("deficit"),
            "soc_curve": facts["soc_curve"],
            "policy": facts["policy"],
            "policy_misses": [r["rule"] for r in missed],
            "write": write_result,
            "dry_run": self.dry_run,
            # What the model was actually asked, and what it actually said.
            # model_eval.py replays these against a candidate endpoint; a
            # replay of a reconstructed prompt would score a model on a
            # question nobody put to it. Pruned after eval_retention_days.
            "prompt": prompt,
            "answer": text,
        }, ts=facts["now"])

        log.info("tick done in %.1fs; %s", time.time() - started, applied)
        return record

    def return_raised_starts(self, facts):
        """Put a start the agent raised back to the owner's baseline.

        Python's write, not the model's. Once the Pi5 has actually started the
        generator, the start threshold has done its job and the Pi5 ignores
        changes to it mid-run, so putting it back at that moment is free and
        leaves no window where a raised start could start something the agent
        did not ask for. Last night's 53.3 stood from 1:36 am until the Pi5
        restarted the Kubota on it at 4:02.

        If the running tick is missed - the agent restarts, the dashboard is
        unreachable - the run having ended is the second chance.
        """
        raised = self.guard.raised_starts()
        if not raised or self.dry_run:
            return []
        data, live = facts["data"], facts["config"]
        base = self.guard.baseline()
        want = dict(toolsmod.thresholds_from_config(live))
        done, why = [], {}
        for gen, note in raised.items():
            skey = "mep_start" if gen == "mep" else "kub_start"
            action = data.get("mep803aAction" if gen == "mep" else "kubotaAction")
            running = action == history.GEN_RUNNING
            ended = self._ran_since(gen, note.get("since", 0), facts["now"])
            # Every state past `requested` is a state in which the raised
            # start has done all it will ever do: the generator is running,
            # the run is over, the owner took it, or it never started. The
            # start goes back on any of them.
            state = self.topup.status(gen)
            spent = state in (topupmod.RUNNING, topupmod.DONE,
                              topupmod.STOPPED_BY_OWNER,
                              topupmod.FAILED_TO_START)
            if not running and not ended and not spent:
                continue
            if want[skey] <= base[skey] + guardmod.EPS:
                self.guard.clear_raised(gen)     # already back, nothing to write
                continue
            # Lowering only. A start that comes back never goes up on the
            # way, whatever the baseline says.
            back_to = max(base[skey], guardmod.HARD_START_FLOOR)
            if back_to > want[skey] + guardmod.EPS:
                self.guard.clear_raised(gen)
                continue
            want[skey] = back_to
            done.append(gen)
            why[gen] = ("is running" if running else
                        "never started" if state == topupmod.FAILED_TO_START
                        else "has finished its run")
        if not done:
            return []

        reason = ("; ".join(f"{g} {why[g]}, so its start returns to the owner's "
                            f"{base['mep_start' if g == 'mep' else 'kub_start']}"
                            for g in done))
        allowed, refusal = self.guard.check(reason=reason, now=facts["now"],
                                            status={"data": data, "config": live},
                                            **want)
        if not allowed:
            log.warning("could not return the raised start: %s", refusal)
            return []
        try:
            applied = toolsmod.thresholds_from_config(
                toolsmod.apply_thresholds(self.cfg, want["mep_start"],
                                          want["mep_stop"], want["kub_start"],
                                          want["kub_stop"],
                                          approval=self.guard.approval()))
        except requests.RequestException as e:
            log.warning("returning the raised start failed: %s", e)
            return []
        self.guard.note_write(applied, now=facts["now"], housekeeping=True)
        for gen in done:
            self.guard.clear_raised(gen)
        telegram.send(self.cfg, toolsmod.write_message(
            applied, reason, before=toolsmod.thresholds_from_config(live),
            voltage=facts["voltage"], default_start=self.cfg["default_start"]))
        log.info("returned raised start(s) %s to the baseline", ", ".join(done))
        return done

    def notify_topup(self, facts):
        """One Telegram per generator that stopped being the agent's tonight.

        Driven by the state, not by the transition that produced it: whichever
        call to gather() moved the machine, the message is still owed and is
        still sent exactly once.
        """
        sent = []
        for gen in self.topup.pending_notices():
            entry = self.topup.entry(gen)
            skey = "mep_start" if gen == "mep" else "kub_start"
            base = self.guard.baseline()[skey]
            if entry["state"] == topupmod.FAILED_TO_START:
                other = next((g for g in topupmod.GENS if g != gen
                              and self.topup.status(g) == topupmod.IDLE), None)
                text = topupmod.failed_to_start_message(gen, entry, base, other)
            else:
                text = topupmod.stopped_by_owner_message(gen, entry)
            if self.dry_run:
                log.info("would tell the owner: %s", text)
            else:
                telegram.send(self.cfg, telegram.escape(text))
            self.topup.mark_notified(gen)
            sent.append(gen)
        return sent

    def predawn_reason_passed(self, facts):
        """Why a lowered stop is no longer justified, or None.

        POLICY 3's pre-dawn case drops both stops so the morning's solar can
        finish a charge that lands just before a clear sunrise. That reason
        lasts exactly one night. It has passed when the sun is up - whatever
        happened, the night it was set for is over - and before that if the
        crossing it was set for stops being projected at all.

        A rule that is only "not firing" because the stops are already where
        it wants them has not stopped meaning it, so `satisfied` is checked:
        without it the stop would go back, the rule would fire again, and the
        two would take turns all night.
        """
        day = sunmod.daylight(self.cfg, facts["now"])
        if day:
            return (f"the sun came up at {history.clock(day[0], self.cfg)} and "
                    f"the night it was set for is over")
        rule = next((r for r in (facts.get("policy") or [])
                     if r.get("rule") == 3 and "pre-dawn" in r.get("name", "")),
                    None)
        if (rule is not None and not rule["fires"] and not rule.get("held")
                and not rule.get("satisfied")):
            return f"the pre-dawn case no longer holds: {rule['detail']}"
        return None

    def return_lowered_stops(self, facts):
        """Put a stop the agent lowered back to the owner's baseline.

        The same housekeeping as a raised start, on the other threshold and
        in the other direction. On 2026-09-01 the pre-dawn case fired at
        10:36 am and dropped both stops to 54.5; nothing put them back, and
        they were still there that evening.
        """
        if self.dry_run:
            return []
        data, live = facts["data"], facts["config"]
        base = self.guard.baseline()
        want = dict(toolsmod.thresholds_from_config(live))
        # The ledger, plus anything actually sitting below the owner's
        # baseline. A stop can be down there without a ledger entry - it was
        # written before this bookkeeping existed - and it is no more
        # justified for that. Their own values are never below their own
        # baseline, because adopting them is what makes them the baseline.
        lowered = set(self.guard.lowered_stops())
        for gen in ("mep", "kubota"):
            pkey = "mep_stop" if gen == "mep" else "kub_stop"
            if want[pkey] < base[pkey] - guardmod.EPS:
                lowered.add(gen)
        if not lowered:
            return []
        why = self.predawn_reason_passed(facts)
        if not why:
            return []
        done = []
        for gen in sorted(lowered):
            pkey = "mep_stop" if gen == "mep" else "kub_stop"
            # Raising only. A stop that comes back never goes down on the way.
            back_to = min(base[pkey], guardmod.HARD_STOP_CEILING)
            if back_to <= want[pkey] + guardmod.EPS:
                self.guard.clear_lowered(gen)     # already back, nothing to write
                continue
            want[pkey] = back_to
            done.append(gen)
        if not done:
            return []

        reason = (f"{' and '.join(done)} stop returns to the owner's "
                  f"{base['mep_stop' if done[0] == 'mep' else 'kub_stop']}: "
                  f"{why}")
        allowed, refusal = self.guard.check(reason=reason, now=facts["now"],
                                            status={"data": data, "config": live},
                                            **want)
        if not allowed:
            log.warning("could not return the lowered stop: %s", refusal)
            return []
        try:
            applied = toolsmod.thresholds_from_config(
                toolsmod.apply_thresholds(self.cfg, want["mep_start"],
                                          want["mep_stop"], want["kub_start"],
                                          want["kub_stop"],
                                          approval=self.guard.approval()))
        except requests.RequestException as e:
            log.warning("returning the lowered stop failed: %s", e)
            return []
        self.guard.note_write(applied, now=facts["now"], housekeeping=True)
        for gen in done:
            self.guard.clear_lowered(gen)
        telegram.send(self.cfg, toolsmod.write_message(
            applied, reason, before=toolsmod.thresholds_from_config(live),
            voltage=facts["voltage"], default_start=self.cfg["default_start"]))
        log.info("returned lowered stop(s) %s to the baseline", ", ".join(done))
        return done

    def _ran_since(self, gen, since, now):
        row = self.conn.execute(
            "SELECT 1 FROM gen_runs WHERE gen=? AND stop_ts IS NOT NULL "
            "AND stop_ts >= ? AND stop_ts <= ? LIMIT 1",
            (gen, since, now)).fetchone()
        return row is not None

    # --- digests ------------------------------------------------------------

    def digest(self, evening):
        """19:00 carries tonight's plan; 07:00 scores last night's projection."""
        facts = self.gather()
        tz = history.tzinfo(self.cfg)
        now = facts["now"]
        head = "Evening plan" if evening else "Overnight report"
        lines = [f"<b>{head}</b>",
                 f"V {_fmt(facts['voltage'], 2)}  {_battery_text(facts)}  "
                 f"{_peak_label(facts)} {_fmt(facts['peak_today'], 2)} V"]

        if evening:
            plan = history.latest_plan(self.conn)
            if plan:
                lines += ["", telegram.escape(plan["text"])]
        else:
            since = now - 16 * 3600
            source_ts, predicted = self.reference_projection(now)
            low = self.conn.execute(
                "SELECT MIN(battery_v) v, ts FROM samples WHERE ts >= ? "
                "ORDER BY battery_v LIMIT 1", (since,)).fetchone()
            if predicted:
                at = history.clock(source_ts, self.cfg)
                lines.append(f"predicted 52.0 V "
                             f"{self._crossing_label(predicted, source_ts)}"
                             f"  (from the {at} plan)")
            else:
                lines.append("no 52.0 V projection was made last night")
            if low and low["v"] is not None:
                at = history.clock(low["ts"], self.cfg)
                lines.append(f"actual low {low['v']:.2f} V at {at}")
            runs = history.gen_runs(self.conn, 1, now=now)
            if runs:
                for r in runs:
                    start = history.clock(r["start_ts"], self.cfg)
                    line = (f"{r['gen']} ran {start} for "
                            f"{r['duration_min']:.0f} min "
                            f"({_fmt(r['start_v'], 1)} -> {_fmt(r['stop_v'], 1)} V)")
                    # Only when the run has a fuel figure. A run whose gross
                    # was never measured says nothing here rather than nought.
                    burned = fuel.phrase(self.cfg, r["fuel_gal"])
                    if burned:
                        line += f", {burned}"
                    lines.append(line)
            else:
                lines.append("no generator runs overnight")

        if not facts["gate"]["open"]:
            lines.append("")
            lines.append("<i>Learning phase: no thresholds are being written.</i>")
        text = "\n".join(lines)
        if self.dry_run:
            print(text)
            return text
        telegram.send(self.cfg, text)
        return text

    def _crossing_label(self, projection, source_ts):
        """How the overnight report writes the projected 52.0 V crossing.

        A bare clock time reads as last night. It is only last night if the
        crossing fell in the overnight window - between the plan that made it
        and the sunrise that ended that night. A plan can project past that
        sunrise, into the following evening, and printing "9:10 pm" for it
        told the owner the pack had crossed while they slept when it had not
        crossed at all. Past the sunrise, say so, and carry the date so the
        time cannot be read as the wrong night.
        """
        c = toolsmod.tonight_crossing(self.cfg, projection, source_ts)
        if c["before_sunrise"] or not c.get("after_sunrise_ts"):
            return f"at {self.model.projection_label(projection, source_ts)}"
        return (f"not before sunrise (next crossing "
                f"{toolsmod.dated_clock(c['after_sunrise_ts'], self.cfg)})")

    def reference_projection(self, now):
        """(tick ts, projection) the overnight report is scored against.

        Not the first projection of the night. That one is made around 21:00
        with the MEP still cooling and the pack still settling, and on the
        first live night it read 01:34 against an actual low of 04:55 - the
        least informed number available. The evening digest's own tick is the
        one the owner was shown at 19:00, so that is the one to score; failing
        that, the last tick before midnight.
        """
        tz = history.tzinfo(self.cfg)
        midnight = int(datetime.fromtimestamp(now, tz)
                       .replace(hour=0, minute=0, second=0, microsecond=0)
                       .timestamp())
        evenings = [h for h in self.cfg["digest_hours"] if h >= 12]
        made = []
        for row in self.conn.execute(
                "SELECT ts, data FROM plans WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (now - 16 * 3600, now)):
            try:
                d = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                continue
            p = d.get("projection") or {}
            if p.get("reached"):
                made.append((row["ts"], p))
        if not made:
            return None, None
        if evenings:
            # The first tick of the digest hour is the one the digest quoted.
            digest_hour = [m for m in made
                           if history.local(m[0], self.cfg).hour == evenings[-1]]
            if digest_hour:
                return digest_hour[0]
        before_midnight = [m for m in made if m[0] < midnight]
        return before_midnight[-1] if before_midnight else made[-1]

    # --- inbound questions --------------------------------------------------

    # --- "will a generator run tonight?" -------------------------------------

    def _topup_window(self, now):
        """(when the night's top-up window opens, whether it has opened).

        After midnight the night in progress opened the evening before, and
        so did its window: the decision was made then.
        """
        today = history.local_day(now, self.cfg)
        st = sunmod.times(self.cfg, today)
        ref = now
        if st and now < st[0]:
            ref = now - 86400
            st = sunmod.times(self.cfg, history.local_day(ref, self.cfg))
        opens = policymod.window_opens(
            self.cfg, {"now": ref, "sunset_ts": st[1] if st else None})
        return opens, bool(opens and now >= opens)

    def _topup_decision(self, opens, now, lang):
        """(clause, a run is under way or asked for) for a window that has opened.

        The state machine first: it is what the rule did. With every generator
        still idle, the rule-4 line of the last plan made in the window says
        why it did nothing.
        """
        es = lang == "es"
        at = history.clock(opens, self.cfg)
        snap = self.topup.snapshot()
        gens = (snap.get("gens") or {}) if (
            snap.get("night") == self.topup.night_key(now)) else {}
        clauses, active = [], False
        for gen in topupmod.GENS:
            e = gens.get(gen) or {}
            state, name = e.get("state", topupmod.IDLE), topupmod._pretty(gen)
            if state == topupmod.IDLE:
                continue
            when = history.clock(e.get("requested_at") or e.get("since") or now,
                                 self.cfg)
            stop = f"{e['stop']:.1f} V" if e.get("stop") is not None else "?"
            if state == topupmod.REQUESTED:
                active = True
                clauses.append(
                    f"la regla de recarga subió el arranque del {name} a las "
                    f"{when} para cargar hasta {stop}" if es else
                    f"the top-up rule raised the {name}'s start at {when} for a "
                    f"run to {stop}")
            elif state == topupmod.RUNNING:
                active = True
                clauses.append(
                    f"el {name} está funcionando en la recarga de esta noche, "
                    f"hasta {stop}" if es else
                    f"the {name} is running on tonight's top-up, to {stop}")
            elif state == topupmod.DONE:
                clauses.append(
                    f"la regla de recarga ya hizo funcionar el {name} esta "
                    f"noche y terminó a las "
                    f"{history.clock(e.get('since') or now, self.cfg)}" if es else
                    f"the top-up rule already ran the {name} tonight: "
                    f"{e.get('why', 'it finished')}")
            elif state == topupmod.STOPPED_BY_OWNER:
                clauses.append(
                    f"la recarga de esta noche con el {name} quedó en manos del "
                    f"dueño" if es else
                    f"tonight's top-up with the {name} ended as the owner's: "
                    f"{e.get('why', '')}".rstrip(": "))
            elif state == topupmod.FAILED_TO_START:
                clauses.append(
                    f"la regla de recarga pidió arrancar el {name} a las {when} "
                    f"y no arrancó" if es else
                    f"the top-up rule asked the {name} to start at {when} and "
                    f"it did not start")
        if clauses:
            return "; ".join(clauses), active

        rows = self.conn.execute(
            "SELECT ts, text, data FROM plans WHERE ts >= ? AND ts <= ? "
            "ORDER BY ts DESC", (opens, now)).fetchall()
        for row in rows:
            try:
                d = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                continue
            r4 = next((r for r in d.get("policy") or [] if r.get("rule") == 4),
                      None)
            if not r4 or (r4.get("held") and "held until" in r4.get("detail", "")):
                continue
            t = history.clock(row["ts"], self.cfg)
            if r4.get("fires"):
                applied = next((ln[len("applied: "):] for ln in
                                (row["text"] or "").splitlines()
                                if ln.startswith("applied: ")), "?")
                return ((f"a las {t} la regla de recarga pidió una recarga, "
                         f"pero no se aplicó ({applied})" if es else
                         f"at {t} the top-up rule called for a run, but it was "
                         f"not applied ({applied})"), False)
            deficit = (d.get("deficit") or {}).get("deficit_wh")
            if not es:
                return (f"at {t} the top-up rule decided no run: "
                        f"{r4.get('detail', 'no reason recorded')}"), False
            if deficit is not None and deficit <= 0:
                why = (f": a la batería le sobran {_kwh(-deficit)} kWh para "
                       f"la noche")
            elif deficit is not None:
                why = (f": el déficit de {_kwh(deficit)} kWh es menor que los "
                       f"{_kwh(self.cfg['min_topup_wh'])} kWh que justifican "
                       f"una recarga")
            else:
                why = ""
            return f"a las {t} la regla de recarga decidió no arrancar{why}", False
        return ((f"la regla de recarga no se ha evaluado desde las {at}" if es
                 else f"the top-up rule has not been evaluated since {at}"),
                False)

    def gen_tonight_answer(self, lang=None, now=None):
        """"Will a generator run tonight?", answered by Python.

        From the latest plan: its before-sunrise 52.0 V projection, its POLICY
        4 deficit to sunrise, and, once the window has opened, what the top-up
        rule did with them. No model call; every figure is one the plan holds.
        """
        es = lang == "es"
        now = int(now or time.time())
        snap = self.latest_plan_json()
        if not snap.get("ts"):
            return ("Aún no hay un plan, así que no puedo decirlo." if es else
                    "No plan has been recorded yet, so I can't say.")
        data = snap.get("data") or {}
        c = toolsmod.tonight_crossing(self.cfg, data.get("projection"),
                                      snap["ts"])
        d = data.get("deficit")
        if not d:
            # A plan written before the deficit was stored with it: the same
            # call POLICY 4 makes, asked now.
            d = self.model.overnight_deficit(
                sunmod.next_sunrise(self.cfg, now=now), now=now)
        deficit = d.get("deficit_wh")
        floor_v = d.get("floor_v", 52.0)
        sunrise = (history.clock(c["sunrise_ts"], self.cfg)
                   if c["sunrise_ts"] else "?")
        opens, decided = self._topup_window(now)
        if decided:
            decision, active = self._topup_decision(opens, now, lang)
        else:
            active = False
            decision = (f"la regla de recarga decide a las "
                        f"{history.clock(opens, self.cfg)}" if es else
                        f"the top-up rule decides at "
                        f"{history.clock(opens, self.cfg)}") if opens else ""

        if deficit is None:
            energy = ""
        elif deficit <= 0:
            energy = (f"guarda {_kwh(d.get('available_wh'))} kWh por encima de "
                      f"{floor_v:.1f} V y se espera que salgan unos "
                      f"{_kwh(d.get('needed_wh'))} kWh antes del amanecer, así "
                      f"que aguanta hasta el amanecer con {_kwh(-deficit)} kWh "
                      f"de sobra" if es else
                      f"it holds {_kwh(d.get('available_wh'))} kWh above "
                      f"{floor_v:.1f} V against about {_kwh(d.get('needed_wh'))} "
                      f"kWh expected out of it by sunrise, so the pack holds "
                      f"through sunrise with {_kwh(-deficit)} kWh to spare")
        else:
            energy = (f"le faltan {_kwh(deficit)} kWh por encima de "
                      f"{floor_v:.1f} V para llegar al amanecer" if es else
                      f"it is {_kwh(deficit)} kWh short of reaching sunrise "
                      f"above {floor_v:.1f} V")
        short = deficit is not None and deficit >= self.cfg["min_topup_wh"]

        if c["already"]:
            lead = ("Sí: la batería ya está en el arranque de 52.0 V o por "
                    "debajo" if es else
                    "Yes: the pack is already at or below the 52.0 V start")
        elif c["daylight"] and not decided and not active:
            # The same discipline POLICY 3 and 4 keep: with the sun still up
            # neither the crossing nor the deficit is settled. No shortfall is
            # quoted either: it counts tonight's drain and none of today's
            # charging, and at 7:16 am on 2026-11-07 it said "16.4 kWh short"
            # on a clear 33 kWh day. The figure is the sun still to come today,
            # which before dawn's first hour is the whole day's forecast.
            try:
                left = self.model.remaining_solar_wh(now=now)
            except Exception:                        # noqa: BLE001
                left = None
            if left is not None:
                sun_part = (f"el pronóstico de hoy es de unos {_kwh(left)} kWh "
                            f"de sol, que entran primero a la batería" if es else
                            f"today's forecast is about {_kwh(left)} kWh of sun, "
                            f"which goes into the pack first")
            else:
                sun_part = ("el sol de hoy entra primero a la batería" if es
                            else "today's sun goes into the pack first")
            decides = ""
            if opens:
                decides = (f"; la regla de recarga decide a las "
                           f"{history.clock(opens, self.cfg)}" if es else
                           f"; the top-up rule decides at "
                           f"{history.clock(opens, self.cfg)}")
            text = (("Es muy pronto para saberlo: " if es else
                     "Too early to tell: ") + sun_part + decides + ".")
            plan_at = history.clock(snap["ts"], self.cfg)
            return text + (f" (Del plan de las {plan_at}.)" if es else
                           f" (From the {plan_at} plan.)")
        elif c["before_sunrise"]:
            lead = (f"Sí, se espera que funcione un generador esta noche: se "
                    f"proyecta que la batería llegue al arranque de 52.0 V a "
                    f"las {c['at']}, antes del amanecer a las {sunrise}" if es else
                    f"Yes, a generator run is expected tonight: the pack is "
                    f"projected to reach the 52.0 V start at {c['at']}, before "
                    f"sunrise at {sunrise}")
        elif active:
            lead = ("Sí, hay una recarga con generador esta noche" if es else
                    "Yes, a generator run is on tonight for the top-up")
        elif not c["reason"].startswith("not reached"):
            lead = (f"No puedo decir si funcionará un generador esta noche: no "
                    f"se sabe cuándo llegará la batería a 52.0 V "
                    f"({c['reason']})" if es else
                    f"I can't say whether a generator will run tonight: when "
                    f"the pack reaches 52.0 V is not known ({c['reason']})")
        elif short and not decided:
            lead = (f"Es probable una recarga esta noche: no se proyecta que la "
                    f"batería llegue al arranque de 52.0 V antes del amanecer a "
                    f"las {sunrise}, pero" if es else
                    f"A top-up run is likely tonight: the pack is not projected "
                    f"to reach the 52.0 V start before sunrise at {sunrise}, but")
        else:
            lead = (f"No se espera que funcione ningún generador esta noche: no "
                    f"se proyecta que la batería llegue al arranque de 52.0 V "
                    f"antes del amanecer a las {sunrise}" if es else
                    f"No generator run is expected tonight: the pack is not "
                    f"projected to reach the 52.0 V start before sunrise at "
                    f"{sunrise}")
        sep = " " if lead.endswith("but") or lead.endswith("pero") else ", y " \
            if es else ", and "
        text = lead + (sep + energy if energy else "") + "."
        if decision:
            text += " " + decision[0].upper() + decision[1:] + "."
        plan_at = history.clock(snap["ts"], self.cfg)
        text += (f" (Del plan de las {plan_at}.)" if es else
                 f" (From the {plan_at} plan.)")
        return text

    def plan_brief(self, text, lang=None):
        """(one spoken sentence, the full record) for a plan question, or None.

        For a caller that speaks: POST /ask with deliver=telegram says the
        sentence and sends the record. None for any other question, and before
        the first plan, so answer() deals with those as it always has. Every
        word is Python's, with figures from the plan the record is.
        """
        if not is_plan_question(text):
            return None
        snap = self.latest_plan_json()
        if not (snap.get("ts") and snap.get("text")):
            return None
        words = PLAN_SPOKEN.get(lang) or PLAN_SPOKEN["en"]
        proj = (snap.get("data") or {}).get("projection") or {}
        start = (snap.get("intended") or {}).get("mep_start", self.cfg["default_start"])
        # Judged against the sunrise after the plan was made, the same check
        # the Q&A and the overnight report use.
        c = toolsmod.tonight_crossing(self.cfg, proj, snap["ts"])
        when = c["at"] if c["before_sunrise"] else ""
        if c["already"]:
            kind = "already"
        elif c["before_sunrise"]:
            kind = "tonight"
        else:
            kind = ("not_tonight" if c["reason"].startswith("not reached")
                    else "unknown")
        return (words[kind].format(v=start, when=when) + words["phone"], snap["text"])

    def answer(self, text, lang=None):
        """Shared by Telegram inbound and POST /ask.

        An answer must be grounded in a tool result. The model will sometimes
        answer a question about live state without calling anything, and then
        it invents the number — which POLICY 9 forbids. So a reply produced
        with no tool call is retried once, and if the model still will not
        look, Python answers from /data instead of letting a made-up voltage
        reach the owner or Alexa.
        """
        # Both the Telegram inbound loop and POST /ask arrive here, so this
        # covers each of them.
        if is_plan_question(text):
            plan = history.latest_plan(self.conn)
            return plan["text"] if plan else "No plan has been recorded yet."
        gen_lang = gen_tonight_language(text)
        if gen_lang:
            # The question's own language: /ask says "en" when it is not told.
            return self.gen_tonight_answer(gen_lang)
        with self.lock:
            tools = self.tools()
            try:
                now_text = history.stamp(int(time.time()), self.cfg)
                context = self.plan_context()
                system = prompts.ask_prompt()
                user = prompts.ask_user(text, lang, now_text, context)
                reply, _ = self.run_model(system, user, tools,
                                          max_rounds=MAX_TOOL_ROUNDS)
                if not tools.calls:
                    log.warning("answer was ungrounded; retrying with a nudge")
                    reply, _ = self.run_model(
                        system,
                        user + "\n\n(You have not called a tool yet. Call the "
                               "tool that answers this before replying.)",
                        tools)
                if not tools.calls:
                    log.warning("model would not call a tool; answering from /data")
                    return self.status_sentence(lang)
            except LLMError as e:
                log.error("model unavailable for question: %s", e)
                return ask_server.FALLBACK.get(lang or "en")
        reply = (reply or "").strip()
        if not reply:
            return ask_server.FALLBACK.get(lang or "en")
        return toolsmod.with_not_final(reply, tools.calls, lang)

    # Ceiling for the injected plan block, in characters. It rides in the
    # uncacheable tail of every question, so it pays full prefill each time;
    # at this size that is a fraction of a second, not the 70 s cold penalty.
    PLAN_CONTEXT_MAX = 1500

    def plan_context(self):
        """Compact authoritative snapshot for the ask prompt.

        Today's plan, the thresholds in force with their provenance, and the
        last few threshold actions with reasons. Built from the same
        latest_plan_json() the Pi5 watchdog reads, so the model is told the
        same story the dashboard shows. Returns None if nothing useful can be
        read - the question then runs exactly as before this existed.
        """
        try:
            snap = self.latest_plan_json()
        except Exception as e:                       # noqa: BLE001
            log.warning("could not build plan context: %s", e)
            return None
        lines = []
        if snap.get("ts") and snap.get("text"):
            plan_text = " ".join(str(snap["text"]).split())
            if len(plan_text) > 600:
                plan_text = plan_text[:600] + " ..."
            lines.append(f"Plan recorded {history.stamp(snap['ts'], self.cfg)}: "
                         f"{plan_text}")
        # The plan text is the agent's shorthand, and Qwen3-8B, live then, misread it on
        # the first afternoon this block existed: every rule said "held" and
        # the recommendation said "no change", so it announced no run and no
        # shortfall tonight - while "projected 52.0 V at 10:13 pm" sat two
        # lines up. The verdict is therefore also stated in words, derived
        # from the same projection the plan printed.
        # A crossing counts as tonight only before the sunrise after the plan:
        # on 2026-11-06 this sentence called one found at 6:47 pm the next
        # evening "IS expected tonight". So the sentence is now the same one
        # Python gives for "will a generator run tonight?", from the same
        # projection and deficit.
        intended = snap.get("intended") or {}
        verdict = None
        if snap.get("ts"):
            try:
                verdict = self.gen_tonight_answer()
            except Exception as e:                   # noqa: BLE001
                log.warning("could not say whether a run is expected: %s", e)
        if verdict:
            lines.append("In plain terms: " + verdict)
            proj = (snap.get("data") or {}).get("projection")
            c = toolsmod.tonight_crossing(self.cfg, proj, snap["ts"])
            if c["before_sunrise"] and not c["daylight"]:
                lines.append(
                    "A policy shown as 'held' is waiting for its window, not "
                    "cancelled; 'recommend: no change' means the thresholds "
                    "are already set right, not that no run is coming.")
        if intended:
            lines.append(
                "Thresholds in force: MEP start {mep_start:.1f} stop "
                "{mep_stop:.1f} V, Kubota start {kub_start:.1f} stop "
                "{kub_stop:.1f} V.".format(**intended))
        baseline = snap.get("baseline") or {}
        if baseline:
            who = ("set by the owner" if snap.get("owner_baseline")
                   else "the config defaults")
            lines.append(
                "Baseline they return to ({}): MEP {:.1f}/{:.1f} V, Kubota "
                "{:.1f}/{:.1f} V. Any difference from the baseline is a "
                "change the agent made, for the reason logged below - it is "
                "not a default.".format(
                    who, baseline["mep_start"], baseline["mep_stop"],
                    baseline["kub_start"], baseline["kub_stop"]))
        actions = snap.get("actions") or []
        if actions:
            lines.append("Recent threshold actions:")
            for a in actions:
                lines.append(f"- {a['at']} {a['result']}: {a['reason']}")
        if not lines:
            return None
        block = "\n".join(lines)
        if len(block) > self.PLAN_CONTEXT_MAX:
            block = block[:self.PLAN_CONTEXT_MAX] + " ..."
        log.info("plan context: %d chars", len(block))
        return block

    def status_sentence(self, lang=None):
        """Deterministic one-line status, used when the model will not look."""
        try:
            data = history.fetch_data(self.cfg)
        except requests.RequestException:
            return ask_server.FALLBACK.get(lang or "en")
        v = _fmt(data.get("batteryVoltage"), 2)
        battery = usablemod.phrase(self.usable_now(data), lang, spoken=True)
        solar = sum(data.get(k) or 0 for k in
                    ("mppt80PVPower", "southArrayPVPower", "westArrayPVPower"))
        running = [n for n, k in (("MEP", "mep803aAction"), ("Kubota", "kubotaAction"))
                   if data.get(k) == history.GEN_RUNNING]
        if lang == "es":
            gen = ("Ningun generador esta funcionando." if not running
                   else f"{' y '.join(running)} esta funcionando.")
            return (f"{battery}, {v} voltios. "
                    f"Solar {solar:.0f} vatios. {gen}")
        gen = ("No generator is running." if not running
               else f"{' and '.join(running)} running.")
        return (f"{battery}, {v} volts. "
                f"Solar {solar:.0f} watts. {gen}")

    def usable_now(self, data=None, now=None, conn=None):
        """usable.current() for this moment; never raises. Display only."""
        conn = conn or self.conn
        try:
            soc = (data or {}).get("battSocBM")
            return usablemod.current(conn, self.cfg, now=now, monitor_soc=soc)
        except Exception as e:                       # noqa: BLE001
            log.warning("could not compute the usable battery figure: %s", e)
            return None

    def battery_json(self, conn=None, query=None):
        """GET /battery: the usable figure, for the VPS and the dashboard.

        With ?date=YYYY-MM-DD (and step=1 or 5 minutes, default 5) it also
        carries that local day's series and anchors under "day", for the
        dashboard's battery chart. A bad date or step is a ValueError (400).
        """
        conn = conn or self.conn
        query = query or {}
        u = usablemod.current(conn, self.cfg)
        u["text"] = {"en": usablemod.phrase(u, "en"), "es": usablemod.phrase(u, "es")}
        if query.get("date"):
            day = query["date"]
            datetime.strptime(day, "%Y-%m-%d")       # ValueError -> 400
            try:
                step = int(query.get("step", 5))
            except ValueError:
                raise ValueError("step must be 1 or 5 (minutes)")
            u["day"] = usablemod.day_series(conn, self.cfg, day, step_min=step)
        return u

    def latest_plan_json(self, conn=None):
        """The plan record plus the live learning gate.

        Takes a connection so the ask server can hand in a short-lived
        read-only one from its own thread. Always returns a payload, even
        before the first tick: the Pi5 watchdog treats any answer as proof the
        agent is alive, and needs `learning.open` to know whether the agent is
        even allowed to have moved the thresholds.
        """
        conn = conn or self.conn
        model = loadmodel.LoadModel(conn, self.cfg)
        try:
            gate = model.learning_status()
        except sqlite3.Error as e:
            log.warning("could not read the learning gate: %s", e)
            gate = None
        # The Pi5 watchdog resets to `defaults`; it must not carry its own copy
        # of numbers that live in config.json. Once the owner has set the
        # thresholds by hand, theirs are the values to return to, so those are
        # what it is told - but it applies one start/stop pair to both
        # generators, so it can only be told a baseline shaped that way.
        baseline = self.guard.baseline()
        symmetric = (baseline["mep_start"] == baseline["kub_start"]
                     and baseline["mep_stop"] == baseline["kub_stop"])
        defaults = ({"start": baseline["mep_start"], "stop": baseline["mep_stop"]}
                    if symmetric else
                    {"start": self.cfg["default_start"],
                     "stop": self.cfg["default_stop"]})
        if not symmetric:
            log.warning("the owner's baseline %s differs per generator; the "
                        "watchdog can only be told one pair, so it still has "
                        "the config defaults", baseline)
        payload = {"ts": None, "text": None, "data": {}, "learning": gate,
                   "defaults": defaults,
                   "baseline": baseline,
                   "owner_baseline": self.guard.owner_baseline(),
                   "intended": self.guard.intended()}
        # The dashboard badge shows these under the plan, so the owner can see
        # what the agent has been refused as well as what it decided.
        try:
            payload["actions"] = [
                {"at": history.clock(r["ts"], self.cfg), "tool": r["tool"],
                 "result": r["result"], "reason": r["reason"],
                 "voltage": r["voltage"]}
                for r in history.recent_actions(conn, limit=5)]
        except sqlite3.Error as e:
            log.warning("could not read recent actions: %s", e)
            payload["actions"] = []
        plan = history.latest_plan(conn)
        if plan:
            try:
                data = json.loads(plan["data"] or "{}")
            except json.JSONDecodeError:
                data = {}
            payload.update(ts=plan["ts"], text=plan["text"], data=data)
        return payload

    def telegram_loop(self):
        """Long-poll for owner messages. Only the configured chat is answered."""
        while not self.stop_event.is_set():
            try:
                updates = telegram.get_updates(self.cfg, offset=self.telegram_offset)
            except Exception as e:                   # noqa: BLE001
                log.warning("telegram poll failed: %s", e)
                self.stop_event.wait(30)
                continue
            for update_id, text in updates:
                self.telegram_offset = update_id + 1
                if not text:
                    continue
                log.info("telegram in: %s", text[:120])
                try:
                    telegram.send(self.cfg, telegram.escape(self.answer(text)))
                except Exception:                    # noqa: BLE001
                    log.exception("failed to answer a Telegram message")
            if not updates:
                self.stop_event.wait(1)

    # --- anomalies ----------------------------------------------------------

    def check_anomalies(self):
        """Deterministic triggers that wake the model immediately."""
        try:
            data = history.fetch_data(self.cfg)
        except requests.RequestException as e:
            log.warning("anomaly check could not read /data: %s", e)
            return []
        now = int(time.time())
        fired = []

        errs = data.get("pollErrors")
        if errs is not None:
            if (self.last_poll_errors is not None
                    and errs - self.last_poll_errors >= POLL_ERROR_JUMP):
                fired.append(("poll_errors",
                              f"Modbus poll errors rose by "
                              f"{errs - self.last_poll_errors} in 5 minutes "
                              f"(now {errs})."))
            self.last_poll_errors = errs

        for key, name in (("mepAgsOnline", "MEP"), ("kubotaAgsOnline", "Kubota")):
            if data.get(key) is False:
                fired.append((f"ags_{key}", f"{name} AGS has gone offline."))

        arrays = {"mppt80": data.get("mppt80PVPower") or 0,
                  "south": data.get("southArrayPVPower") or 0,
                  "west": data.get("westArrayPVPower") or 0}
        for name, w in arrays.items():
            others = [v for k, v in arrays.items() if k != name]
            avg = sum(others) / len(others)
            if avg > ARRAY_MIN_OTHERS_AVG_W and w < ARRAY_IMBALANCE_RATIO * avg:
                since = self.array_low_since.setdefault(name, now)
                if now - since >= ARRAY_IMBALANCE_SECONDS:
                    fired.append((f"array_{name}",
                                  f"{name} array has produced under 30% of the "
                                  f"others' average for 30 minutes "
                                  f"({w:.0f} W vs {avg:.0f} W)."))
            else:
                self.array_low_since.pop(name, None)

        v = data.get("batteryVoltage")
        gen_running = (data.get("mep803aAction") == history.GEN_RUNNING
                       or data.get("kubotaAction") == history.GEN_RUNNING)
        if v is not None:
            if v < 51.0:
                fired.append(("v_critical", f"Battery is at {v:.2f} V, below 51.0."))
            elif (v < 52.5 and not gen_running
                  and data.get("autoGenEnabled") is False):
                fired.append(("v_low_no_autogen",
                              f"Battery is at {v:.2f} V with no generator running "
                              f"and auto-gen disabled."))

        drift = self.soc_drift(data)
        if drift:
            fired.append(drift)

        raised = []
        for key, message in fired:
            if now - self.anomaly_last.get(
                    key, 0) < ANOMALY_COOLDOWNS.get(key, ANOMALY_COOLDOWN):
                continue
            self.anomaly_last[key] = now
            raised.append((key, message))
            log.warning("anomaly %s: %s", key, message)
            self.on_anomaly(key, message)
        return raised

    def soc_drift(self, data, floor_v=52.0, now=None):
        """The Battery Monitor claiming more charge than the curve, or None.

        Nothing decides on the Battery Monitor's state of charge any more, so
        a shunt that has drifted no longer moves a threshold - and would
        never be noticed either. This is the check that keeps it visible.
        Not asked while a generator is running: both the voltage and the
        shunt read high under charge, and the curve is a discharge curve.
        """
        if (data.get("mep803aAction") == history.GEN_RUNNING
                or data.get("kubotaAction") == history.GEN_RUNNING):
            return None
        if data.get("battMonitorOnline") is False:
            return None
        try:
            d = self.model.soc_disagreement(data.get("battSocBM"),
                                            data.get("batteryVoltage"),
                                            floor_v=floor_v, now=now)
        except sqlite3.Error as e:
            log.warning("could not compare SOC with the learned curve: %s", e)
            return None
        if not d or d["excess"] <= SOC_DRIFT_EXCESS:
            return None
        return ("soc_drift",
                f"Battery Monitor SOC {d['soc_pct']:.0f}% implies "
                f"{d['implied_wh']:,} Wh above {floor_v:.1f} V at "
                f"{d['voltage']:.2f} V; the learned Wh-vs-V curve says "
                f"{d['learned_wh']:,} Wh ({d['source']}, {d['nights']} nights) "
                f"— {d['excess'] * 100:.0f}% more than the pack has been seen "
                f"to hold. The shunt may need re-syncing. No decision uses "
                f"SOC, so nothing has moved because of it.")

    def on_anomaly(self, key, message):
        """Wake the model with the anomaly as its question."""
        question = (f"ANOMALY: {message} What does this mean and what, "
                    f"if anything, should be done?")
        hint = next((h for prefix, h in ANOMALY_HINTS.items()
                     if key.startswith(prefix)), None)
        if hint:
            question += f"\n\n{hint}"
        try:
            reply = self.answer(question)
        except Exception:                            # noqa: BLE001
            log.exception("anomaly handling failed")
            reply = ""
        text = f"⚠️ <b>{telegram.escape(message)}</b>"
        if reply:
            text += f"\n\n{telegram.escape(reply)}"
        if self.dry_run:
            print(text)
        else:
            telegram.send(self.cfg, text)

    # --- wiring -------------------------------------------------------------

    def sample_once(self):
        try:
            history.poll_once(self.conn, self.cfg)
        except (requests.RequestException, Exception) as e:   # noqa: BLE001
            log.debug("sample failed: %s", e)
        self.update_usable()

    def update_usable(self):
        """Log any battery anchor in the samples since the last one."""
        try:
            usablemod.update(self.conn)
        except Exception as e:                       # noqa: BLE001
            log.warning("battery anchor update failed: %s", e)

    def poll_errors_seen(self, now=None):
        """Has the Pi5's own Modbus poll lost a read in the last 2 minutes?

        pollErrors is a per-cycle count, zeroed at the top of every 5-second
        poll, not a running total — so "it went up" is "it left the zero it
        normally sits at", and any non-zero reading in the window says so.
        Both the stored samples and a live /data read count; the samples are
        60 s apart and a lost read is gone in 5, so neither alone is enough.
        """
        now = int(now or time.time())
        seen = [r["poll_errors"] for r in self.conn.execute(
            "SELECT poll_errors FROM samples WHERE ts >= ? AND ts <= ?",
            (now - COUNTERS_QUIET_SECONDS, now))
            if r["poll_errors"] is not None]
        try:
            live = history.fetch_data(self.cfg).get("pollErrors")
        except requests.RequestException as e:
            log.warning("could not read /data before the counters run: %s", e)
            live = None
        if live is not None:
            seen.append(live)
        return any(v > 0 for v in seen)

    def record_counters(self, now=None):
        """The gateway's energy counters, hourly, off the tick.

        72 reads, each its own TCP connection, at the gateway the Pi5 polls
        every 5 seconds. Run from the tick they collided with that poll and
        cost the owner a Telegram; run here they are spaced, they are hourly,
        and they stand down while the Pi5 is already losing reads rather than
        adding to a problem the gateway is already having.
        """
        now = int(now or time.time())
        if self.poll_errors_seen(now):
            log.info("counters run skipped: the Pi5's poll lost a read in the "
                     "last %d s", COUNTERS_QUIET_SECONDS)
            return 0
        try:
            return counters.record(self.conn, self.cfg, ts=now)
        except Exception as e:                       # noqa: BLE001
            log.warning("counters run failed: %s", e)
            return 0

    def run(self, ask_host=None):
        sched = BackgroundScheduler(timezone=self.cfg["tz"])
        sched.add_job(self.sample_once, "interval",
                      seconds=history.SAMPLE_SECONDS, id="sampler",
                      max_instances=1, coalesce=True)
        sched.add_job(self.tick, "interval", minutes=self.cfg["tick_minutes"],
                      id="tick", max_instances=1, coalesce=True)
        sched.add_job(self.check_anomalies, "interval", minutes=5,
                      id="anomalies", max_instances=1, coalesce=True)
        sched.add_job(self.record_counters, "cron", minute=COUNTERS_MINUTE,
                      id="counters", max_instances=1, coalesce=True)
        for hour in self.cfg["digest_hours"]:
            evening = hour >= 12
            sched.add_job(self.digest, "cron", hour=hour, minute=0,
                          args=[evening], id=f"digest_{hour}")
        sched.add_job(self.purge, "cron", hour=3, minute=30, id="purge")
        sched.start()

        server = ask_server.serve(self.cfg, self.answer, self.latest_plan_json,
                                  host=ask_host, db_path=self.db_path,
                                  brief_handler=self.plan_brief,
                                  battery_provider=self.battery_json)
        threading.Thread(target=self.telegram_loop, name="telegram",
                         daemon=True).start()
        # In the background: startup must not wait on the model, and the
        # model may not be there at all.
        threading.Thread(target=self.warm_prompt_cache, name="warmup",
                         daemon=True).start()

        log.info("solar agent running: tick every %s min, digests at %s, "
                 "counters hourly at :%02d",
                 self.cfg["tick_minutes"], self.cfg["digest_hours"],
                 COUNTERS_MINUTE)
        # Bootstrap the battery anchors before the first plan record names
        # the usable figure; on a fresh table this replays every sample.
        self.update_usable()
        self.tick()
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1)
        finally:
            log.info("shutting down")
            sched.shutdown(wait=False)
            if server:
                server.shutdown()

    def warm_prompt_cache(self):
        """Pay for the prompt cache before a person is waiting on it.

        The first model call after a restart rebuilds the cache, about 50 s on
        the KAMRUI's integrated GPU. A question in that window costs that plus
        a tool call plus a second turn - 80 to 100 s - and the Pi5 dashboard
        gave up at 90, so the owner read "not answering" about an answer that
        arrived. Nothing was broken; the first caller was simply the one who
        paid.

        So the agent pays instead, at startup, with a throwaway question. The
        system text is exactly what ask_prompt produces and the tool schemas
        are the ones every turn sends, because a cache is a prefix and a
        prefix that differs anywhere is a cache that misses. Only the NOW line
        and the question itself change afterwards, and both come after the
        POLICY block that is most of the bytes.

        Runs in its own thread and never raises. A missing llama-server is a
        thing the agent is expected to survive - it starts, samples, plans and
        refuses to answer - so a warm-up that cannot connect says so quietly
        and stops.
        """
        for attempt in range(WARMUP_ATTEMPTS):
            if self.stop_event.is_set():
                return None
            started = time.monotonic()
            try:
                self.llm.chat(
                    [{"role": "system", "content": prompts.ask_prompt()},
                     {"role": "user", "content": prompts.ask_user(
                         WARMUP_QUESTION,
                         now_text=history.stamp(int(time.time()), self.cfg))}],
                    tools=toolsmod.SCHEMAS, temperature=0.0,
                    max_tokens=WARMUP_MAX_TOKENS)
            except LLMError as e:
                if attempt + 1 < WARMUP_ATTEMPTS:
                    log.debug("prompt cache warm-up: %s; trying again in %ss",
                              e, WARMUP_RETRY_SECONDS)
                    self.stop_event.wait(WARMUP_RETRY_SECONDS)
                    continue
                log.info("prompt cache not warmed: llama-server is not "
                         "answering. The agent runs without it; the first "
                         "question will pay for the cache.")
                return None
            except Exception:                        # noqa: BLE001
                log.exception("prompt cache warm-up failed")
                return None
            elapsed = time.monotonic() - started
            log.info("prompt cache warmed in %.1f s", elapsed)
            return elapsed
        return None

    def purge(self):
        conn = self.connection()
        history.purge_samples(conn)
        n = history.purge_plan_prompts(conn, self.cfg)
        if n:
            log.info("dropped the recorded prompt from %s old plans", n)

    def shutdown(self, *_):
        self.stop_event.set()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="one tick against the live dashboard; write nothing")
    ap.add_argument("--once", action="store_true", help="one real tick, then exit")
    ap.add_argument("--digest", choices=["morning", "evening"],
                    help="send one digest and exit")
    ap.add_argument("--ask", help="ask one question and exit")
    ap.add_argument("--ask-host", help="override the ask server bind address")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.load()
    agent = Agent(cfg, dry_run=args.dry_run)

    if args.ask:
        print(agent.answer(args.ask))
        return 0
    if args.digest:
        agent.digest(args.digest == "evening")
        return 0
    if args.dry_run or args.once:
        record = agent.tick()
        if record is None:
            print("tick failed; see the log above")
            return 1
        print(record)
        return 0

    signal.signal(signal.SIGTERM, agent.shutdown)
    signal.signal(signal.SIGINT, agent.shutdown)
    agent.run(ask_host=args.ask_host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
