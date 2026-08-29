"""Deterministic guard. The model proposes; this decides.

Every write passes check(). A refusal returns a reason string, which the tool
hands straight back to the model so it can adjust or explain. The nine rules
are SPEC section 7 and are not model-editable.

Note on rule 7 (stale data): SPEC names /data's `lastUpdate` as "timestamp of
the snapshot", but pi5/app.py line 868 documents that key as *uptime* —
"lastUpdate is really uptime; kept as-is for the ESP32 display and Alexa
webhook. clockTime is the actual wall clock of this poll." Keying staleness
off uptime would never fire, so the rule is implemented against `clockTime`,
which carries the meaning the rule intends. `lastUpdate` is still recorded.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import config
import history
import loadmodel
import policy as policymod

log = logging.getLogger(__name__)

MIN_STOP_MINUS_START = 2.0
STALE_SECONDS = 300
RATE_LIMIT_SECONDS = 3600
OWNER_OVERRIDE_SECONDS = 6 * 3600
# The Pi5 watchdog reads liveness from GET /plan and resets after 6 hours of
# silence, so it needs nothing finer than this. At the 15-minute tick the
# heartbeat wrote "Config updated" to the Pi5 event log 96 times a day.
HEARTBEAT_SECONDS = 3600
# Thresholds are written to one decimal, so anything under this is the same value.
EPS = 0.05

GEN_KEYS = (("mep", "mep_start", "mep_stop", "mep803a"),
            ("kubota", "kub_start", "kub_stop", "kubota"))

# "Returned to default" and its cousins. A reason that says only this explains
# nothing: it names a destination, not a cause.
RESTORE_DEFAULT_RE = re.compile(
    r"\b(return(?:ing|ed|s)?|restor(?:e|es|ing|ed)|revert(?:ing|ed|s)?|back)\b"
    r"[^.;]{0,40}?\bdefaults?\b", re.IGNORECASE)


class Guard:
    def __init__(self, conn, cfg, model=None, state_path=None):
        self._conn = conn
        self.cfg = cfg
        self.model = model or loadmodel.LoadModel(conn, cfg)
        self.state_path = state_path or os.path.join(config.DATA_DIR, "guard_state.json")
        self.state = self._load_state()

    @property
    def conn(self):
        return history.resolve(self._conn)

    # --- persisted state ----------------------------------------------------

    def _load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"intended": None, "last_write_ts": 0,
                    "last_heartbeat_ts": 0, "override_until": 0,
                    "override_adopted": None, "owner_baseline": None}

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def note_write(self, applied, now=None):
        """Record what the dashboard reports after a successful write.

        Reading the values back means a Pi5 clamp cannot later be mistaken for
        the owner editing the thresholds by hand.
        """
        self.state["intended"] = dict(applied)
        self.state["last_write_ts"] = int(now or time.time())
        self._save_state()

    def note_heartbeat(self, applied, now=None):
        """Record a heartbeat re-send.

        Deliberately does not touch last_write_ts. The heartbeat is not a
        change, and rule 5 measures the time since the agent last actually
        moved the thresholds. Setting that clock from the heartbeat meant
        every model write was refused as "last write was N minutes ago" for as
        long as the heartbeat kept running - which is why the only write of
        the first live night landed at 04:10, in the one window where the
        owner stand-down had held the heartbeat back.
        """
        self.state["intended"] = dict(applied)
        self.state["last_heartbeat_ts"] = int(now or time.time())
        self._save_state()

    def intended(self):
        """Thresholds the agent means to be in force; defaults until it writes."""
        if self.state.get("intended"):
            return dict(self.state["intended"])
        return self._config_defaults()

    def _config_defaults(self):
        return {"mep_start": self.cfg["default_start"],
                "mep_stop": self.cfg["default_stop"],
                "kub_start": self.cfg["default_start"],
                "kub_stop": self.cfg["default_stop"]}

    def owner_baseline(self):
        """The thresholds the owner last set by hand, if they ever have."""
        b = self.state.get("owner_baseline")
        return dict(b) if b else None

    def baseline(self):
        """The values a change returns to once its reason has passed.

        POLICY 6 says to return to default. Once the owner has set the
        thresholds themselves, theirs are the default; config's are not. The
        6-hour stand-down expiring does not repeal the owner's decision, it
        only ends the pause - which is what went wrong at 04:10 on the first
        live night, when the agent wrote the owner's 55.0 stops back to 56.0
        with the reason "returned to default".
        """
        return self.owner_baseline() or self._config_defaults()

    # --- helpers ------------------------------------------------------------

    def _status(self, status=None):
        if status is not None:
            return status
        return {"data": history.fetch_data(self.cfg),
                "config": history.fetch_config(self.cfg)}

    @staticmethod
    def _live_thresholds(live):
        return {"mep_start": live["mep803a"]["startVoltage"],
                "mep_stop": live["mep803a"]["stopVoltage"],
                "kub_start": live["kubota"]["startVoltage"],
                "kub_stop": live["kubota"]["stopVoltage"]}

    @staticmethod
    def _same(a, b):
        return all(abs(a[k] - b[k]) < EPS for k in
                   ("mep_start", "mep_stop", "kub_start", "kub_stop"))

    def _clock_age(self, data, now):
        """Seconds since the dashboard's poll, from clockTime. None if unusable."""
        stamp = data.get("clockTime")
        if not stamp or ":" not in stamp or stamp.startswith("-"):
            return None
        try:
            h, m, s = (int(x) for x in stamp.split(":"))
        except ValueError:
            return None
        local_now = history.local(now, self.cfg)
        poll = local_now.replace(hour=h, minute=m, second=s, microsecond=0)
        # clockTime carries no date, so a poll just before midnight read just
        # after it looks like it is in the future.
        if poll > local_now + timedelta(seconds=60):
            poll -= timedelta(days=1)
        return (local_now - poll).total_seconds()

    # --- the rules ----------------------------------------------------------

    def check(self, mep_start, mep_stop, kub_start, kub_stop, reason,
              now=None, status=None, policy=None):
        """Return (allowed, reason). Always audited, pass or refuse.

        `policy` is this tick's rule evaluation from policy.py. Rule 8 needs
        it to tell a rule-driven change from a drift back to config defaults.
        None means no rule is known to fire, which is the safe reading.
        """
        now = int(now or time.time())
        want = {"mep_start": float(mep_start), "mep_stop": float(mep_stop),
                "kub_start": float(kub_start), "kub_stop": float(kub_stop)}
        args = dict(want, reason=reason)
        try:
            st = self._status(status)
        except Exception as e:                      # noqa: BLE001
            return self._audit(args, False, f"cannot read the dashboard: {e}",
                               None, None, now)
        data, live = st["data"], st["config"]
        v = data.get("batteryVoltage")
        soc = data.get("battSocBM")

        allowed, why = self._evaluate(want, data, live, v, soc, now, reason, policy)
        return self._audit(args, allowed, why, v, soc, now)

    def _evaluate(self, want, data, live, v, soc, now, reason="", policy=None):
        # Rule 7: stale data. Nothing below can be trusted without this.
        if not data.get("battMonitorOnline"):
            return False, ("battery monitor is offline, so state of charge and "
                           "battery power are unknown; no write")
        age = self._clock_age(data, now)
        if age is None:
            return False, "dashboard reported no usable clockTime; no write"
        if age > STALE_SECONDS:
            return False, (f"dashboard data is {int(age)}s old "
                           f"(limit {STALE_SECONDS}s); no write")
        if v is None:
            return False, "dashboard reported no battery voltage; no write"

        # Rule 6: learning gate.
        gate = self.model.learning_status(now=now)
        if not gate["open"]:
            missing = []
            if not gate["has_prior_year"]:
                missing.append("no history for this calendar month from a prior year")
            if not gate["has_live_days"]:
                missing.append(f"only {gate['live_days']} consecutive days of live "
                               f"samples, need {gate['live_days_required']}")
            return False, "learning phase: " + "; ".join(missing)

        # Rule 8: owner override.
        live_now = self._live_thresholds(live)
        if self.state.get("intended") and not self._same(live_now, self.state["intended"]):
            self.state["override_until"] = now + OWNER_OVERRIDE_SECONDS
            self.state["override_adopted"] = live_now
            self.state["owner_baseline"] = dict(live_now)
            self.state["intended"] = dict(live_now)
            self._save_state()
            log.warning("owner changed thresholds by hand; adopting %s and "
                        "standing down for 6 h", live_now)
            return False, (
                "the owner changed the thresholds in the dashboard "
                f"(now MEP {live_now['mep_start']}/{live_now['mep_stop']}, "
                f"Kubota {live_now['kub_start']}/{live_now['kub_stop']}). "
                "Adopting those values and making no writes for 6 hours")
        if now < self.state.get("override_until", 0):
            mins = int((self.state["override_until"] - now) / 60)
            return False, (f"standing down after an owner threshold change; "
                           f"{mins} minutes left")

        # Rule 8, second half: the owner's values are the baseline once they
        # have set them. The stand-down expiring ends the pause, not their
        # decision. Only a computed POLICY rule may move off that baseline,
        # and when its reason has passed the return is to the baseline, not to
        # config's defaults.
        fired = policymod.firing(policy or [])
        owner = self.owner_baseline()
        if RESTORE_DEFAULT_RE.search(reason or "") and not fired:
            back_to = self.baseline()
            if not self._same(want, back_to):
                return False, (
                    f"\"restore default\" is not a reason on its own, and "
                    f"{want['mep_start']}/{want['mep_stop']}, "
                    f"{want['kub_start']}/{want['kub_stop']} are not the values "
                    f"to return to; those are MEP {back_to['mep_start']}/"
                    f"{back_to['mep_stop']}, Kubota {back_to['kub_start']}/"
                    f"{back_to['kub_stop']}")
        if owner and not self._same(want, owner) and not fired:
            return False, (
                f"the owner set MEP {owner['mep_start']}/{owner['mep_stop']}, "
                f"Kubota {owner['kub_start']}/{owner['kub_stop']} by hand, and "
                f"those are the baseline. Only a POLICY rule that fires may "
                f"move them, and none fires; the agent may always return to "
                f"them")

        # Rule 1: bounds.
        smin, smax = self.cfg["start_voltage_min"], self.cfg["start_voltage_max"]
        pmin, pmax = self.cfg["stop_voltage_min"], self.cfg["stop_voltage_max"]
        for gen, skey, pkey, _ in GEN_KEYS:
            if not smin - EPS <= want[skey] <= smax + EPS:
                return False, (f"{gen} start {want[skey]} is outside the permitted "
                               f"{smin}-{smax} V")
            if not pmin - EPS <= want[pkey] <= pmax + EPS:
                return False, (f"{gen} stop {want[pkey]} is outside the permitted "
                               f"{pmin}-{pmax} V")
            if want[pkey] - want[skey] < MIN_STOP_MINUS_START - EPS:
                return False, (f"{gen} stop {want[pkey]} must be at least "
                               f"{MIN_STOP_MINUS_START} V above its start "
                               f"{want[skey]}")

        # Rule 2: no-op.
        if self._same(want, live_now):
            return False, "those are already the live thresholds; nothing to change"

        # Rule 5: rate limit.
        since = now - self.state.get("last_write_ts", 0)
        if self.state.get("last_write_ts") and since < RATE_LIMIT_SECONDS:
            return False, (f"last write was {int(since / 60)} minutes ago; "
                           f"at most one write per "
                           f"{int(RATE_LIMIT_SECONDS / 60)} minutes")

        # Rule 3: a running generator's stop may rise but never fall.
        for gen, skey, pkey, cfg_key in GEN_KEYS:
            action = data.get("mep803aAction" if gen == "mep" else "kubotaAction")
            if action == history.GEN_RUNNING and want[pkey] < live[cfg_key]["stopVoltage"] - EPS:
                return False, (f"{gen} is running; its stop cannot be lowered from "
                               f"{live[cfg_key]['stopVoltage']} to {want[pkey]} "
                               f"mid-run (raising is allowed)")

        # Rule 4: reachability for any generator that will fire now. The
        # arithmetic is the load model's, so this refusal and the POLICY 5
        # line in the plan record cannot disagree about the same question.
        firing = [g for g in GEN_KEYS if want[g[1]] > v + EPS]
        solo = len(firing) == 1
        for gen, skey, pkey, cfg_key in firing:
            window_h = min(live[cfg_key]["maxRuntime"] / 60.0,
                           self.cfg["ags_max_run_hours"][gen])
            reach = self.model.reach(gen, v, want[pkey], window_h, solo=solo,
                                     soc_now=soc, now=now)
            if reach["hours"] is None:
                return False, (
                    f"{reach['why']}. Use the default thresholds "
                    f"{self.cfg['default_start']} / {self.cfg['default_stop']}")
            if not reach["ok"]:
                # The load model's own sentence, so this refusal and the
                # POLICY line in the plan record cannot read differently.
                return False, (f"{gen} cannot lift the pack from {v} V to "
                               f"{want[pkey]} V in its run window: "
                               f"{reach['why']}")

        return True, "permitted"

    # --- heartbeat (SPEC section 9) ----------------------------------------

    def heartbeat(self, now=None):
        """(should_send, thresholds, reason) for the hourly re-send.

        Exempt from the no-op rule, but only once the learning gate is open,
        never while standing down for the owner, and never sooner than an hour
        after the thresholds were last sent by anything.
        """
        now = int(now or time.time())
        gate = self.model.learning_status(now=now)
        if not gate["open"]:
            return False, None, "learning phase: heartbeat withheld"
        if now < self.state.get("override_until", 0):
            return False, None, "standing down after an owner threshold change"
        last = max(self.state.get("last_write_ts", 0) or 0,
                   self.state.get("last_heartbeat_ts", 0) or 0)
        if last and now - last < HEARTBEAT_SECONDS:
            return False, None, (f"the thresholds were last sent "
                                 f"{int((now - last) / 60)} minutes ago; the "
                                 f"heartbeat is hourly")
        return True, self.intended(), "heartbeat"

    # --- rule 9: audit ------------------------------------------------------

    def _audit_line(self, now, text):
        line = (f"{datetime.fromtimestamp(now, history.tzinfo(self.cfg)).isoformat()} "
                f"{text}")
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(config.AUDIT_LOG, "a") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning("could not write audit log: %s", e)

    def _audit(self, args, allowed, why, v, soc, now):
        result = "allowed" if allowed else "refused"
        history.record_action(self.conn, "set_gen_thresholds", args,
                              allowed, why, v, soc, result, ts=now)
        self._audit_line(now, f"{result} args={json.dumps(args, sort_keys=True)} "
                              f"V={v} SOC={soc} reason={why}")
        log.info("guard %s: %s", result, why)
        return allowed, why

    def record_policy_miss(self, rules, recommend, v=None, soc=None, now=None):
        """A rule fired and the model neither acted on it nor overruled it.

        Not a refusal - nothing was attempted. It goes in the same audit log
        because the question it answers is the same one: why did the agent do
        what it did on a given night.
        """
        now = int(now or time.time())
        for r in rules:
            args = {"rule": r["rule"], "name": r["name"], "detail": r["detail"],
                    "proposal": r.get("proposal"), "recommend": recommend}
            why = (f"POLICY {r['rule']} {r['name']} fired ({r['detail']}) and was "
                   f"neither set nor overruled; the model said: {recommend}")
            history.record_action(self.conn, "policy_miss", args, False, why,
                                  v, soc, "missed", ts=now)
            self._audit_line(now, f"policy_miss args={json.dumps(args, sort_keys=True, default=str)} "
                                  f"V={v} SOC={soc} reason={why}")
            log.warning("policy miss: %s", why)
        return len(rules)
