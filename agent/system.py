"""The system manifest: what the hardware is, read from one file.

agent/system.yaml is the single description of the site. The SYSTEM section of
the model's prompt is generated from it, the config keys that describe
hardware or policy are taken from it rather than repeated in config.json, and
guard.py checks its own hard limits against it before the agent writes
anything.

config.json keeps what is secret or per-install - the Telegram token, the
gateway password, the model endpoint. Everything a second pair of eyes would
want to check about the system itself lives in the manifest, where it can be
read without reading code.
"""

import logging
import os

import yaml

log = logging.getLogger(__name__)

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "system.yaml")

# Manifest sections that must be present. A manifest missing one of these
# would leave the prompt or the guard quietly describing nothing.
REQUIRED = ("site", "network", "inverters", "generators", "battery", "arrays",
            "policy")

_cache = {}


class ManifestError(RuntimeError):
    """The manifest is missing, unreadable, or disagrees with the code."""


def load(path=None, force=False):
    """The manifest as a dict, read once."""
    path = path or MANIFEST_PATH
    if not force and path in _cache:
        return _cache[path]
    try:
        with open(path) as f:
            manifest = yaml.safe_load(f)
    except OSError as e:
        raise ManifestError(f"cannot read the system manifest at {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ManifestError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(manifest, dict):
        raise ManifestError(f"{path} does not describe a system")
    missing = [k for k in REQUIRED if k not in manifest]
    if missing:
        raise ManifestError(f"{path} is missing: {', '.join(missing)}")
    _cache[path] = manifest
    return manifest


# --- what the rest of the agent takes from it --------------------------------

def config_overlay(manifest):
    """The config keys the manifest owns.

    Anything here is a fact about the system or a rule about what may be done
    to it, so it belongs in one readable file rather than in a JSON blob
    beside a password.
    """
    site, net, gens = manifest["site"], manifest["network"], manifest["generators"]
    battery, policy = manifest["battery"], manifest["policy"]
    out = {
        "lat": site["latitude"], "lon": site["longitude"], "tz": site["timezone"],
        "dashboard_url": net["dashboard_url"],
        "ags_max_run_hours": {g: gens["max_run"]["ags_hours"]
                              for g in ("mep", "kubota")},
        "assumed_charge_a": {g: gens[g]["assumed_charge_a"]
                             for g in ("mep", "kubota")},
        # Rated watts and the consumption curve, per generator. The price of
        # a gallon is not here: it is per-install and it moves, so it lives
        # in config.json and is optional.
        "fuel": {g: gens[g]["fuel"] for g in ("mep", "kubota")
                 if gens[g].get("fuel")},
        "exercise": {"mep_days": gens["mep"]["exercise"]["every_days"],
                     "kubota_days": gens["kubota"]["exercise"]["every_days"],
                     "start": gens["mep"]["exercise"]["at"],
                     "minutes": gens["mep"]["exercise"]["minutes"]},
    }
    out.update(policy)
    # The floor and the ceiling are the battery's, not the policy's: the
    # policy may sit inside them and never outside.
    out["start_voltage_min"] = max(policy["start_voltage_min"],
                                   battery["floor_v"])
    out["stop_voltage_max"] = min(policy["stop_voltage_max"],
                                  battery["ceiling_v"])
    return out


def check_separation(separation, manifest=None):
    """guard.py's MIN_STOP_MINUS_START and the manifest must agree too.

    The same reasoning as the hard limits: the guard keeps its own constant,
    and the manifest describes it to everyone else, so the two are checked
    against each other rather than left to drift.
    """
    policy = (manifest or load())["policy"]
    if float(policy["min_stop_minus_start"]) != float(separation):
        raise ManifestError(
            f"system.yaml policy.min_stop_minus_start is "
            f"{policy['min_stop_minus_start']} but guard.py's "
            f"MIN_STOP_MINUS_START is {separation}; one of them is wrong")
    return True


def check_hard_limits(floor, ceiling, manifest=None):
    """The guard's constants and the manifest must say the same thing.

    guard.py keeps the two limits as code constants on purpose - a number that
    can be edited in a data file is not a hard limit. But a manifest that
    disagreed with them would describe a system that does not exist, to the
    model and to anyone reading it, so the two are checked against each other
    before the agent starts.
    """
    battery = (manifest or load())["battery"]
    if float(battery["floor_v"]) != float(floor):
        raise ManifestError(
            f"system.yaml battery.floor_v is {battery['floor_v']} but guard.py's "
            f"HARD_START_FLOOR is {floor}; one of them is wrong")
    if float(battery["ceiling_v"]) != float(ceiling):
        raise ManifestError(
            f"system.yaml battery.ceiling_v is {battery['ceiling_v']} but "
            f"guard.py's HARD_STOP_CEILING is {ceiling}; one of them is wrong")
    return True


# --- the SYSTEM section of the prompt ----------------------------------------

def _gen_line(key, gen, max_run):
    charge = f"{gen['nameplate_kw']} kW nameplate, {gen['charge_rate_pct']}% charge rate"
    line = (f"  {gen['label']} (AGS slave {gen['ags_slave']}): {charge}, feeding "
            f"{', '.join(gen['feeds'])}. Exercises "
            f"{gen['exercise']['minutes']} minutes at {gen['exercise']['at']} "
            f"every {gen['exercise']['every_days']} days. Assumed charge "
            f"{gen['assumed_charge_a']} A into the pack until it has runs of "
            f"its own.")
    magnum = gen.get("magnum")
    if magnum:
        line += (f" Its runs also drive a {magnum['model']} that is not on "
                 f"Modbus, so only the shunt's current measures what a "
                 f"{gen['label']} run delivers.")
    return line


def system_prompt_section(manifest=None):
    """The SYSTEM block, written from the manifest rather than by hand."""
    m = manifest or load()
    b, arrays, gens = m["battery"], m["arrays"], m["generators"]
    lines = []

    inverters = ", ".join(f"{i['model']} (slave {i['slave']}, {i['role']})"
                          for i in m["inverters"])
    lines.append(f"Inverters: {inverters}.")

    lines.append(
        f"Battery: {b['capacity_kwh_nominal']} kWh nominal "
        f"({b['capacity_ah_nominal']} Ah), {b['configuration']}, "
        f"{b['chemistry']}. {' '.join(str(b['policy']).split())} "
        f"Never below {b['floor_v']} V, never above {b['ceiling_v']} V; "
        f"full would be {b['full_v']} V and is not a target. "
        f"A {b['monitor']['model']} (slave {b['monitor']['slave']}) measures "
        f"true net current at the shunt, positive when charging.")

    t = arrays["totals"]
    controllers = "; ".join(
        f"{a['name']} on {a['controller']} (slave {a['slave']})"
        for a in arrays["controllers"])
    lines.append(f"Solar: {t['panels']} panels of {t['panel_watts']} W, "
                 f"{t['nominal_kw']} kW nominal, on three controllers - "
                 f"{controllers}.")

    lines.append("Generators:")
    for key in ("mep", "kubota"):
        lines.append(_gen_line(key, gens[key], gens["max_run"]))
    lines.append(
        f"  A run is capped at {gens['max_run']['pi5_minutes']} minutes by the "
        f"Pi5 and {gens['max_run']['ags_hours']} hours by the AGS, whichever "
        f"binds first. Exercise runs are not mine and are not a signal.")

    lines.append(
        "The Pi5 starts both generators when the pack falls below the start "
        "threshold and stops each one at its own stop threshold. Setting those "
        "four thresholds is the only change I can make. I never start or stop "
        "a generator directly.")
    return "\n".join(lines)
