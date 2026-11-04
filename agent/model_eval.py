#!/usr/bin/env python3
"""Replay stored ticks against a candidate model and score what it does.

The agent's own prompt and answer are recorded with every plan, so a
candidate can be asked exactly what the live model was asked, on exactly the
nights that have already happened, and be marked on the four things that have
actually gone wrong in production:

  tool calls   every call names a real tool and its arguments bind
  numbers      no figure in the answer that was not in the prompt or in a
               tool result - POLICY 9, and the failure that made the agent
               invent a voltage for Alexa
  rules        a POLICY rule that fires is set or overruled in writing, never
               answered with "no change"
  narration    the model never tells the owner a write happened; only the
               write path may say that, and at 12:17 am one model said it
               after the guard had refused

Nothing here writes. The tools are read-only and pinned to the tick being
replayed: get_status comes from the sample recorded at that minute,
get_history and the forecasts are computed from the database as of then, and
set_gen_thresholds and send_telegram are captured rather than performed. The
one exception is get_weather, which has no stored history and answers for now
rather than for then; replays stay close to the present for that reason.

  ./model_eval.py -n 20
  ./model_eval.py -n 20 --candidate http://127.0.0.1:8082/v1/chat/completions=qwen3-14b
  ./model_eval.py --exam --repeat 3 --candidate http://127.0.0.1:8082/v1/chat/completions=gemma4

See the README for running a second llama-server to compare against.
"""

import argparse
import contextlib
import copy
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time

import requests

import config
import eval_cases
import history
import llm as llmmod
import loadmodel
import policy as policymod
import prompts
import sun as sunmod
import tools as toolsmod
import weather

log = logging.getLogger(__name__)

# A number in the model's answer. Thousands separators included, so "9,000"
# is one figure and not two.
NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# A rule number is a citation, not a measurement. "overrule POLICY 4" must not
# read as the model having asserted the figure 4.
CITATION_RE = re.compile(r"\bPOLICY\s*\d+", re.IGNORECASE)

# Telling the owner something has already been done. The contracted
# "recommend: ..." line is removed before this runs, so what is left claiming
# a completed change is narration.
NARRATION_RE = re.compile(
    r"\b(?:adjusted|changed|updated|has been set|have been set|i (?:have )?set|"
    r"i (?:have )?raised|i (?:have )?lowered|thresholds? (?:are|is) now|"
    r"now set to|successfully (?:set|applied|changed))\b", re.IGNORECASE)

RECOMMEND_LINE_RE = re.compile(r"^\s*recommend:.*$", re.IGNORECASE | re.MULTILINE)


# --- the ticks to replay -----------------------------------------------------

class Tick:
    """One recorded tick, with everything needed to put it to a model again."""

    def __init__(self, ts, prompt, answer, data):
        self.ts = ts
        self.prompt = prompt
        self.answer = answer
        self.data = data
        self.policy = data.get("policy") or []

    @property
    def fired(self):
        return policymod.firing(self.policy)


def load_ticks(conn, limit):
    """The most recent ticks that still have their prompt, oldest first.

    Ticks whose prompt has been pruned are skipped rather than reconstructed:
    a replay of a rebuilt prompt scores a model on a question nobody asked it.
    """
    out = []
    for row in conn.execute("SELECT ts, data FROM plans ORDER BY ts DESC"):
        if len(out) >= limit:
            break
        try:
            data = json.loads(row["data"] or "{}")
        except json.JSONDecodeError:
            continue
        if not data.get("prompt"):
            continue
        out.append(Tick(row["ts"], data["prompt"], data.get("answer") or "", data))
    out.reverse()
    return out


# --- tools, read-only and pinned to the tick ---------------------------------

