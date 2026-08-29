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
from datetime import datetime, timedelta

import requests
from apscheduler.schedulers.background import BackgroundScheduler

import ask_server
import config
import counters
import guard as guardmod
import history
import loadmodel
import policy as policymod
import prompts
import telegram
import tools as toolsmod
import weather
from llm import LLM, LLMError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
ANOMALY_COOLDOWN = 1800
POLL_ERROR_JUMP = 10
ARRAY_IMBALANCE_RATIO = 0.30
ARRAY_IMBALANCE_SECONDS = 1800
DAYLIGHT_MIN_W = 200          # below this the arrays are not comparable
RECOMMEND_RE = re.compile(r"^\s*recommend:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _fmt(v, places=1, dash="?"):
    return dash if v is None else f"{v:.{places}f}"


def _kwh(wh):
    return "?" if wh is None else f"{wh / 1000.0:.1f}"


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
        self.guard = guardmod.Guard(self.connection, cfg, model=self.model)
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
        today = history.local_day(now, self.cfg)

        solar_w = sum(data.get(k) or 0 for k in
                      ("mppt80PVPower", "southArrayPVPower", "westArrayPVPower"))
        gen_running = (data.get("mep803aAction") == history.GEN_RUNNING
                       or data.get("kubotaAction") == history.GEN_RUNNING)

        peak_row = self.conn.execute(
            "SELECT MAX(battery_v) p, MIN(battery_v) m FROM samples WHERE ts >= ?",
            (history.day_bounds(today, self.cfg)[0],)).fetchone()
        peak_today = peak_row["p"] if peak_row else None
        if peak_today is None or (data.get("batteryVoltage") or 0) > peak_today:
            peak_today = data.get("batteryVoltage")

        wx = weather.summary(self.cfg, now=now)
        sunrise_ts = wx.get("next_sunrise_ts")
        hours_to_sunrise = (max(1, int((sunrise_ts - now) / 3600) + 1)
                            if sunrise_ts else 12)
        forecast = self.model.load_forecast(min(hours_to_sunrise, 24), now=now)
        projection = self.model.project_voltage(52.0, now=now)
        drawdown = self.model.overnight_drawdown(now=now)
        gate = self.model.learning_status(now=now)
        soc_curve = self.model.soc_curve_status()

        tomorrow_cloud = None
        est_solar = None
        if wx.get("tomorrow"):
            t = wx["tomorrow"]
            tomorrow_cloud = (t.get("daylight_cloud_pct")
                              if t.get("daylight_cloud_pct") is not None
                              else t.get("cloud_pct"))
            est_solar = self.model.estimate_solar_wh(tomorrow_cloud, now=now)

        # POLICY 4 and 5 are arithmetic over these two, so they are gathered
        # here rather than left for the model to guess at.
        charge_rates, run_window_h = {}, {}
        for gen, cfg_key in (("mep", "mep803a"), ("kubota", "kubota")):
            rate = self.model.charge_rate(gen, solo=True, now=now)
            if rate is None:
                rate = self.model.charge_rate(gen, solo=None, now=now)
            charge_rates[gen] = rate
            try:
                run_window_h[gen] = min(live[cfg_key]["maxRuntime"] / 60.0,
                                        self.cfg["ags_max_run_hours"][gen])
            except (KeyError, TypeError):
                run_window_h[gen] = self.cfg["ags_max_run_hours"][gen]

        facts = {
            "now": now, "today": today, "data": data, "config": live,
            "voltage": data.get("batteryVoltage"), "soc": data.get("battSocBM"),
            "load_w": None if gen_running else (data.get("acPower1") or 0)
                                              + (data.get("acPower2") or 0),
            "solar_w": solar_w, "gen_running": gen_running,
            "peak_today": peak_today,
            "weather": wx, "sunrise_ts": sunrise_ts,
            "forecast": forecast, "projection": projection,
            "drawdown": drawdown, "gate": gate, "soc_curve": soc_curve,
            "tomorrow_cloud": tomorrow_cloud, "est_solar": est_solar,
            "summary_24h": history.summary(self.conn, 24, now=now),
            "thresholds": toolsmod.thresholds_from_config(live),
            "intended": self.guard.intended(),
            "owner_baseline": self.guard.owner_baseline(),
            "charge_rates": charge_rates, "run_window_h": run_window_h,
        }
        facts["policy"] = policymod.evaluate(self.cfg, facts)
        return facts

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
        lines.append(f"{t.strftime('%Y-%m-%d %H:%M')}  "
                     f"V {_fmt(facts['voltage'], 1)}  "
                     f"SOC {facts['soc'] if facts['soc'] is not None else '?'}%  "
                     f"load {load_kw}")

        peak = facts["peak_today"]
        thresh = self.cfg["solo_peak_threshold"]
        if peak is None:
            lines.append(f"peak today: ?  (threshold {thresh})")
        elif peak < thresh:
            lines.append(f"peak today: {peak:.1f} V  "
                         f"(threshold {thresh} -> solar shortfall)")
        else:
            lines.append(f"peak today: {peak:.1f} V  (threshold {thresh} -> reached)")

        month = t.strftime("%b")
        kind = "weekend" if t.weekday() >= 5 else "weekday"
        if facts["drawdown"]:
            lines.append(f"overnight Wh (profile, {month} {kind}): "
                         f"{facts['drawdown']['wh']:,}")
        else:
            lines.append(f"overnight Wh (profile, {month} {kind}): not learned yet")

        proj = facts["projection"]
        sunrise = (datetime.fromtimestamp(facts["sunrise_ts"], tz).strftime("%H:%M")
                   if facts["sunrise_ts"] else "?")
        if proj and proj.get("reached"):
            lines.append(f"projected 52.0 V at: "
                         f"{self.model.projection_label(proj, facts['now'])}   "
                         f"sunrise {sunrise}")
        else:
            why = (proj or {}).get("reason", "unknown")
            lines.append(f"projected 52.0 V at: not projected ({why})   "
                         f"sunrise {sunrise}")

        if facts["tomorrow_cloud"] is None:
            lines.append("forecast tomorrow: unavailable")
        elif facts["est_solar"]:
            e = facts["est_solar"]
            lines.append(f"forecast tomorrow: {facts['tomorrow_cloud']}% cloud, "
                         f"est. solar {_kwh(e['wh'])} kWh "
                         f"({month} clear-day {_kwh(e['clear_day_wh'])})")
        else:
            lines.append(f"forecast tomorrow: {facts['tomorrow_cloud']}% cloud, "
                         f"est. solar not learned yet")

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
            f"Time: {t.strftime('%Y-%m-%d %H:%M %Z')} ({'weekend' if t.weekday() >= 5 else 'weekday'})",
            "",
            "NOW",
            f"  battery {_fmt(f['voltage'], 2)} V, SOC {f['soc']}%, "
            f"monitor {'online' if f['data'].get('battMonitorOnline') else 'OFFLINE'}",
            f"  solar {f['solar_w']} W, house load "
            f"{'unknown (generator running)' if f['load_w'] is None else str(f['load_w']) + ' W'}",
            f"  MEP {'RUNNING' if f['data'].get('mep803aAction') == history.GEN_RUNNING else 'stopped'}"
            f", AGS {'online' if f['data'].get('mepAgsOnline') else 'OFFLINE'}"
            f"; Kubota {'RUNNING' if f['data'].get('kubotaAction') == history.GEN_RUNNING else 'stopped'}"
            f", AGS {'online' if f['data'].get('kubotaAgsOnline') else 'OFFLINE'}",
            f"  peak voltage today {_fmt(f['peak_today'], 2)} V",
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
        sc = f["soc_curve"]
        if sc.get("soc_at_start_threshold") is not None:
            parts.append(f"  learned: {sc['start_threshold_v']} V is about "
                         f"{sc['soc_at_start_threshold']}% SOC")
        if wx.get("tomorrow"):
            parts.append(f"  tomorrow: {f['tomorrow_cloud']}% daylight cloud, "
                         f"max {wx['tomorrow']['max_temp_c']} C")
        if f["est_solar"]:
            parts.append(f"  estimated solar tomorrow: {_kwh(f['est_solar']['wh'])} kWh "
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
                which = ", ".join(f"POLICY {r['rule']} {r['name']}" for r in fired)
                parts.append(f"  {which} FIRES. Either set the thresholds it "
                             f"calls for, or overrule it on its own line: "
                             f'"overrule POLICY <n>: <reason>". Saying "no '
                             f'change" without that line is a policy miss.')
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
            counters.record(self.conn, self.cfg, ts=facts["now"])
        except Exception as e:                       # noqa: BLE001
            log.warning("store update failed, continuing: %s", e)

        tools = self.tools(policy=facts["policy"])
        try:
            text, write_result = self.run_model(
                prompts.system_prompt(), self.tick_prompt(facts), tools)
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
            "peak_today": facts["peak_today"],
            "thresholds": facts["thresholds"],
            "gate_open": facts["gate"]["open"],
            "projection": facts["projection"],
            "soc_curve": facts["soc_curve"],
            "policy": facts["policy"],
            "policy_misses": [r["rule"] for r in missed],
            "write": write_result,
            "dry_run": self.dry_run,
        }, ts=facts["now"])

        self.heartbeat(facts)
        log.info("tick done in %.1fs; %s", time.time() - started, applied)
        return record

    def heartbeat(self, facts):
        """SPEC section 9: re-send the intended thresholds every tick."""
        if self.dry_run:
            return False
        send, values, why = self.guard.heartbeat(now=facts["now"])
        if not send:
            log.debug("heartbeat withheld: %s", why)
            return False
        try:
            live = toolsmod.apply_thresholds(
                self.cfg, values["mep_start"], values["mep_stop"],
                values["kub_start"], values["kub_stop"])
            self.guard.note_write(toolsmod.thresholds_from_config(live),
                                  now=facts["now"])
            log.debug("heartbeat sent %s", values)
            return True
        except requests.RequestException as e:
            log.warning("heartbeat failed: %s", e)
            return False

    # --- digests ------------------------------------------------------------

    def digest(self, evening):
        """19:00 carries tonight's plan; 07:00 scores last night's projection."""
        facts = self.gather()
        tz = history.tzinfo(self.cfg)
        now = facts["now"]
        head = "Evening plan" if evening else "Overnight report"
        lines = [f"<b>{head}</b>",
                 f"V {_fmt(facts['voltage'], 2)}  SOC {facts['soc']}%  "
                 f"peak today {_fmt(facts['peak_today'], 2)} V"]

        if evening:
            plan = history.latest_plan(self.conn)
            if plan:
                lines += ["", plan["text"]]
        else:
            since = now - 16 * 3600
            predicted = None
            for row in self.conn.execute(
                    "SELECT ts, data FROM plans WHERE ts >= ? ORDER BY ts", (since,)):
                try:
                    d = json.loads(row["data"] or "{}")
                except json.JSONDecodeError:
                    continue
                p = (d.get("projection") or {})
                if p.get("reached"):
                    predicted = p
                    break
            low = self.conn.execute(
                "SELECT MIN(battery_v) v, ts FROM samples WHERE ts >= ? "
                "ORDER BY battery_v LIMIT 1", (since,)).fetchone()
            if predicted:
                lines.append(f"predicted 52.0 V at {predicted.get('at')}")
            else:
                lines.append("no 52.0 V projection was made last night")
            if low and low["v"] is not None:
                at = datetime.fromtimestamp(low["ts"], tz).strftime("%H:%M")
                lines.append(f"actual low {low['v']:.2f} V at {at}")
            runs = history.gen_runs(self.conn, 1, now=now)
            if runs:
                for r in runs:
                    start = datetime.fromtimestamp(r["start_ts"], tz).strftime("%H:%M")
                    lines.append(f"{r['gen']} ran {start} for "
                                 f"{r['duration_min']:.0f} min "
                                 f"({_fmt(r['start_v'], 1)} -> {_fmt(r['stop_v'], 1)} V)")
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

    # --- inbound questions --------------------------------------------------

    def answer(self, text, lang=None):
        """Shared by Telegram inbound and POST /ask.

        An answer must be grounded in a tool result. Qwen3-8B will sometimes
        answer a question about live state without calling anything, and then
        it invents the number — which POLICY 7 forbids. So a reply produced
        with no tool call is retried once, and if the model still will not
        look, Python answers from /data instead of letting a made-up voltage
        reach the owner or Alexa.
        """
        if text.strip().lower() in ("plan", "/plan", "el plan"):
            plan = history.latest_plan(self.conn)
            return plan["text"] if plan else "No plan has been recorded yet."
        with self.lock:
            tools = self.tools()
            try:
                reply, _ = self.run_model(prompts.ask_prompt(lang), text, tools,
                                          max_rounds=MAX_TOOL_ROUNDS)
                if not tools.calls:
                    log.warning("answer was ungrounded; retrying with a nudge")
                    reply, _ = self.run_model(
                        prompts.ask_prompt(lang),
                        text + "\n\n(You have not called a tool yet. Call the "
                               "tool that answers this before replying.)",
                        tools)
                if not tools.calls:
                    log.warning("model would not call a tool; answering from /data")
                    return self.status_sentence(lang)
            except LLMError as e:
                log.error("model unavailable for question: %s", e)
                return ask_server.FALLBACK.get(lang or "en")
        return (reply or "").strip() or ask_server.FALLBACK.get(lang or "en")

    def status_sentence(self, lang=None):
        """Deterministic one-line status, used when the model will not look."""
        try:
            data = history.fetch_data(self.cfg)
        except requests.RequestException:
            return ask_server.FALLBACK.get(lang or "en")
        v = _fmt(data.get("batteryVoltage"), 2)
        soc = data.get("battSocBM")
        solar = sum(data.get(k) or 0 for k in
                    ("mppt80PVPower", "southArrayPVPower", "westArrayPVPower"))
        running = [n for n, k in (("MEP", "mep803aAction"), ("Kubota", "kubotaAction"))
                   if data.get(k) == history.GEN_RUNNING]
        if lang == "es":
            gen = ("Ningun generador esta funcionando." if not running
                   else f"{' y '.join(running)} esta funcionando.")
            return (f"La bateria esta a {v} voltios, {soc} por ciento de carga. "
                    f"Solar {solar:.0f} vatios. {gen}")
        gen = ("No generator is running." if not running
               else f"{' and '.join(running)} running.")
        return (f"Battery {v} volts, {soc} percent charge. "
                f"Solar {solar:.0f} watts. {gen}")

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
        payload = {"ts": None, "text": None, "data": {}, "learning": gate,
                   # The Pi5 watchdog resets to these; it must not carry its
                   # own copy of numbers that live in config.json.
                   "defaults": {"start": self.cfg["default_start"],
                                "stop": self.cfg["default_stop"]},
                   "intended": self.guard.intended()}
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
                    telegram.send(self.cfg, self.answer(text))
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
        if sum(arrays.values()) > DAYLIGHT_MIN_W:
            for name, w in arrays.items():
                others = [v for k, v in arrays.items() if k != name]
                avg = sum(others) / len(others)
                if avg > 0 and w < ARRAY_IMBALANCE_RATIO * avg:
                    since = self.array_low_since.setdefault(name, now)
                    if now - since >= ARRAY_IMBALANCE_SECONDS:
                        fired.append((f"array_{name}",
                                      f"{name} array has produced under 30% of the "
                                      f"others' average for 30 minutes "
                                      f"({w:.0f} W vs {avg:.0f} W)."))
                else:
                    self.array_low_since.pop(name, None)
        else:
            self.array_low_since.clear()

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

        raised = []
        for key, message in fired:
            if now - self.anomaly_last.get(key, 0) < ANOMALY_COOLDOWN:
                continue
            self.anomaly_last[key] = now
            raised.append((key, message))
            log.warning("anomaly %s: %s", key, message)
            self.on_anomaly(key, message)
        return raised

    def on_anomaly(self, key, message):
        """Wake the model with the anomaly as its question."""
        try:
            reply = self.answer(f"ANOMALY: {message} What does this mean and what, "
                                f"if anything, should be done?")
        except Exception:                            # noqa: BLE001
            log.exception("anomaly handling failed")
            reply = ""
        text = f"⚠️ <b>{message}</b>"
        if reply:
            text += f"\n\n{reply}"
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

    def run(self, ask_host=None):
        sched = BackgroundScheduler(timezone=self.cfg["tz"])
        sched.add_job(self.sample_once, "interval",
                      seconds=history.SAMPLE_SECONDS, id="sampler",
                      max_instances=1, coalesce=True)
        sched.add_job(self.tick, "interval", minutes=self.cfg["tick_minutes"],
                      id="tick", max_instances=1, coalesce=True)
        sched.add_job(self.check_anomalies, "interval", minutes=5,
                      id="anomalies", max_instances=1, coalesce=True)
        for hour in self.cfg["digest_hours"]:
            evening = hour >= 12
            sched.add_job(self.digest, "cron", hour=hour, minute=0,
                          args=[evening], id=f"digest_{hour}")
        sched.add_job(lambda: history.purge_samples(self.connection()), "cron", hour=3,
                      minute=30, id="purge")
        sched.start()

        server = ask_server.serve(self.cfg, self.answer, self.latest_plan_json,
                                  host=ask_host, db_path=self.db_path)
        threading.Thread(target=self.telegram_loop, name="telegram",
                         daemon=True).start()

        log.info("solar agent running: tick every %s min, digests at %s",
                 self.cfg["tick_minutes"], self.cfg["digest_hours"])
        self.tick()
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1)
        finally:
            log.info("shutting down")
            sched.shutdown(wait=False)
            if server:
                server.shutdown()

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
