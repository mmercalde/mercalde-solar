# Task: LED flicker during generator charge at ~55–56 V

Find the source of LED light flicker that starts when the pack reaches ~55–56 V during generator charging.

Read-only. Do NOT write any Modbus register and do not touch solar-dashboard.service.

## Setup

- Run on Pi5 (`ssh pi5`), from `/home/michael/solar_dashboard`, using `SchneiderModbusTCP` from `schneider_modbus.py`.
- Gateway: 192.168.3.131, port 503.
- Units:
  - XW Pro Master ID 10
  - XW Pro Slave ID 12
  - XW+ 5548 ID 11 (Kubota on AC2)
  - Battery monitor ID 191
- Register addresses come from the Conext XW 503 map, the same map `app.py` uses for ID 11.

## 1. Identify the live generator

Work out which generator is running and which XW units have a live AC2. Read 0x00A2 AC2 Voltage and 0x00AC AC2 Power on 10, 12 and 11.

## 2. Fast poll

Poll as fast as the gateway allows, keeping the register set small. Report the sample rate you actually achieve. Log to CSV with timestamps.

- On each live unit:
  - 0x00A2 AC2 V
  - 0x00A6 AC2 Hz
  - 0x00A4 AC2 A
  - 0x00AC AC2 W
  - 0x005C charge DC A
  - 0x007B charger status
- On Master 10:
  - 0x008E AC Load L1 V
  - 0x0090 AC Load L2 V
  - 0x0098 AC Load Hz
  - 0x007A inverter status (watch for gen_support / pass-through changes)
- On ID 191:
  - 0x0048 shunt current (sint32 x0.001)

## 3. Capture window

Capture a baseline at ~54 V, then capture continuously through 55–57 V.

## 4. Analyze

- Do AC2 V/Hz and output V/Hz start oscillating at 55–56 V? Give the amplitude and period.
- Does the oscillation start at the generator side (AC2) or the inverter side (output)?
- Does it line up with any of these?
  - charge-current hunting
  - a bulk→absorption change
  - units handing current back and forth
  - inverter status toggling into gen support
- Compare everything against the 54 V baseline.

## 5. Report

Report the findings with the numbers. No fixes until Michael reviews.

Note: Modbus through the gateway runs at a few reads per second at best. It will catch slow surges from current hunting or governor hunting, but not fast flicker. If the logged numbers are steady while the lights still flicker, the problem is faster than Modbus can see.