class EvalTools(toolsmod.Tools):
    """The real tools, answering as of `at`, writing and sending nothing."""

    def __init__(self, conn, cfg, at, thresholds):
        super().__init__(conn, cfg, guard=None, dry_run=True)
        self.at = at
        self.at_thresholds = thresholds or {}
        self.sent = []
        self.proposed = []

    def call(self, name, args):
        """Record every call, including one that names no tool at all.

        Tools.call drops an unknown name before it reaches self.calls,
        because there the list is what proves an answer was grounded and a
        call that did nothing proves nothing. Here it is precisely what is
        being marked, so it is kept.
        """
        before = len(self.calls)
        result = super().call(name, args)
        if len(self.calls) == before:
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                parsed = {"error": "the tool returned unparseable JSON"}
            self.calls.append((name, args, parsed))
        return result

    def _sample(self):
        return self.conn.execute(
            "SELECT * FROM samples WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (self.at,)).fetchone()

    def get_status(self):
        s = self._sample()
        if s is None:
            return {"error": "no sample was recorded for that minute"}
        th = self.at_thresholds
        solar = sum(s[k] or 0 for k in ("mppt80_pv", "south_pv", "west_pv"))
        running = (s["mep_action"] == history.GEN_RUNNING
                   or s["kub_action"] == history.GEN_RUNNING)
        return {
            "voltage": s["battery_v"], "soc_pct": s["batt_soc"],
            "battery_w": s["batt_power"], "battery_a": s["batt_current"],
            "battery_monitor_online": bool(s["batt_monitor_online"]),
            "ah_remaining": s["batt_ah_remaining"],
            "minutes_to_discharge": s["batt_min_to_discharge"],
            "solar_w": solar,
            "solar_by_array_w": {"mppt80": s["mppt80_pv"], "south": s["south_pv"],
                                 "west": s["west_pv"]},
            "load_w": (None if running
                       else (s["ac_power1"] or 0) + (s["ac_power2"] or 0)),
            "generator_running": running,
            # run_reason as the sample recorded it, so a replayed "why is it
            # running" is answered from the same field the live tool uses.
            "mep": {"running": s["mep_action"] == history.GEN_RUNNING,
                    "ags_online": bool(s["mep_ags_online"]),
                    "run_reason": s["mep_on_reason"],
                    "start_v": th.get("mep_start"), "stop_v": th.get("mep_stop")},
            "kubota": {"running": s["kub_action"] == history.GEN_RUNNING,
                       "ags_online": bool(s["kub_ags_online"]),
                       "run_reason": s["kub_on_reason"],
                       "start_v": th.get("kub_start"), "stop_v": th.get("kub_stop")},
            "auto_gen_enabled": bool(s["auto_gen_enabled"]),
            "poll_errors": s["poll_errors"],
            "as_of": history.stamp(self.at, self.cfg),
        }

    def get_history(self, hours=24, window=None):
        # Same shape as the live tool, anchored at self.at: a replay that
        # cannot say "overnight" cannot replay the question that needed it.
        if window:
            since, until, label = sunmod.window_span(self.cfg, window, self.at)
            if since is None:
                return {"error": label, "windows": list(sunmod.WINDOWS)}
            out = history.summary(self.conn, since=since, until=until,
                                  now=self.at, cfg=self.cfg)
            out["window"] = window
            out["window_label"] = label
            return out
        hours = max(1, min(int(hours), 720))
        return history.summary(self.conn, hours, now=self.at, cfg=self.cfg)

    def get_load_forecast(self, hours):
        hours = max(1, min(int(hours), 48))
        out = self.model.load_forecast(hours, now=self.at)
        out["projected_52v"] = self.model.project_voltage(52.0, now=self.at)
        drawdown = self.model.overnight_drawdown(now=self.at)
        if drawdown:
            out["overnight_drawdown"] = drawdown
        return out

    def get_gen_runtime(self, days):
        days = max(1, min(int(days), 365))
        runs = history.gen_runs(self.conn, days, now=self.at)
        return {"days": days, "charge_rates": self.model.charge_rates(now=self.at),
                "runs": [{"gen": r["gen"],
                          "start": history.stamp(r["start_ts"], self.cfg),
                          "minutes": r["duration_min"],
                          "amps_into_pack": r["rate_a"],
                          "house_load_w": r["load_w"],
                          "solo": bool(r["solo"]), "kind": r["kind"]}
                         for r in runs],
                "note": "exercise runs are excluded"}

    def get_ac_diag(self):
        return {"error": "AC diagnostics are live-only and are not replayed"}

    def send_telegram(self, text):
        self.sent.append(text)
        return {"sent": False, "replay": True}

    def set_gen_thresholds(self, mep_start, mep_stop, kub_start, kub_stop, reason):
        values = {"mep_start": float(mep_start), "mep_stop": float(mep_stop),
                  "kub_start": float(kub_start), "kub_stop": float(kub_stop)}
        self.proposed.append(dict(values, reason=reason))
        return {"applied": False, "replay": True, "would_set": values,
                "reason": "recorded for scoring; nothing is written in a replay"}


# --- the four checks ---------------------------------------------------------

def numbers_in(text):
    """Every figure in a piece of text, normalised so 9,000 == 9000.0."""
    out = set()
    for raw in NUMBER_RE.findall(text or ""):
        try:
            out.add(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def unsourced_numbers(answer, sources):
    """Figures the model stated that were in none of its inputs.

    POLICY 9: restate numbers only from tool results. A conversion the model
    did in its head - 1200 W read back as 1.2 kW - counts, because doing
    arithmetic is exactly what it is told not to do.
    """
    known = set()
    for s in sources:
        known |= numbers_in(s)
    stated = numbers_in(CITATION_RE.sub("", answer or ""))
    return sorted(n for n in stated if n not in known)


def invalid_calls(calls):
    """Calls that named no tool, or whose arguments would not bind."""
    bad = []
    for name, args, result in calls:
        if isinstance(result, dict) and str(result.get("error", "")).startswith(
                ("no such tool", "bad arguments")):
            bad.append(f"{name}({', '.join(sorted(args))}): {result['error']}")
    return bad


def narration(answer, sent):
    """Claims that a change has been made, from a model that made none."""
    body = RECOMMEND_LINE_RE.sub("", answer or "")
    out = [t for t in sent if NARRATION_RE.search(t or "")]
    if NARRATION_RE.search(body):
        out.append(NARRATION_RE.search(body).group(0) + " (in the final answer)")
    return out


def score_tick(tick, answer, tools):
    """Everything one replayed tick says about a model."""
    proposed = tools.proposed[-1] if tools.proposed else None
    write = ({"applied": False, "would_set": {k: v for k, v in proposed.items()
                                              if k != "reason"}}
             if proposed else None)
    sources = [tick.prompt] + [json.dumps(r, default=str) for _, _, r in tools.calls]
    missed = policymod.misses(tick.policy, answer, write)
    return {
        "ts": tick.ts,
        "calls": len(tools.calls),
        "invalid": invalid_calls(tools.calls),
        "unsourced": unsourced_numbers(answer, sources),
        "fired": len(tick.fired),
        "missed": [f"POLICY {r['rule']} {r['name']}" for r in missed],
        "narrated": narration(answer, tools.sent),
        "answer": answer,
    }


def clean(row):
    return not (row["invalid"] or row["unsourced"] or row["missed"]
                or row["narrated"])


# --- running a candidate ------------------------------------------------------

class Candidate:
    def __init__(self, url, name, label=None):
        self.url, self.name = url, name
        self.label = label or f"{name} @ {url.split('//')[-1].split('/')[0]}"

    def llm(self, cfg, timeout):
        return llmmod.LLM(dict(cfg, llm_url=self.url, llm_model=self.name),
                          timeout=timeout)


def replay(tick, candidate, cfg, conn, rounds=4, timeout=180):
    """Put one tick to one model. Returns the scored row."""
    tools = EvalTools(conn, cfg, tick.ts, tick.data.get("thresholds"))
    client = candidate.llm(cfg, timeout)
    messages = [{"role": "system", "content": prompts.system_prompt()},
                {"role": "user", "content": tick.prompt}]
    answer = ""
    for _ in range(rounds):
        msg = client.chat(messages, tools=toolsmod.SCHEMAS)
        calls = llmmod.LLM.tool_calls(msg)
        if not calls:
            answer = msg.get("content", "")
            break
        messages.append(msg)
        for call_id, name, args in calls:
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "name": name, "content": tools.call(name, args)})
    else:
        messages.append({"role": "user",
                         "content": "Tool budget spent. Give your final answer now."})
        answer = client.chat(messages).get("content", "")
    return score_tick(tick, answer, tools)


