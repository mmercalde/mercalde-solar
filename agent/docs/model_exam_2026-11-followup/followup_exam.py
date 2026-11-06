#!/usr/bin/env python
"""The follow-up exam on the live server, in pieces.

model_eval.py --exam --repeat 3 --pause 30 --fresh-snapshot, same order, same
functions, same grading. What differs: every answer is written as it arrives, so
a stop loses nothing and a later run carries on from the same frozen snapshot.
It waits for the server to be idle before each question, and stops asking at
--stop-asking or if the real agent logs "model unavailable".
Heat is logged by the flight recorder and stops nothing.
"""
import argparse, json, os, subprocess, sys, time
import requests
REPO = os.path.expanduser("~/mercalde-solar/agent")
sys.path.insert(0, REPO)
os.chdir(os.path.dirname(REPO))
import config, eval_cases, history, model_eval
from model_eval import Candidate

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True)
ap.add_argument("--url", required=True)
ap.add_argument("--name", default="gemma4")
ap.add_argument("--stop-asking", type=int, required=True, help="epoch seconds")
ap.add_argument("--pause", type=float, default=30)
ap.add_argument("--repeat", type=int, default=3)
ap.add_argument("--questions", default="agent/docs/exam_questions_2026-11.txt")
ap.add_argument("--limit", type=int, default=0, help="dry runs: ask at most this many")
args = ap.parse_args()

rows_path = os.path.join(args.dir, "rows.jsonl")
os.makedirs(args.dir, exist_ok=True)
def say(*a):
    with open(os.path.join(args.dir, "events.log"), "a") as f:
        print(time.strftime("%F %T"), *a, file=f, flush=True)

done = {}
if os.path.exists(rows_path):
    for line in open(rows_path, encoding="utf-8"):
        r = json.loads(line)
        done[r["key"]] = r

cfg = config.load()
snapshot = model_eval.make_snapshot(args.dir)          # kept if it is already there
warning = model_eval.snapshot_warning(snapshot, cfg)
candidate = Candidate(args.url, args.name)
directory = model_eval.prepare_sandbox(candidate, snapshot)
truth_conn = history.readonly_connection(snapshot)
with open(args.questions, encoding="utf-8") as f:
    questions = model_eval.parse_questions(f)
plan = [("case", c, n) for c in eval_cases.CASES for n in range(1, args.repeat + 1)]
plan += [("question", q, 0) for q in questions]
started = time.strftime("%F %T")
say(f"run start: {len(done)} of {len(plan)} already done; snapshot {snapshot}; warning {warning}")


def agent_said_unavailable():
    out = subprocess.run(["journalctl", "-u", "solar-agent", "--since", started, "--no-pager",
                          "--grep", "model unavailable"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "model unavailable" in l]


def wait_for_idle():
    base = args.url.split("/v1/")[0]
    began = time.time()
    while time.time() < args.stop_asking:
        try:
            slots = requests.get(base + "/slots", timeout=5).json()
        except (requests.RequestException, ValueError):
            break                     # the dry run's closed port, or a server to report
        if not any(s.get("is_processing") for s in slots):
            break
        time.sleep(5)
    return int(time.time() - began)


why, asked, first = "finished", 0, True
with model_eval.sandboxed(directory):
    exam_agent = model_eval.build_exam_agent(candidate, cfg, directory)
    for kind, item, attempt in plan:
        key = (f"case:{item['id']}:{attempt}" if kind == "case" else f"q:{item[1] or 'en'}:{item[0]}")
        if key in done:
            continue
        if args.limit and asked >= args.limit:
            why = "limit"; break
        if not first:
            time.sleep(args.pause)
        first = False
        # Never start on top of a real request: on 2026-11-05 a question that began
        # during the owner's 12,000-token "check the log" filled the shared pool.
        waited = wait_for_idle()
        if waited >= 5:
            say(f"waited {waited} s for the server to be idle")
        if time.time() >= args.stop_asking:
            why = "reached the stop time"; break
        bad = agent_said_unavailable()
        if bad:
            why = f"the real agent logged: {bad[-1]}"; break
        if kind == "case":
            row = model_eval.ask_candidate(exam_agent, item["question"], item.get("lang"), cfg)
            truth = item["truth"](truth_conn, cfg, int(time.time()))
            passed, grade = item["grade"](row["reply"], truth)
            row = dict(row, id=item["id"], about=item["about"], attempt=attempt,
                       passed=bool(passed), why=grade, truth=truth)
        else:
            row = model_eval.ask_candidate(exam_agent, item[0], item[1], cfg)
        row["key"], row["kind"] = key, kind
        done[key] = row
        with open(rows_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
        asked += 1
        say(f"{len(done)}/{len(plan)} {key[:60]} {row['wall_s']} s {row['answered_by']}"
            + (f" {'PASS' if row['passed'] else 'FAIL'}" if kind == "case" else ""))
truth_conn.close()

rows = list(done.values())
result = {"label": candidate.label, "url": candidate.url, "model": candidate.name,
          "snapshot": snapshot, "sandbox": directory,
          "cases": [r for r in rows if r["kind"] == "case"],
          "questions": [r for r in rows if r["kind"] == "question"]}
with open(os.path.join(args.dir, "result.json"), "w", encoding="utf-8") as f:
    json.dump([dict(result, summary=model_eval.exam_summary(result))], f, indent=1,
              default=str, ensure_ascii=False)
with open(os.path.join(args.dir, "report.txt"), "w", encoding="utf-8") as f:
    f.write(model_eval.exam_candidate_report([result]) + "\n")
say(f"run end: {why}; {len(done)} of {len(plan)} done")
