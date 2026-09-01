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
import sun as sunmod
import system as systemmod

log = logging.getLogger(__name__)

# Absolute limits on the pack. Not read from config.json, not relaxable by a
# POLICY rule, not overridable by the owner's baseline, and checked before
# anything else on every write the agent makes: a start below the floor asks
# the pack to sit lower than the bank is meant to go, and a stop above the
# ceiling charges it harder than it is meant to be charged. config.py clamps
# its own bounds to these at load, and pi5/agent_watchdog.sh carries the same
# two numbers so a reset cannot land outside them either.
#
# Config's start_voltage_min / stop_voltage_max may be tighter than these and
# often are. They may never be looser.
HARD_START_FLOOR = 52.0
HARD_STOP_CEILING = 57.0
# The manifest describes the same two limits to the model and to anyone
# reading it. If it ever disagreed with these, one of them would be lying.
systemmod.check_hard_limits(HARD_START_FLOOR, HARD_STOP_CEILING)
# Float noise only. These are hard limits, so nothing is rounded into them:
# a start of 51.9 is below the floor, not close enough to it.
HARD_EPS = 1e-9

MIN_STOP_MINUS_START = 2.0
systemmod.check_separation(MIN_STOP_MINUS_START)
STALE_SECONDS = 300
RATE_LIMIT_SECONDS = 3600
OWNER_OVERRIDE_SECONDS = 6 * 3600
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
    def __init__(self, conn, cfg, model=None, state_path=None, topup=None):
        self._conn = conn
        self.cfg = cfg
        self.model = model or loadmodel.LoadModel(conn, cfg)
        # The top-up state machine. The guard is where a raised start is
        # recorded and where an owner's edit is detected, so it is where both
        # transitions have to be raised from.
        self.topup = topup
        self.state_path = state_path or os.path.join(config.DATA_DIR, "guard_state.json")
        self.state = self._load_state()
        self.last_seen = None
        self.last_check = None

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
                    "override_until": 0,
                    "override_adopted": None, "owner_baseline": None,
                    "raised_starts": {}}

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def note_write(self, applied, now=None, housekeeping=False):
        """Record what the dashboard reports after a successful write.

        Reading the values back means a Pi5 clamp cannot later be mistaken for
        the owner editing the thresholds by hand.

        `housekeeping` leaves the rate-limit clock where it was. Putting a
        raised start back is the agent tidying up after itself, not a decision
        about the night, and letting it start the hour again would mean every
        top-up bought an hour of silence afterwards - which the replay of the
        first night showed swallowing the pre-dawn stop that followed it.
        """
        now = int(now or time.time())
        base = self.baseline()
        raised = dict(self.state.get("raised_starts") or {})
        for gen, skey, pkey, _ in GEN_KEYS:
            if applied[skey] > base[skey] + EPS:
                fresh = gen not in raised
                raised.setdefault(gen, {"since": now, "baseline": base[skey],
                                        "start": applied[skey]})
                # A start that has just gone up is the top-up being asked for.
                # It is asked once: the state machine refuses a second request
                # for the same generator on the same night.
                if fresh and self.topup is not None:
                    self.topup.request(gen, applied[skey], applied[pkey], now)
            else:
                raised.pop(gen, None)
        self.state["raised_starts"] = raised
        self.state["intended"] = dict(applied)
        if not housekeeping:
            self.state["last_write_ts"] = now
        self._save_state()

    def raised_starts(self):
        """Generators whose start the agent has raised and not yet put back."""
        return dict(self.state.get("raised_starts") or {})

    def clear_raised(self, gen):
        raised = dict(self.state.get("raised_starts") or {})
        if raised.pop(gen, None) is not None:
            self.state["raised_starts"] = raised
            self._save_state()

    def owner_changed(self, live_now, intended):
        """Generators whose live numbers are not what the agent last wrote.

        The comparison is against the agent's own last write and never against
        the baseline. An owner who puts a raised start back to 52.0 has
        restored the baseline, and measuring against the baseline would read
        that as nothing having happened - when in fact the owner has just
        overruled the agent, which is the one thing rule 8 exists to notice.
        """
        out = []
        for gen, skey, pkey, _ in GEN_KEYS:
            if (abs(live_now[skey] - intended[skey]) >= EPS
                    or abs(live_now[pkey] - intended[pkey]) >= EPS):
                out.append(gen)
        return out

    def adopt_owner(self, live_now, now, gens, why):
        """Their values are the baseline, the agent stands down, the night ends.

        One path for both ways this is found: mid-run by rule 8, and at
        startup when what is in force is not what this agent last wrote.
        """
        now = int(now)
        self.state["override_until"] = now + OWNER_OVERRIDE_SECONDS
        self.state["override_adopted"] = dict(live_now)
        self.state["owner_baseline"] = dict(live_now)
        self.state["intended"] = dict(live_now)
        # Their values are the baseline now; nothing of ours is raised.
        self.state["raised_starts"] = {}
        self._save_state()
        if self.topup is not None:
            for gen in gens:
                self.topup.owner_took_it(gen, now, why)
        log.warning("owner changed thresholds by hand; adopting %s and "
                    "standing down for 6 h", live_now)
        return live_now

    def adopt_live(self, values, now=None):
        """Settle what is in force at startup: the agent's own, or the owner's.

        Stored intent is a memory of what a previous process meant, and it may
        not be re-asserted over the dashboard - after the freeze at 4:01 and
        the reboot at 8:26 the state file still said Kubota 53.3/57.0, and
        re-asserting it wrote over the owner's 52/56 and started the Kubota
        twice in full sun. But it is still evidence of one thing: what this
        agent last wrote. If the live values match it, they are the agent's
        own and calling them the owner's is how the raised start of
        2026-08-30 became a baseline of 54.0 three minutes after the agent
        wrote it, and could then never be put back. If they differ, the owner
        moved them while the agent was away, which is an owner override like
        any other.
        """
        now = int(now or time.time())
        values = dict(values)
        intended = self.state.get("intended")
        if intended:
            changed = self.owner_changed(values, intended)
            if not changed:
                log.info("the live thresholds %s are this agent's own last "
                         "write; the baseline stays %s", values,
                         self.baseline())
                return self.baseline()
            return self.adopt_owner(
                values, now, changed,
                "the thresholds were changed while the agent was not running")
        # Nothing has ever been written, so there is nothing to compare and
        # everything in force is the owner's.
        self.state["owner_baseline"] = dict(values)
        self.state["intended"] = dict(values)
        self.state["raised_starts"] = {}
        self._save_state()
        log.info("adopted the live thresholds as the baseline: %s", values)
        return values

    def approval(self):
        """The decision that permits one write, or None.

        apply_thresholds refuses to send anything without this, so there is no
        path to the dashboard that check() has not seen and rule 9 has not
        recorded.
        """
        decided = self.last_check or {}
        return dict(decided) if decided.get("allowed") else None

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
        # What the dashboard said when this write was judged. The tool reads
        # it back to describe the change to the owner without a second fetch.
        self.last_seen = {"thresholds": self._live_thresholds(live),
                          "voltage": v, "soc": soc, "ts": now}
        # What check() decided to write, which may be less than was asked for.
        self.last_check = {"values": dict(want), "refused": [], "requested": dict(want)}

        allowed, why = self._evaluate(want, data, live, v, soc, now, reason, policy)
        return self._audit(args, allowed, why, v, soc, now)

    @staticmethod
    def hard_limits(want):
        """(ok, reason) for the two absolute limits. No state, no config.

        First, and separately from rule 1's configured bounds, so that no
        later rule, no owner baseline and no edit to config.json can produce a
        write outside them.
        """
        for gen, skey, pkey, _ in GEN_KEYS:
            if want[skey] < HARD_START_FLOOR - HARD_EPS:
                return False, (f"floor {HARD_START_FLOOR}: {gen} start "
                               f"{want[skey]} is below it, and the floor is "
                               f"absolute")
            if want[pkey] > HARD_STOP_CEILING + HARD_EPS:
                return False, (f"ceiling {HARD_STOP_CEILING}: {gen} stop "
                               f"{want[pkey]} is above it, and the ceiling is "
                               f"absolute")
        return True, "within the hard limits"

    def _evaluate(self, want, data, live, v, soc, now, reason="", policy=None):
        # The hard limits, before anything else and regardless of everything
        # else. Nothing below can widen them.
        ok, why = self.hard_limits(want)
        if not ok:
            return False, why

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
        changed = (self.owner_changed(live_now, self.state["intended"])
                   if self.state.get("intended") else [])
        if changed:
            self.adopt_owner(live_now, now, changed,
                             "the owner changed the thresholds in the "
                             "dashboard")
            return False, (
                "the owner changed the thresholds in the dashboard "
                f"(now MEP {live_now['mep_start']}/{live_now['mep_stop']}, "
                f"Kubota {live_now['kub_start']}/{live_now['kub_stop']}). "
                "Adopting those values and making no writes for 6 hours")
        if now < self.state.get("override_until", 0):
            mins = int((self.state["override_until"] - now) / 60)
            return False, (f"standing down after an owner threshold change; "
                           f"{mins} minutes left")

        # Rules 1, 3 and 4 are about one generator's own pair of numbers, so a
        # proposal that breaks them for one generator is trimmed rather than
        # thrown away whole. Last night's pre-dawn proposal bundled "Kubota
        # start back to 52" - which was wanted - with "Kubota stop down to
        # 54.5" mid-run, which is forbidden, and lost both.
        kept, dropped = self._trim(want, data, live, live_now, v, soc, now)
        if dropped and kept is None:
            return False, "; ".join(dropped)
        if dropped:
            self.last_check["refused"] = list(dropped)
            want = kept

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

        # Rule 10: the daylight hold. Between sunrise and sunset no write may
        # raise a generator's start above the owner's baseline, whatever rule
        # proposed it. The day's solar goes into the pack first, and the Pi5's
        # own low-voltage auto-start stays the only thing that runs a
        # generator in that window. POLICY 4 holds itself to the same window,
        # but this does not depend on a rule remembering to.
        daylight = self._daylight(now)
        if daylight:
            base = self.baseline()
            raised = [gen for gen, skey, _, _ in GEN_KEYS
                      if want[skey] > base[skey] + EPS]
            if raised:
                return False, (
                    f"daylight hold: it is {history.clock(now, self.cfg)}, "
                    f"between sunrise {history.clock(daylight[0], self.cfg)} and "
                    f"sunset {history.clock(daylight[1], self.cfg)}, and this "
                    f"would raise {' and '.join(raised)} start above the "
                    f"baseline. The day's solar goes into the pack first; only "
                    f"the Pi5's {self.cfg['default_start']} V auto-start runs a "
                    f"generator before sunset")

        # Rule 2: no-op.
        if self._same(want, live_now):
            return False, ("those are already the live thresholds; nothing to "
                           "change" if not dropped else
                           "nothing is left to write once "
                           + "; ".join(dropped))
        self.last_check["values"] = dict(want)

        # Rule 5: rate limit. It exists to stop the agent churning the
        # thresholds upward; a write that only moves them back toward the
        # owner's baseline is the opposite of churn and waiting an hour to
        # make it just leaves a raised start standing for that hour.
        since = now - self.state.get("last_write_ts", 0)
        if (self.state.get("last_write_ts") and since < RATE_LIMIT_SECONDS
                and not self.toward_baseline(want, live_now)):
            return False, (f"last write was {int(since / 60)} minutes ago; "
                           f"at most one write per "
                           f"{int(RATE_LIMIT_SECONDS / 60)} minutes")

        for gen, skey, pkey, cfg_key in GEN_KEYS:
            action = data.get("mep803aAction" if gen == "mep" else "kubotaAction")
            if action == history.GEN_RUNNING and want[pkey] < live[cfg_key]["stopVoltage"] - EPS:
                return False, (f"{gen} is running; its stop cannot be lowered from "
                               f"{live[cfg_key]['stopVoltage']} to {want[pkey]} "
                               f"mid-run (raising is allowed)")

        ok, why = self._reachable(want, live, v, soc, now)
        if not ok:
            return False, why

        return True, ("permitted" if not dropped
                      else "permitted in part; " + "; ".join(dropped))

    def toward_baseline(self, want, live):
        """Does every value move toward the owner's baseline, and one reach it?

        "Toward" is measured as distance from the baseline, so lowering a
        raised start counts and so does lifting a stop that was dropped below
        it. Anything that takes a value further away is an ordinary write and
        waits its turn.
        """
        base = self.baseline()
        closer = False
        for key in ("mep_start", "mep_stop", "kub_start", "kub_stop"):
            was, now_ = abs(live[key] - base[key]), abs(want[key] - base[key])
            if now_ > was + EPS:
                return False
            if now_ < was - EPS:
                closer = True
        return closer

    def _gen_faults(self, gen, pair, want, data, live, live_now, v, soc, now):
        """Why this generator's proposed pair of numbers cannot stand, if it cannot.

        Only the rules that are about one generator on its own: its bounds,
        its separation, and lowering the stop of a generator that is running.
        Reachability is about the set that fires, so it is asked later.
        """
        start, stop = pair
        skey, pkey = (("mep_start", "mep_stop") if gen == "mep"
                      else ("kub_start", "kub_stop"))
        cfg_key = "mep803a" if gen == "mep" else "kubota"
        smin, smax = self.cfg["start_voltage_min"], self.cfg["start_voltage_max"]
        pmin, pmax = self.cfg["stop_voltage_min"], self.cfg["stop_voltage_max"]

        if not smin - EPS <= start <= smax + EPS:
            return (f"{gen} start {start} is outside the permitted "
                    f"{smin}-{smax} V")
        if not pmin - EPS <= stop <= pmax + EPS:
            return (f"{gen} stop {stop} is outside the permitted "
                    f"{pmin}-{pmax} V")
        if stop - start < MIN_STOP_MINUS_START - EPS:
            return (f"{gen} stop {stop} must be at least "
                    f"{MIN_STOP_MINUS_START} V above its start {start}")
        action = data.get("mep803aAction" if gen == "mep" else "kubotaAction")
        if action == history.GEN_RUNNING and stop < live[cfg_key]["stopVoltage"] - EPS:
            return (f"{gen} is running; its stop cannot be lowered from "
                    f"{live[cfg_key]['stopVoltage']} to {stop} mid-run "
                    f"(raising is allowed)")
        return None

    def _trim(self, want, data, live, live_now, v, soc, now):
        """(values to write, what was dropped and why).

        Each generator's numbers are tried as asked; where that fails, with
        the stop left where it is, then with the start left where it is. The
        first combination that stands is taken, so an allowed change is not
        lost to a forbidden one beside it.
        """
        kept, dropped = dict(want), []
        for gen, skey, pkey, _ in GEN_KEYS:
            asked = (want[skey], want[pkey])
            options = ((asked, None),
                       ((want[skey], live_now[pkey]), f"{gen} stop"),
                       ((live_now[skey], want[pkey]), f"{gen} start"),
                       ((live_now[skey], live_now[pkey]), f"{gen} start and stop"))
            first = None
            for pair, giving_up in options:
                fault = self._gen_faults(gen, pair, want, data, live, live_now,
                                         v, soc, now)
                if first is None:
                    first = fault
                if fault is None:
                    kept[skey], kept[pkey] = pair
                    if giving_up and (abs(pair[0] - asked[0]) > EPS
                                      or abs(pair[1] - asked[1]) > EPS):
                        dropped.append(f"{first} (left {giving_up} alone)")
                    break
            else:
                return None, [first or f"{gen} cannot be written"]

        return kept, dropped

    def _reachable(self, want, live, v, soc, now):
        """Rule 4, for whatever will actually fire, after the trim.

        Asked once about the set that will run: both engines on one pack is a
        single question, and asking each alone would refuse a pair for a
        shortfall neither of them has.
        """
        firing = [g for g in GEN_KEYS if want[g[1]] > v + EPS]
        if not firing:
            return True, None
        window_h = min(min(live[cfg_key]["maxRuntime"] / 60.0,
                           self.cfg["ags_max_run_hours"][gen])
                       for gen, _, _, cfg_key in firing)
        solo = len(firing) == 1
        gen = firing[0][0] if solo else None
        who = firing[0][0] if solo else "both generators"
        target = max(want[pkey] for _, _, pkey, _ in firing)
        reach = self.model.reach(gen, v, target, window_h, solo=solo,
                                 soc_now=soc, now=now)
        if reach["hours"] is None:
            return False, (f"{reach['why']}. Use the default thresholds "
                           f"{self.cfg['default_start']} / "
                           f"{self.cfg['default_stop']}")
        if not reach["ok"]:
            # The load model's own sentence, so this refusal and the POLICY
            # line in the plan record cannot read differently.
            return False, (f"{who} cannot lift the pack from {v} V to "
                           f"{target} V in its run window: {reach['why']}")
        return True, None

    def _daylight(self, now):
        """(sunrise, sunset) if `now` is between them, else None.

        Computed from the site's latitude and longitude, so the hold applies
        whether or not anything else is reachable. The only case that returns
        None is a latitude inside a polar circle, where the sun does not both
        rise and set that day; this site is at 32 degrees.
        """
        return sunmod.daylight(self.cfg, now)

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
        if self.last_check is not None:
            self.last_check["allowed"] = bool(allowed)
            self.last_check["ts"] = now
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