# --- the table ----------------------------------------------------------------

def table(results):
    """One row per model, and the faults spelled out underneath."""
    head = (f"{'model':<28}{'ticks':>6}{'clean':>7}{'calls':>7}"
            f"{'invalid':>9}{'unsourced':>11}{'fired':>7}{'missed':>8}{'narrated':>10}")
    lines = [head, "-" * len(head)]
    for label, rows in results.items():
        if not rows:
            lines.append(f"{label:<28}{'no ticks replayed':>48}")
            continue
        n = len(rows)
        lines.append(
            f"{label:<28}{n:>6}"
            f"{sum(clean(r) for r in rows) * 100 // n:>6}%"
            f"{sum(r['calls'] for r in rows):>7}"
            f"{sum(len(r['invalid']) for r in rows):>9}"
            f"{sum(len(r['unsourced']) for r in rows):>11}"
            f"{sum(r['fired'] for r in rows):>7}"
            f"{sum(len(r['missed']) for r in rows):>8}"
            f"{sum(len(r['narrated']) for r in rows):>10}")
    return "\n".join(lines)


def faults(results, cfg, limit=12):
    """What actually went wrong, so a number in the table can be chased."""
    out = []
    for label, rows in results.items():
        shown = 0
        for r in rows:
            if clean(r) or shown >= limit:
                continue
            shown += 1
            when = history.stamp(r["ts"], cfg)
            out.append(f"\n{label} — {when}")
            for bad in r["invalid"]:
                out.append(f"  invalid call   {bad}")
            if r["unsourced"]:
                out.append(f"  unsourced      {r['unsourced']}")
            for m in r["missed"]:
                out.append(f"  rule missed    {m}")
            for t in r["narrated"]:
                out.append(f"  narrated       {t[:90]}")
    return "\n".join(out)


