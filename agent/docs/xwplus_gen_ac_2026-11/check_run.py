#!/usr/bin/env python3
"""Check the XW+ generator-AC registers against a Kubota run. Read-only.

    agent/venv/bin/python agent/docs/xwplus_gen_ac_2026-11/check_run.py [YYYY-MM-DD] [db]

Uses the samples columns added on 2026-11-08 (xw_gen_ac_v/a/w, xw_charge_dc_a/w,
xw_battery_v, kub_charge_rate) and the hourly counters table's xw_plus
generator_input. Opens the database read-only. See ../xwplus_gen_ac_2026-11.md.
"""
import os
import sqlite3
import statistics as st
import sys
import time

AGENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
day = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d")
db = sys.argv[2] if len(sys.argv) > 2 else os.path.join(AGENT, "data", "history.sqlite")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
t0 = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
rows = c.execute("SELECT * FROM samples WHERE ts >= ? AND ts < ? AND kub_action = 9 "
                 "ORDER BY ts", (t0, t0 + 86400)).fetchall()
if not rows:
    sys.exit(f"no Kubota-running samples on {day}")
if all(r["xw_gen_ac_w"] is None for r in rows):
    sys.exit("the Kubota ran but the xw_* columns are empty: was the agent restarted "
             "with the 2026-11-08 samples change?")
fmt = lambda t: time.strftime("%H:%M", time.localtime(t))
print(f"Kubota running {fmt(rows[0]['ts'])}-{fmt(rows[-1]['ts'])}, {len(rows)} samples")
steady = [r for r in rows if (r["xw_gen_ac_w"] or 0) > 500]
if not steady:
    sys.exit("no minute with more than 500 W of generator AC: 0x00AC reads 0 while running -> FAIL")
med = lambda k: st.median(r[k] for r in steady if r[k] is not None)
v, a, w = med("xw_gen_ac_v"), med("xw_gen_ac_a"), med("xw_gen_ac_w")
dc_a, bv = med("xw_charge_dc_a"), med("xw_battery_v")
dc_w = dc_a * bv
pf = w / (v * a) if v and a else None
eff = dc_w / w if w else None
rates = sorted({r["kub_charge_rate"] for r in steady})
print(f"steady minutes (> 500 W): {len(steady)}, rate cap seen {rates} %")
print(f"0x00A2 AC volts   median {v:.1f} V        -> {'OK' if 220 <= v <= 260 else 'CHECK'} (expect ~240)")
print(f"0x00A4 AC amps    median {a:.2f} A")
print(f"0x00AC AC watts   median {w:.0f} W")
print(f"W / (V x A)       {pf:.3f}             -> {'OK' if pf and 0.85 <= pf <= 1.02 else 'CHECK'} (power factor)")
print(f"DC side           {dc_a:.1f} A x {bv:.2f} V = {dc_w:.0f} W (xwPlusChargePower median {med('xw_charge_dc_w'):.0f} W)")
print(f"DC / AC           {eff:.3f}             -> {'OK' if eff and 0.80 <= eff <= 0.97 else 'CHECK'} (expect ~0.90)")
# Energy cross-check: integrate 0x00AC over the run, against the lifetime counter's rise.
wh = sum((x["xw_gen_ac_w"] + y["xw_gen_ac_w"]) / 2 * (y["ts"] - x["ts"]) / 3600
         for x, y in zip(rows, rows[1:])
         if y["ts"] - x["ts"] <= 300 and x["xw_gen_ac_w"] is not None and y["xw_gen_ac_w"] is not None)
cnt = c.execute("SELECT ts, kwh FROM counters WHERE device='xw_plus' AND counter='generator_input' "
                "AND period='lifetime' AND ts BETWEEN ? AND ? ORDER BY ts",
                (rows[0]["ts"] - 3600, rows[-1]["ts"] + 3600)).fetchall()
if len(cnt) >= 2:
    kwh = cnt[-1]["kwh"] - cnt[0]["kwh"]
    ratio = wh / 1000 / kwh if kwh else None
    print(f"energy            integral of 0x00AC {wh / 1000:.2f} kWh vs Generator Input counter "
          f"{kwh:.2f} kWh ({fmt(cnt[0]['ts'])}-{fmt(cnt[-1]['ts'])}) -> "
          f"{'OK' if ratio and 0.9 <= ratio <= 1.1 else 'CHECK'} (ratio {ratio:.2f})")
else:
    print("energy            no hourly counter snapshots around the run")
