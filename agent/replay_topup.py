#!/usr/bin/env python3
"""Replay a night's samples through the top-up state machine and the guard.

No model, no network, no writes. It walks the minute samples of a night that
has already happened, moves the state machine on what the generators actually
did, evaluates POLICY 4 at the same moments the live agent evaluated it, and
puts anything that fires through the guard - printing every transition and
every write it would have made.

    agent/venv/bin/python agent/replay_topup.py \\
        --from "2026-08-30 19:00" --to "2026-08-31 00:00"

The clock is honest about the future. The load model is built with `as_of`,
so what the pack holds and what the charging curves know are read at the tick
being replayed and not from the rest of the night, and the weather is the
forecast recorded in that tick's own plan record rather than today's.

`--owner-writes` takes a JSON list of `/config` writes the agent did not
make, each `{"at": "2026-08-30 21:12", "mep_stop": 56.6}`, applied to the
simulated dashboard at that minute. Without it the replay shows what the
agent alone would have done; with it, what it would have done against an
owner who was also at the dashboard.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfgmod                                    # noqa: E402
import guard as guardmod                                   # noqa: E402
import history                                             # noqa: E402
import loadmodel                                           # noqa: E402
import policy as policymod                                 # noqa: E402
import sun as sunmod                                       # noqa: E402
import tools as toolsmod                                   # noqa: E402
import topup as topupmod                                   # noqa: E402
import weather                                             # noqa: E402

GEN_KEYS = {"mep": ("mep_start", "mep_stop", "mep803a", "mep803aAction",
                    "mep803aMode", "mepAgsOnline"),
            "kubota": ("kub_start", "kub_stop", "kubota", "kubotaAction",
                       "kubotaMode", "kubotaAgsOnline")}

# Both wordings: records written before the rename say "forecast tomorrow",
# records written after say "next daylight (Fri Sep 4)". A replay reads
# nights from either era.
CLOUD_RE = re.compile(r"(?:forecast tomorrow|next daylight \([^)]*\)): "
                      r"(\d+)% cloud")


def parse_when(text, cfg):
    tz = history.tzinfo(cfg)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=tz).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"could not read a date and time from {text!r}")


def as_data(row, live):
    """A `/data` dict, as the dashboard would have answered that minute."""
    return {
        "batteryVoltage": row["battery_v"], "battSocBM": row["batt_soc"],
        "battPower": row["batt_power"], "battCurrent": row["batt_current"],
        "battMonitorOnline": bool(row["batt_monitor_online"]),
        "acPower1": row["ac_power1"], "acPower2": row["ac_power2"],
        "mppt80PVPower": row["mppt80_pv"], "southArrayPVPower": row["south_pv"],
        "westArrayPVPower": row["west_pv"],
        "mep803aAction": row["mep_action"], "kubotaAction": row["kub_action"],
        "mep803aMode": row["mep_mode"], "kubotaMode": row["kub_mode"],
        "mepAgsOnline": row["mep_ags_online"],
        "kubotaAgsOnline": row["kub_ags_online"],
        "pollErrors": row["poll_errors"],
        "autoGenEnabled": bool(row["auto_gen_enabled"]),
        "clockTime": history.local(row["ts"], live["cfg"]).strftime("%H:%M:%S"),
        "lastUpdate": "0:00:00",
    }


def as_config(t):
    """A `/config` payload from four thresholds."""
    return {"mep803a": {"startVoltage": t["mep_start"],
                        "stopVoltage": t["mep_stop"],
                        "maxRuntime": 120, "chargeRate": 100, "cooldown": 5},
            "kubota": {"startVoltage": t["kub_start"],
                       "stopVoltage": t["kub_stop"],
                       "maxRuntime": 120, "chargeRate": 70, "cooldown": 5}}


class Replay:
    def __init__(self, cfg, db, start, end, owner_writes=None, workdir=None):
        self.cfg = cfg
        self.start, self.end = start, end
        self.conn = history.connect(db)
        self.model = loadmodel.LoadModel(self.conn, cfg, as_of=True)
        self.dir = workdir or tempfile.mkdtemp(prefix="replay-")
        self.topup = topupmod.TopUp(cfg, path=os.path.join(self.dir, "topup.json"))
        self.guard = guardmod.Guard(self.conn, cfg, model=self.model,
                                    topup=self.topup,
                                    state_path=os.path.join(self.dir, "guard.json"))
        self.owner_writes = sorted(owner_writes or [], key=lambda w: w["at"])
        self.live = None
        self.transitions, self.writes, self.owner_events = [], [], []
        self.clouds = self._recorded_clouds()
        # Read before anything runs. The guard audits into this same copy of
        # the database, so afterwards the replay's own decisions would be
        # sitting in the table it compares itself against.
        self.real_writes = self.recorded_writes()
        self.diverged_at = None

    # --- what the night recorded -------------------------------------------

    def _recorded_clouds(self):
        """The coming daylight's cloud cover, as each tick's own record had it.

        POLICY 3 is the only rule that reads it, and today's forecast has
        nothing to say about a night in the past.
        """
        out = {}
        for r in self.conn.execute(
                "SELECT ts, text FROM plans WHERE ts BETWEEN ? AND ?",
                (self.start - 3600, self.end + 3600)):
            m = CLOUD_RE.search(r["text"] or "")
            if m:
                out[r["ts"]] = int(m.group(1))
        return out

    def cloud_at(self, ts):
        if not self.clouds:
            return None
        return self.clouds[min(self.clouds, key=lambda t: abs(t - ts))]

    def tick_times(self):
        """The moments the live agent evaluated, so this is like for like."""
        rows = [r["ts"] for r in self.conn.execute(
            "SELECT ts FROM plans WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (self.start, self.end))]
        if rows:
            return rows
        step = self.cfg["tick_minutes"] * 60
        return list(range(self.start, self.end, step))

    def samples(self):
        return self.conn.execute(
            "SELECT * FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (self.start, self.end))

    def recorded_writes(self):
        """What the agent actually wrote that night, from the actions table."""
        out = []
        for r in self.conn.execute(
                "SELECT ts, args FROM actions WHERE tool='set_gen_thresholds' "
                "AND allowed=1 AND ts BETWEEN ? AND ? ORDER BY ts",
                (self.start, self.end)):
            out.append((r["ts"], json.loads(r["args"])))
        return out

    # --- the replay ---------------------------------------------------------

    def observations(self, data, live_cfg, ts):
        out = {}
        for gen, (_, _, cfg_key, act, mode, online) in GEN_KEYS.items():
            out[gen] = {
                "action": data.get(act), "mode": data.get(mode),
                "ags_online": data.get(online),
                "auto_gen_enabled": data.get("autoGenEnabled"),
                "voltage": data.get("batteryVoltage"),
                "stop_v": live_cfg[cfg_key]["stopVoltage"],
                "cap_minutes": min(live_cfg[cfg_key]["maxRuntime"],
                                   self.cfg["ags_max_run_hours"][gen] * 60),
                "run": self.latest_run(gen, ts),
            }
        return out

    def latest_run(self, gen, ts):
        row = self.conn.execute(
            "SELECT * FROM gen_runs WHERE gen=? AND stop_ts IS NOT NULL "
            "AND stop_ts <= ? ORDER BY stop_ts DESC LIMIT 1",
            (gen, ts)).fetchone()
        return dict(row) if row is not None else None

    def facts(self, ts, data, live_cfg):
        sunrise = sunmod.next_sunrise(self.cfg, now=ts)
        day = history.local_day(ts, self.cfg)
        sunset = (sunmod.times(self.cfg, day) or (None, None))[1]
        run_window = {}
        for gen, (_, _, cfg_key, *_rest) in GEN_KEYS.items():
            run_window[gen] = min(live_cfg[cfg_key]["maxRuntime"] / 60.0,
                                  self.cfg["ags_max_run_hours"][gen])
        return {
            "now": ts, "data": data, "config": live_cfg,
            "voltage": data.get("batteryVoltage"), "soc": data.get("battSocBM"),
            "sunrise_ts": sunrise, "sunset_ts": sunset,
            "peak_today": history.solar_peak(self.conn, self.cfg, day, now=ts),
            "remaining_solar_wh": self.model.remaining_solar_wh(now=ts),
            "projection": self.model.project_voltage(52.0, now=ts),
            "deficit": self.model.overnight_deficit(sunrise, now=ts),
            "next_daylight_cloud": self.cloud_at(ts),
            "next_daylight_date": history.local_day(sunrise, self.cfg)
                                  if sunrise else None,
            "thresholds": toolsmod.thresholds_from_config(live_cfg),
            "baseline": self.guard.baseline(),
            "run_window_h": run_window,
            "topup": self.topup.snapshot(),
        }

    def apply_owner_writes(self, ts):
        """Anything the owner did to /config at or before this minute."""
        while self.owner_writes and self.owner_writes[0]["at"] <= ts:
            w = self.owner_writes.pop(0)
            before = dict(self.live)
            for key in ("mep_start", "mep_stop", "kub_start", "kub_stop"):
                if key in w:
                    self.live[key] = float(w[key])
            self.owner_events.append((ts, w.get("note", ""), before,
                                      dict(self.live)))

    def run(self):
        rows = list(self.samples())
        if not rows:
            raise SystemExit("no samples in that window")
        ticks = set(self.tick_times())
        first = rows[0]
        self.live = {"mep_start": None}
        # The thresholds in force when the window opens, taken from the last
        # write before it or from the config defaults.
        self.live = self.opening_thresholds()
        self.guard.adopt_live(dict(self.live), now=first["ts"])
        self.topup.roll(first["ts"])
        self.cfg_holder = {"cfg": self.cfg}

        for row in rows:
            ts = row["ts"]
            self.apply_owner_writes(ts)
            data = as_data(row, self.cfg_holder)
            live_cfg = as_config(self.live)
            # The same question the agent asks every tick: are these
            # thresholds this agent's own, or did the owner move them?
            self.guard.adopt_live(dict(self.live), now=ts)
            self.drain(ts)
            # Every minute, so `running` is seen when it happens and the five
            # minute start timeout is a real five minutes.
            self.topup.advance(self.observations(data, live_cfg, ts), ts)
            self.drain(ts)
            if ts in ticks:
                self.tick(ts, data, live_cfg)
                self.drain(ts)
        return self

    def drain(self, ts):
        """Take every move the machine has made since last asked.

        The guard moves it too - a raised start becomes `requested`, an
        owner's edit becomes `stopped_by_owner` - so the transitions are
        collected from the machine itself rather than from advance() alone.
        """
        while self.topup.moves:
            self.transitions.append((ts, self.topup.moves.pop(0)))

    def opening_thresholds(self):
        """What `/config` actually held when the window opened.

        The first plan record in the window carries the thresholds the agent
        read that tick, which is the only record of the dashboard's own state
        - `samples` does not keep it. Failing that, the agent's last write
        before the window, and failing that the config defaults.
        """
        row = self.conn.execute(
            "SELECT data FROM plans WHERE ts >= ? ORDER BY ts LIMIT 1",
            (self.start,)).fetchone()
        if row:
            try:
                t = (json.loads(row["data"] or "{}") or {}).get("thresholds")
            except json.JSONDecodeError:
                t = None
            if t:
                return {k: float(t[k]) for k in ("mep_start", "mep_stop",
                                                 "kub_start", "kub_stop")}
        row = self.conn.execute(
            "SELECT args FROM actions WHERE tool='set_gen_thresholds' "
            "AND allowed=1 AND ts < ? ORDER BY ts DESC LIMIT 1",
            (self.start,)).fetchone()
        if row:
            a = json.loads(row["args"])
            return {k: float(a[k]) for k in ("mep_start", "mep_stop",
                                             "kub_start", "kub_stop")}
        return {"mep_start": self.cfg["default_start"],
                "mep_stop": self.cfg["default_stop"],
                "kub_start": self.cfg["default_start"],
                "kub_stop": self.cfg["default_stop"]}

    def recorded_thresholds(self, ts):
        row = self.conn.execute(
            "SELECT data FROM plans WHERE ts=? LIMIT 1", (ts,)).fetchone()
        if not row:
            return None
        try:
            return (json.loads(row["data"] or "{}") or {}).get("thresholds")
        except json.JSONDecodeError:
            return None

    def note_divergence(self, ts):
        """The first tick where this replay is no longer the night that ran.

        Everything after it is a counterfactual: the samples record what the
        generators did under the thresholds that were really in force, and
        once the replay sets different ones there is no evidence left for
        what would have happened. The state machine is still driven by the
        recorded `*Action`, so a generator the replay asked for and the night
        never started reads as `failed_to_start`, which is an artefact of the
        replay and not a finding about the plant.
        """
        if self.diverged_at is not None:
            return
        was = self.recorded_thresholds(ts)
        if was and any(abs(float(was[k]) - self.live[k]) >= 0.05
                       for k in self.live):
            self.diverged_at = ts

    def return_raised_starts(self, ts, data, live_cfg):
        """The agent's own write: a raised start comes back once it is spent.

        The same rule agent.py applies - every state past `requested` means
        the start has done all it will do - so the replay shows this write
        too. It is the one the night could never make, because the agent had
        adopted its own raised start as the owner's baseline.
        """
        raised = self.guard.raised_starts()
        if not raised:
            return
        base = self.guard.baseline()
        want = dict(self.live)
        done = []
        for gen in list(raised):
            skey = "mep_start" if gen == "mep" else "kub_start"
            if self.topup.status(gen) in (topupmod.IDLE, topupmod.REQUESTED):
                continue
            back = max(base[skey], guardmod.HARD_START_FLOOR)
            if back > want[skey] - 0.05:
                self.guard.clear_raised(gen)
                continue
            want[skey] = back
            done.append(gen)
        if not done:
            return
        reason = "; ".join(f"{g} is {self.topup.status(g)}, so its start "
                           f"returns to the baseline" for g in done)
        allowed, why = self.guard.check(
            want["mep_start"], want["mep_stop"], want["kub_start"],
            want["kub_stop"], reason, now=ts,
            status={"data": data, "config": live_cfg}, policy=None)
        if not allowed:
            self.writes.append((ts, None, {"rule": 4, "name": "housekeeping",
                                           "detail": reason}, why, []))
            return
        written = dict(self.guard.last_check["values"])
        self.live.update(written)
        self.guard.note_write(written, now=ts, housekeeping=True)
        for gen in done:
            self.guard.clear_raised(gen)
        self.writes.append((ts, written, {"rule": 4, "name": "housekeeping",
                                          "detail": reason}, why, []))

    def tick(self, ts, data, live_cfg):
        self.note_divergence(ts)
        self.return_raised_starts(ts, data, live_cfg)
        live_cfg = as_config(self.live)
        facts = self.facts(ts, data, live_cfg)
        rules = policymod.evaluate(self.cfg, facts, self.model)
        facts["policy"] = rules
        fired = policymod.firing(rules)
        if not fired:
            self.writes.append((ts, None, None, "no rule fires", rules))
            return
        rule = fired[0]
        p = rule["proposal"]
        if not p:
            return
        allowed, why = self.guard.check(
            p["mep_start"], p["mep_stop"], p["kub_start"], p["kub_stop"],
            f"POLICY {rule['rule']} {rule['name']}", now=ts,
            status={"data": data, "config": live_cfg}, policy=rules)
        if allowed:
            written = dict(self.guard.last_check["values"])
            self.live.update(written)
            self.guard.note_write(written, now=ts)
            self.writes.append((ts, written, rule, why, rules))
        else:
            self.writes.append((ts, None, rule, why, rules))

    # --- what it says -------------------------------------------------------

    def report(self, out=sys.stdout):
        w = out.write
        clock = lambda t: history.clock(t, self.cfg)          # noqa: E731

        w("\n=== owner writes injected ===\n")
        if not self.owner_events:
            w("  none\n")
        for ts, note, before, after in self.owner_events:
            moved = ", ".join(f"{k} {before[k]}→{after[k]}"
                              for k in sorted(after) if before[k] != after[k])
            w(f"  {clock(ts)}  {moved or 'no threshold changed'}"
              f"{'  — ' + note if note else ''}\n")

        if self.diverged_at is not None:
            w(f"\nEverything from {clock(self.diverged_at)} is a "
              f"counterfactual: from there the replay's thresholds are not "
              f"the ones the night ran under, and the recorded generator\n"
              f"actions are evidence about those, not about these. A "
              f"generator the replay asked for and the night never started "
              f"reads as failed_to_start for that reason alone.\n")

        w("\n=== state transitions ===\n")
        if not self.transitions:
            w("  none\n")
        for ts, m in self.transitions:
            w(f"  {clock(ts)}  {m['gen']:<7} {m['from']} → {m['to']}\n")
            w(f"            {m['why']}\n")

        w("\n=== what the replay would have written ===\n")
        wrote = [x for x in self.writes if x[1]]
        if not wrote:
            w("  nothing\n")
        for ts, values, rule, why, _rules in wrote:
            w(f"  {clock(ts)}  MEP {values['mep_start']}/{values['mep_stop']}, "
              f"Kubota {values['kub_start']}/{values['kub_stop']}\n")
            w(f"            {rule['detail']}\n")

        w("\n=== every tick ===\n")
        for ts, values, rule, why, rules in self.writes:
            four = next((r for r in rules if r["rule"] == 4), None)
            verdict = ("WROTE" if values else
                       "fired, refused" if rule else
                       "held" if four and four.get("held") else "no")
            w(f"  {clock(ts)}  {verdict}\n")
            if four:
                w(f"            POLICY 4: {four['detail'][:400]}\n")
            if rule and not values:
                w(f"            guard: {why}\n")

        w("\n=== against the night as it happened ===\n")
        real = self.real_writes
        w(f"  the agent wrote {len(real)} time(s); this replay writes "
          f"{len(wrote)}\n")
        for ts, a in real:
            w(f"    was  {clock(ts)}  MEP {a['mep_start']}/{a['mep_stop']}, "
              f"Kubota {a['kub_start']}/{a['kub_stop']}\n")
        for ts, values, *_ in wrote:
            w(f"    now  {clock(ts)}  MEP {values['mep_start']}/{values['mep_stop']}, "
              f"Kubota {values['kub_start']}/{values['kub_stop']}\n")
        return self


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="history.sqlite to replay")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--owner-writes", default=None,
                    help="JSON list of /config writes the agent did not make")
    ap.add_argument("--config", default=None,
                    help="the config.json the night actually ran under; the "
                         "local one otherwise. It matters: a replay under a "
                         "different learning_live_days is answering a "
                         "different question")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config or
                      (cfgmod.EXAMPLE_PATH
                       if not os.path.exists(cfgmod.CONFIG_PATH) else None))
    db = args.db or cfgmod.DB_PATH
    owner = None
    if args.owner_writes:
        with open(args.owner_writes) as f:
            owner = [dict(w, at=parse_when(w["at"], cfg)) for w in json.load(f)]

    # A copy, so nothing here can touch the night it is reading.
    tmp = tempfile.mkdtemp(prefix="replay-")
    copy = os.path.join(tmp, "history.sqlite")
    shutil.copy(db, copy)
    # No network. Overnight the forecast contributes no solar to the deficit
    # walk anyway, and tomorrow's cloud comes from each tick's own plan
    # record rather than from today's forecast, which has nothing to say
    # about a night in the past.
    weather.hourly = lambda *a, **k: []
    weather.summary = lambda *a, **k: {}
    if history.local(parse_when(args.end, cfg), cfg).hour not in range(0, 8) \
            and history.local(parse_when(args.start, cfg), cfg).hour < 18:
        print("warning: this window includes daylight, and the replay counts "
              "no forecast solar in it", file=sys.stderr)
    Replay(cfg, copy, parse_when(args.start, cfg), parse_when(args.end, cfg),
           owner_writes=owner, workdir=tmp).run().report()


if __name__ == "__main__":
    main()