# --- the exam: questions put to the running agent ----------------------------

def ask(url, question, timeout=90):
    """POST one question to the agent's own /ask, as Alexa and the dashboard do."""
    r = requests.post(url, json={"text": question}, timeout=timeout)
    r.raise_for_status()
    return r.text.strip()


def exam(url, cfg, conn, timeout=90, only=None):
    """Run the Q&A cases against a live agent and mark each one."""
    now = int(time.time())
    rows = []
    for case in eval_cases.CASES:
        if only and case["id"] not in only:
            continue
        truth = case["truth"](conn, cfg, now)
        try:
            reply = ask(url, case["question"], timeout=timeout)
            passed, why = case["grade"](reply, truth)
        except requests.RequestException as e:
            reply, passed, why = "", False, f"the agent did not answer: {e}"
        rows.append({"id": case["id"], "question": case["question"],
                     "about": case["about"], "reply": reply,
                     "pass": passed, "why": why, "truth": truth})
    return rows


def exam_report(rows):
    out = []
    for r in rows:
        out.append(f"\n{'PASS' if r['pass'] else 'FAIL'}  {r['id']}"
                   f"  ({r['about']})")
        out.append(f"  Q: {r['question']}")
        for line in (r["reply"] or "(no reply)").splitlines() or [""]:
            out.append(f"  A: {line}")
        out.append(f"  -> {r['why']}")
    passed = sum(bool(r["pass"]) for r in rows)
    out.append(f"\n{passed}/{len(rows)} passed")
    return "\n".join(out)


# --- the exam, put to a candidate in-process ----------------------------------
#
# POST /ask only reaches the model in config.json. These run the agent's own
# answer() against another endpoint, on a frozen copy of the database.
#
# dry_run stops the write in Tools.set_gen_thresholds and the send in
# Tools.send_telegram, and db_path keeps the action log off the live database.
# Neither covers the files beside the database: Guard.check runs ahead of the
# dry-run test, appends to data/audit.log, and can reach adopt_owner, which
# saves guard_state.json and topup_state.json. Each candidate therefore gets a
# copy of those files too, and config.DATA_DIR points at the copy while its
# Agent is built and questioned. Each gets its own copy of the database as well:
# an action one model provoked would otherwise reach the next model in its plan
# context.

