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
import time
from datetime import datetime, timedelta

import config
import history
import loadmodel

log = logging.getLogger(__name__)

MIN_STOP_MINUS_START = 2.0
STALE_SECONDS = 300
RATE_LIMIT_SECONDS = 3600
OWNER_OVERRIDE_SECONDS = 6 * 3600
# Thresholds are written to one decimal, so anything under this is the same value.
EPS = 0.05

GEN_KEYS = (("mep", "mep_start", "mep_stop", "mep803a"),
            ("kubota", "kub_start", "kub_stop", "kubota"))


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
                    "override_until": 0, "override_adopted": None}

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

    def intended(self):
        """Thresholds the agent means to be in force; defaults until it writes."""
        if self.state.get("intended"):
            return dict(self.state["intended"])
        return {"mep_start": self.cfg["default_start"],
                "mep_stop": self.cfg["default_stop"],
                "kub_start": self.cfg["default_start"],
                "kub_stop": self.cfg["default_stop"]}

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
              now=None, status=None):
        """Return (allowed, reason). Always audited, pass or refuse."""
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

        allowed, why = self._evaluate(want, data, live, v, now)
        return self._audit(args, allowed, why, v, soc, now)

    def _evaluate(self, want, data, live, v, now):
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

        # Rule 4: reachability for any generator that will fire now.
        firing = [g for g in GEN_KEYS if want[g[1]] > v + EPS]
        solo = len(firing) == 1
        for gen, skey, pkey, cfg_key in firing:
            rate = self.model.charge_rate(gen, solo=solo, now=now)
            if rate is None:
                rate = self.model.charge_rate(gen, solo=None, now=now)
            if rate is None or not rate.get("v_per_h"):
                return False, (
                    f"no observed charge rate for {gen} yet, so {want[pkey]} V "
                    f"cannot be shown to be reachable. Use the default thresholds "
                    f"{self.cfg['default_start']} / {self.cfg['default_stop']}")
            window_h = min(live[cfg_key]["maxRuntime"] / 60.0,
                           self.cfg["ags_max_run_hours"][gen])
            needed_h = (want[pkey] - v) / rate["v_per_h"]
            if needed_h > window_h + 1e-9:
                return False, (
                    f"{gen} would need {needed_h:.1f} h to lift the pack from "
                    f"{v} V to {want[pkey]} V at its observed "
                    f"{rate['v_per_h']} V/h, but its run window is "
                    f"{window_h:.1f} h")

        return True, "permitted"

    # --- heartbeat (SPEC section 9) ----------------------------------------

    def heartbeat(self, now=None):
        """(should_send, thresholds, reason) for the per-tick re-send.

        Exempt from the rate limit and the no-op rule, but only once the
        learning gate is open, and never while standing down for the owner.
        """
        now = int(now or time.time())
        gate = self.model.learning_status(now=now)
        if not gate["open"]:
            return False, None, "learning phase: heartbeat withheld"
        if now < self.state.get("override_until", 0):
            return False, None, "standing down after an owner threshold change"
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
