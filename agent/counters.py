"""Gateway energy counters over Modbus TCP 503.

Read once per agent tick into the `counters` table as an independent
cross-check on `daily`, which is integrated from /data samples.

Register addresses come from docs/energy_registers.md (Schneider specs
9906268B, 9906269A, 9906270A). Each counter occupies six consecutive uint32
periods at a fixed stride; all are kWh at scale 0.001, MSW-first.

The slave-id-to-model mapping was confirmed against the live gateway: reading
the wrong model's table on a slave returns absurd values (a "today" figure
larger than lifetime) or fails outright.
"""

import logging
import time
from urllib.parse import urlparse

import schneider_modbus

log = logging.getLogger(__name__)

MODBUS_PORT = 503
KWH_SCALE = 0.001

# Offset from a counter's base address to each period. SPEC section 3 names
# four of the six; hour and week are read by the same code if ever needed.
PERIOD_OFFSET = {"hour": 0x00, "today": 0x04, "week": 0x08,
                 "month": 0x0C, "year": 0x10, "lifetime": 0x14}
PERIODS = ("today", "month", "year", "lifetime")

XW_BASE = {
    "energy_from_battery": 0x00D0,
    "energy_to_battery": 0x00E8,
    "load_output": 0x0130,
    "generator_input": 0x0148,
}
MPPT60_BASE = {"energy_from_pv": 0x0066, "energy_to_battery": 0x007E}
MPPT80_BASE = {"energy_from_pv": 0x0070, "energy_to_battery": 0x0088}

# Slave ids and names match pi5/app.py. Models verified live, not assumed.
DEVICES = [
    {"name": "xw_master", "slave": 10, "bases": XW_BASE},      # XW Pro 6848 Master
    {"name": "xw_plus", "slave": 11, "bases": XW_BASE},        # XW+ 5548 (Kubota system)
    {"name": "xw_slave", "slave": 12, "bases": XW_BASE},       # XW Pro 6848 Slave
    {"name": "west", "slave": 30, "bases": MPPT60_BASE},       # MPPT 60
    {"name": "south", "slave": 31, "bases": MPPT60_BASE},      # MPPT 60
    {"name": "mppt80", "slave": 170, "bases": MPPT80_BASE},    # MPPT 80
]

XW_DEVICES = [d["name"] for d in DEVICES if d["bases"] is XW_BASE]
PV_DEVICES = [d["name"] for d in DEVICES if d["bases"] is not XW_BASE]


def modbus_host(cfg):
    """Gateway IP, taken from the configured InsightLocal URL."""
    return urlparse(cfg["gateway"]["url"]).hostname


def read_all(cfg, host=None, client=None):
    """Read every counter/period for every device.

    Returns {(device, counter, period): kwh}. Registers that fail to read are
    left out rather than recorded as zero: a missing counter must not look
    like a reset one.
    """
    host = host or modbus_host(cfg)
    client = client or schneider_modbus.SchneiderModbusTCP()
    out = {}
    for dev in DEVICES:
        for counter, base in dev["bases"].items():
            for period in PERIODS:
                addr = base + PERIOD_OFFSET[period]
                raw = client.read_holding_register_32(
                    host, MODBUS_PORT, dev["slave"], addr)
                if raw is None:
                    log.warning("counter read failed: %s %s %s (slave %d addr 0x%04X)",
                                dev["name"], counter, period, dev["slave"], addr)
                    continue
                out[(dev["name"], counter, period)] = round(raw * KWH_SCALE, 3)
    return out


def derive_totals(readings):
    """Add system rollups the registers do not provide directly.

    The Battery Monitor exposes no energy counters (docs/energy_registers.md),
    so battery in/out is the sum of the XW units' to/from-battery counters,
    which is where all battery current actually flows.
    """
    derived = {}
    for period in PERIODS:
        def total(names, counter):
            vals = [readings[(n, counter, period)] for n in names
                    if (n, counter, period) in readings]
            return round(sum(vals), 3) if vals else None

        for key, names, counter in (
            ("battery_in", XW_DEVICES, "energy_to_battery"),
            ("battery_out", XW_DEVICES, "energy_from_battery"),
            ("load", XW_DEVICES, "load_output"),
            ("generator", XW_DEVICES, "generator_input"),
            ("pv", PV_DEVICES, "energy_from_pv"),
        ):
            v = total(names, counter)
            if v is not None:
                derived[("system", key, period)] = v
    return derived


def record(conn, cfg, ts=None, host=None, client=None):
    """Read all counters and write one snapshot into `counters`."""
    ts = int(ts or time.time())
    readings = read_all(cfg, host=host, client=client)
    if not readings:
        log.warning("no counters read; gateway unreachable on Modbus 503?")
        return 0
    readings.update(derive_totals(readings))
    conn.executemany(
        "INSERT OR REPLACE INTO counters (ts, device, counter, period, kwh) "
        "VALUES (?,?,?,?,?)",
        [(ts, dev, ctr, per, kwh) for (dev, ctr, per), kwh in readings.items()])
    conn.commit()
    return len(readings)


def latest(conn, period="today"):
    """Most recent snapshot for one period: {(device, counter): kwh}."""
    row = conn.execute("SELECT MAX(ts) t FROM counters").fetchone()
    if not row or row["t"] is None:
        return {}
    rows = conn.execute(
        "SELECT device, counter, kwh FROM counters WHERE ts=? AND period=?",
        (row["t"], period)).fetchall()
    return {(r["device"], r["counter"]): r["kwh"] for r in rows}


def cross_check(conn, cfg, day=None):
    """Compare today's counter deltas against `daily`, which is integrated
    from samples. Returns None when there is not enough of either to compare."""
    import history
    day = day or history.local_day(int(time.time()), cfg)
    d = conn.execute("SELECT * FROM daily WHERE day=?", (day,)).fetchone()
    today = latest(conn, "today")
    if not d or not today:
        return None
    out = {"day": day}
    pv_kwh = today.get(("system", "pv"))
    load_kwh = today.get(("system", "load"))
    if pv_kwh is not None and d["solar_wh"] is not None:
        out["solar"] = {"counters_kwh": pv_kwh,
                        "samples_kwh": round(d["solar_wh"] / 1000.0, 3)}
    if load_kwh is not None and d["load_wh"] is not None:
        out["load"] = {"counters_kwh": load_kwh,
                       "samples_kwh": round(d["load_wh"] / 1000.0, 3)}
    return out


def main():
    """Read and print every counter; useful for verifying the register table."""
    import config
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load()
    readings = read_all(cfg)
    readings.update(derive_totals(readings))
    for (dev, ctr, per) in sorted(readings):
        print(f"{dev:12s} {ctr:22s} {per:9s} {readings[(dev, ctr, per)]:12.3f} kWh")
    print(f"\n{len(readings)} values")


if __name__ == "__main__":
    main()