SNAPSHOT_DIR = "/tmp/abexam"
SANDBOX_FILES = ("guard_state.json", "topup_state.json", "weather_archive.json")

# What answer() logs when it gives up on the model. It returns only the text.
UNGROUNDED_LOG = "answer was ungrounded"
STATUS_LOG = "model would not call a tool"
MODEL_ERROR_LOG = "model unavailable for question"

# "53,5 V" is 53.5 in Spanish. NUMBER_RE reads a comma as a thousands separator
# and would report it as the invented figure 535.
SPANISH_DECIMAL_RE = re.compile(r"(?<![\d,.])(\d{1,3}),(\d{1,2})(?![\d,])")


def make_snapshot(snapshot_dir=SNAPSHOT_DIR, source=None, fresh=False):
    """The session's frozen database. Taken once, so every model gets the same.

    Through SQLite's backup API, because the agent has the source open in WAL
    mode and a file copy of that can miss what is still in the log.
    """
    path = os.path.join(snapshot_dir, "history.sqlite")
    if os.path.exists(path) and not fresh:
        return path
    os.makedirs(snapshot_dir, exist_ok=True)
    tmp = path + ".tmp"
    src = history.readonly_connection(source or config.DB_PATH)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp, path)
    return path


def snapshot_warning(path, cfg, now=None):
    """Words like "last night" stop meaning the same thing past midnight."""
    now = int(now or time.time())
    taken = int(os.path.getmtime(path))
    if history.local_day(taken, cfg) == history.local_day(now, cfg):
        return None
    return (f"the snapshot was taken on {history.local_day(taken, cfg)} and it is "
            f"now {history.local_day(now, cfg)}: questions about yesterday and "
            "last night no longer cover what they did when it was taken")


def candidate_slug(label):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")


@contextlib.contextmanager
def sandboxed(directory):
    """Point everything rooted in DATA_DIR at `directory`, then put it back."""
    saved = (config.DATA_DIR, config.DB_PATH, config.AUDIT_LOG, weather.ARCHIVE_CACHE)
    config.DATA_DIR = directory
    config.DB_PATH = os.path.join(directory, "history.sqlite")
    config.AUDIT_LOG = os.path.join(directory, "audit.log")
    weather.ARCHIVE_CACHE = os.path.join(directory, "weather_archive.json")
    try:
        yield
    finally:
        config.DATA_DIR, config.DB_PATH, config.AUDIT_LOG, weather.ARCHIVE_CACHE = saved


def prepare_sandbox(candidate, snapshot, data_dir=None):
    """A private copy of the database and the state files for one candidate."""
    directory = os.path.join(os.path.dirname(snapshot), candidate_slug(candidate.label))
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory)
    shutil.copy(snapshot, os.path.join(directory, "history.sqlite"))
    for name in SANDBOX_FILES:
        live = os.path.join(data_dir or config.DATA_DIR, name)
        if os.path.exists(live):
            shutil.copy(live, os.path.join(directory, name))
    return directory


def build_exam_agent(candidate, cfg, directory):
    """An Agent on the candidate's endpoint. Call inside sandboxed(directory)."""
    import agent as agentmod    # the scheduler and the rest are only needed here

    class ExamAgent(agentmod.Agent):
        """Keeps the last Tools, since answer() returns only the text."""
        last_tools = None

        def tools(self, policy=None):
            self.last_tools = super().tools(policy=policy)
            return self.last_tools

    return ExamAgent(dict(copy.deepcopy(cfg), llm_url=candidate.url, llm_model=candidate.name),
                     dry_run=True, db_path=os.path.join(directory, "history.sqlite"))


