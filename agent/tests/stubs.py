"""Stand-ins shared by the test modules.

policy.py decides and the load model does the physics. The rules are what the
policy tests are about, so here the physics is stated outright instead of
being learned from a database; loadmodel's own arithmetic is covered in
test_loadmodel.py.
"""

import math

import loadmodel


class StubModel:
    """The physics, stated rather than learned.

    policy.py decides; the load model answers "can this generator get there in
    that long". Its own arithmetic is covered in test_loadmodel.py, so here it
    is stated outright and the rules are what is under test.

    `soc_per_v` points of state of charge per volt, `rates` amps and %SOC/h
    per generator, with None for the pair meaning no paired history.
    """

    def __init__(self, rates=None, pair=None, soc_at_52=40.0, soc_per_v=10.0,
                 basis="resting curve, 0 charging runs on record"):
        self.basis = basis
        self.rates = rates if rates is not None else {
            "mep": {"a": 90.0, "soc_per_h": 15.0},
            "kubota": {"a": 60.0, "soc_per_h": 10.0}}
        self.pair = pair
        self.soc_at_52, self.soc_per_v = soc_at_52, soc_per_v

    def _soc(self, v):
        return self.soc_at_52 + (v - 52.0) * self.soc_per_v

    def _volts(self, soc):
        return 52.0 + (soc - self.soc_at_52) / self.soc_per_v

    def _rate(self, gen):
        return self.pair if gen is None else self.rates.get(gen)

    def reach(self, gen, from_v, target_v, window_h, solo=None, soc_now=None,
              now=None):
        rate = self._rate(gen)
        who = "both generators" if gen is None else gen
        if rate is None:
            return {"ok": False, "rate": None, "hours": None, "basis": None,
                    "why": f"no observed charge rate for {who}, so "
                           f"{target_v:.1f} V cannot be shown to be reachable"}
        soc = self._soc(from_v) if soc_now is None else soc_now
        hours = max(0.0, (self._soc(target_v) - soc) / rate["soc_per_h"])
        ok = hours <= window_h + 1e-9
        phrase = loadmodel.rate_phrase(rate)
        return {"ok": ok, "rate": rate, "hours": hours, "basis": self.basis,
                "why": (f"{target_v:.1f} reachable in {hours:.1f} h at {phrase}"
                        if ok else
                        f"{target_v:.1f} needs {hours:.1f} h at {phrase} but "
                        f"the run window is {window_h:.1f} h")}

    def best_reachable_target(self, gen, from_v, window_h, ceiling, floor,
                              step=0.5, solo=None, soc_now=None, now=None):
        rate = self._rate(gen)
        if rate is None:
            return None
        soc = self._soc(from_v) if soc_now is None else soc_now
        v = self._volts(soc + rate["soc_per_h"] * window_h)
        v = math.floor(min(v, ceiling) / step) * step
        return round(v, 2) if v >= floor - 1e-9 else None