class LogCatcher(logging.Handler):
    """What answer() logged while one question was being answered."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def saw(self, text):
        return any(text in m for m in self.messages)


def numbers_as_written(reply, lang):
    return SPANISH_DECIMAL_RE.sub(r"\1.\2", reply or "") if lang == "es" else (reply or "")


def ask_candidate(exam_agent, question, lang, cfg):
    """One question, and everything the report needs to know about the answer."""
    import agent as agentmod
    plan_question = agentmod.is_plan_question(question)
    context = exam_agent.plan_context() if not plan_question else None
    now_text = history.stamp(int(time.time()), cfg)
    catcher = LogCatcher()
    logger = logging.getLogger(agentmod.__name__)
    logger.addHandler(catcher)
    exam_agent.last_tools = None
    started = time.time()
    try:
        reply = exam_agent.answer(question, lang)
    finally:
        wall = round(time.time() - started, 1)
        logger.removeHandler(catcher)
    tools = exam_agent.last_tools
    calls = list(tools.calls) if tools else []
    if plan_question:
        answered_by = "python_plan"
    elif catcher.saw(MODEL_ERROR_LOG):
        answered_by = "model_error"
    elif catcher.saw(STATUS_LOG):
        answered_by = "python_status"
    else:
        answered_by = "model"
    # Figures Python wrote, or a fixed apology, say nothing about the model.
    unsourced = []
    if answered_by == "model":
        sources = [question, context or "", now_text]
        sources += [json.dumps(r, default=str) for _, _, r in calls]
        unsourced = unsourced_numbers(numbers_as_written(reply, lang), sources)
    return {"question": question, "lang": lang or "en", "reply": reply,
            "wall_s": wall, "answered_by": answered_by,
            "tool_calls": [{"name": n, "args": a} for n, a, _ in calls],
            "invalid_calls": invalid_calls(calls),
            "unsourced": unsourced,
            "ungrounded_retry": catcher.saw(UNGROUNDED_LOG),
            "status_fallback": answered_by == "python_status"}


def parse_questions(lines):
    """[(question, lang)] from a plain list. `es:` marks Spanish; # is a comment."""
    out = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text[:3].lower() == "es:":
            out.append((text[3:].strip(), "es"))
        else:
            out.append((text, None))
    return out


def exam_candidate(candidate, cfg, snapshot, repeat=1, only=None, questions=(),
                   pause=0, data_dir=None):
    """Every graded case `repeat` times, then each extra question once."""
    directory = prepare_sandbox(candidate, snapshot, data_dir)
    truth_conn = history.readonly_connection(snapshot)
    cases, extras = [], []
    first = [True]

    def wait():
        if pause and not first[0]:
            time.sleep(pause)
        first[0] = False

    try:
        with sandboxed(directory):
            exam_agent = build_exam_agent(candidate, cfg, directory)
            for case in eval_cases.CASES:
                if only and case["id"] not in only:
                    continue
                for attempt in range(1, repeat + 1):
                    wait()
                    row = ask_candidate(exam_agent, case["question"], None, cfg)
                    truth = case["truth"](truth_conn, cfg, int(time.time()))
                    passed, why = case["grade"](row["reply"], truth)
                    cases.append(dict(row, id=case["id"], about=case["about"],
                                      attempt=attempt, passed=bool(passed), why=why,
                                      truth=truth))
            for question, lang in questions:
                wait()
                extras.append(ask_candidate(exam_agent, question, lang, cfg))
    finally:
        truth_conn.close()
    return {"label": candidate.label, "url": candidate.url, "model": candidate.name,
            "snapshot": snapshot, "sandbox": directory, "cases": cases, "questions": extras}


def exam_summary(result):
    """The counts the report table is built from."""
    rows = result["cases"] + result["questions"]
    graded = [r for r in result["cases"] if r["answered_by"] != "python_plan"]
    model = [r for r in rows if r["answered_by"] in ("model", "python_status")]
    walls = [r["wall_s"] for r in model]
    return {"graded": len(graded), "passed": sum(r["passed"] for r in graded),
            "python_answered": sum(r["answered_by"] == "python_plan" for r in rows),
            "ungrounded_retries": sum(r["ungrounded_retry"] for r in rows),
            "status_fallbacks": sum(r["status_fallback"] for r in rows),
            "model_errors": sum(r["answered_by"] == "model_error" for r in rows),
            "unsourced": sum(len(r["unsourced"]) for r in rows),
            "invalid_calls": sum(len(r["invalid_calls"]) for r in rows),
            "write_attempts": sum(c["name"] == "set_gen_thresholds"
                                  for r in rows for c in r["tool_calls"]),
            "avg_wall_s": round(sum(walls) / len(walls), 1) if walls else None,
            "max_wall_s": max(walls) if walls else None,
            "avg_tool_calls": (round(sum(len(r["tool_calls"]) for r in model) / len(model), 2)
                               if model else None)}


def _answer_lines(row):
    calls = ", ".join(c["name"] for c in row["tool_calls"]) or "none"
    flags = [f for f, on in (("UNGROUNDED RETRY", row["ungrounded_retry"]),
                             ("ANSWERED FROM /data", row["status_fallback"]),
                             ("MODEL ERROR", row["answered_by"] == "model_error"),
                             ("PYTHON, NOT THE MODEL", row["answered_by"] == "python_plan")) if on]
    out = [f"  {row['wall_s']} s  tools: {calls}  unsourced: {row['unsourced'] or 'none'}"
           + (f"  [{', '.join(flags)}]" if flags else "")]
    out += [f"  A: {line}" for line in (row["reply"] or "(no reply)").splitlines() or [""]]
    return out


def exam_candidate_report(results):
    out = []
    for result in results:
        s = exam_summary(result)
        out += [f"\n=== {result['label']}", f"snapshot {result['snapshot']}"]
        seen = {}
        for r in result["cases"]:
            seen.setdefault(r["id"], []).append(r)
        for cid, rows in seen.items():
            python = all(r["answered_by"] == "python_plan" for r in rows)
            count = f"{sum(r['passed'] for r in rows)}/{len(rows)}"
            out.append(f"\n{cid}: {count}" + ("  (answered by Python, says nothing "
                                              "about the model)" if python else ""))
            out.append(f"  Q: {rows[0]['question']}")
            for r in rows:
                out.append(f"  {'PASS' if r['passed'] else 'FAIL'} #{r['attempt']}: {r['why']}")
                out += _answer_lines(r)
        for r in result["questions"]:
            out.append(f"\n[{r['lang']}] {r['question']}")
            out += _answer_lines(r)
        out += [
            f"\ngraded by model: {s['passed']}/{s['graded']}, "
            f"answered by Python: {s['python_answered']}",
            f"ungrounded retries {s['ungrounded_retries']}, /data fallbacks "
            f"{s['status_fallbacks']}, model errors {s['model_errors']}, unsourced "
            f"{s['unsourced']}, invalid calls {s['invalid_calls']}, threshold "
            f"proposals {s['write_attempts']} (dry run, nothing written)",
            f"wall avg {s['avg_wall_s']} s, max {s['max_wall_s']} s, "
            f"tool calls per answer {s['avg_tool_calls']}"]
    return "\n".join(out)


def run_candidate_exam(args, cfg):
    """--exam --candidate: nothing here touches the running agent or its files."""
    questions = []
    if args.questions:
        with open(args.questions, encoding="utf-8") as f:
            questions = parse_questions(f)
    snapshot = make_snapshot(fresh=args.fresh_snapshot)
    print(f"snapshot {snapshot}, taken "
          f"{history.stamp(int(os.path.getmtime(snapshot)), cfg)}", file=sys.stderr)
    warning = snapshot_warning(snapshot, cfg)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    results = []
    for spec in args.candidate:
        url, name = parse_candidate(spec)
        results.append(exam_candidate(
            Candidate(url, name or cfg["llm_model"]), cfg, snapshot, repeat=args.repeat,
            only=args.case or None, questions=questions, pause=args.pause))
    if args.json:
        print(json.dumps([dict(r, summary=exam_summary(r)) for r in results],
                         indent=1, default=str, ensure_ascii=False))
    else:
        print(exam_candidate_report(results))
    return 0


def parse_candidate(spec):
    """`url=name`, or just a url to reuse the configured model name."""
    url, _, name = spec.partition("=")
    return url, (name or None)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--ticks", type=int, default=20,
                    help="how many recorded ticks to replay (default 20)")
    ap.add_argument("--candidate", action="append", default=[], metavar="URL[=MODEL]",
                    help="an endpoint to score; repeatable. The configured "
                         "llm_url is always scored as the incumbent unless "
                         "--only-candidates is given.")
    ap.add_argument("--only-candidates", action="store_true",
                    help="skip the configured endpoint")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--rounds", type=int, default=4, help="tool-call budget")
    ap.add_argument("--json", action="store_true", help="emit the rows as JSON")
    ap.add_argument("--exam", action="store_true",
                    help="put the Q&A cases in eval_cases.py to the running "
                         "agent over POST /ask and mark them")
    ap.add_argument("--ask-url", help="the agent's /ask (default: the "
                                      "configured bind address and port)")
    ap.add_argument("--case", action="append", default=[],
                    help="run only these exam cases; repeatable")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="with --exam --candidate: ask each case N times")
    ap.add_argument("--questions", metavar="FILE",
                    help="with --exam --candidate: extra ungraded questions, one "
                         "per line, `es:` for Spanish, asked once each")
    ap.add_argument("--pause", type=float, default=0, metavar="SECONDS",
                    help="with --exam --candidate: wait between questions")
    ap.add_argument("--fresh-snapshot", action="store_true",
                    help=f"with --exam --candidate: replace {SNAPSHOT_DIR}/history.sqlite "
                         "instead of reusing it")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    exam_only = {"--repeat": args.repeat != 1, "--questions": args.questions,
                 "--pause": args.pause, "--fresh-snapshot": args.fresh_snapshot}
    if not (args.exam and args.candidate) and any(exam_only.values()):
        ap.error("only with --exam --candidate: "
                 + ", ".join(k for k, v in exam_only.items() if v))
    if args.repeat < 1:
        ap.error("--repeat must be at least 1")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")
    cfg = config.load()
    if args.exam and args.candidate:
        return run_candidate_exam(args, cfg)
    conn = history.connect()

    if args.exam:
        import ask_server
        url = args.ask_url or f"http://{ask_server.BIND_HOST}:{cfg['ask_port']}/ask"
        print(f"asking {url}")
        rows = exam(url, cfg, conn, timeout=args.timeout, only=args.case or None)
        if args.json:
            print(json.dumps(rows, indent=1, default=str))
        else:
            print(exam_report(rows))
        return 0 if all(r["pass"] for r in rows) else 1

    ticks = load_ticks(conn, args.ticks)
    if not ticks:
        print("No recorded prompts to replay. The agent stores one with every "
              "plan from this version on; run it for a few ticks first.")
        return 1

    candidates = []
    if not args.only_candidates:
        candidates.append(Candidate(cfg["llm_url"], cfg["llm_model"],
                                    label=f"{cfg['llm_model']} (live)"))
    for spec in args.candidate:
        url, name = parse_candidate(spec)
        candidates.append(Candidate(url, name or cfg["llm_model"]))

    # Progress is overwritten in place, which only works on a terminal; piped
    # to a file the carriage returns would run the lines together.
    live_output = sys.stdout.isatty()

    def progress(text):
        if live_output:
            print(text.ljust(78), end="\r", flush=True)

    print(f"replaying {len(ticks)} ticks, "
          f"{history.stamp(ticks[0].ts, cfg)} to {history.stamp(ticks[-1].ts, cfg)}\n")
    results = {}
    for cand in candidates:
        rows = []
        for i, tick in enumerate(ticks, 1):
            started = time.time()
            try:
                rows.append(replay(tick, cand, cfg, conn, rounds=args.rounds,
                                   timeout=args.timeout))
            except llmmod.LLMError as e:
                progress("")
                print(f"  {cand.label}: {e}")
                break
            progress(f"  {cand.label}: {i}/{len(ticks)} "
                     f"({time.time() - started:.0f}s)")
        progress("")
        results[cand.label] = rows

    if args.json:
        print(json.dumps(results, indent=1, default=str))
        return 0
    print()
    print(table(results))
    detail = faults(results, cfg)
    if detail:
        print("\nfaults" + detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
