# Energy counter registers (Modbus TCP port 503 via Gateway 192.168.3.131)
Source: Schneider specs 9906268B (XW), 9906269A (MPPT 60), 9906270A (MPPT 80).
All are uint32, kWh, scale 0.001, read-only, MSW-first word order (use SchneiderModbusTCP.read_holding_register_32).
Each counter has six consecutive periods at +0x0000 Hour, +0x0004 Today, +0x0008 Week, +0x000C Month, +0x0010 Year, +0x0014 Lifetime.

## XW inverters — slave IDs 10, 11, 12
| Counter | Hour | Today | Week | Month | Year | Lifetime |
|---|---|---|---|---|---|---|
| Energy From Battery | 0x00D0 | 0x00D4 | 0x00D8 | 0x00DC | 0x00E0 | 0x00E4 |
| Energy To Battery   | 0x00E8 | 0x00EC | 0x00F0 | 0x00F4 | 0x00F8 | 0x00FC |
| Load Output Energy  | 0x0130 | 0x0134 | 0x0138 | 0x013C | 0x0140 | 0x0144 |
| Generator Input Energy | 0x0148 | 0x014C | 0x0150 | 0x0154 | 0x0158 | 0x015C |
Sum across the three XW units for system totals.

## MPPT 60 — slave IDs 30 (west array) and 31 (south array)
| Counter | Hour | Today | Week | Month | Year | Lifetime |
|---|---|---|---|---|---|---|
| Energy From PV    | 0x0066 | 0x006A | 0x006E | 0x0072 | 0x0076 | 0x007A |
| Energy To Battery | 0x007E | 0x0082 | 0x0086 | 0x008A | 0x008E | 0x0092 |

## MPPT 80 — slave ID 170 (ground + terrace)
| Counter | Hour | Today | Week | Month | Year | Lifetime |
|---|---|---|---|---|---|---|
| Energy From PV    | 0x0070 | 0x0074 | 0x0078 | 0x007C | 0x0080 | 0x0084 |
| Energy To Battery | 0x0088 | 0x008C | 0x0090 | 0x0094 | 0x0098 | 0x009C |

## Battery Monitor — slave ID 191
No energy counters. Live only: 0x0046 Voltage (V ×0.001), 0x0048 Current (sint32 A ×0.001), 0x004C SOC %.

## Slave id to model mapping — verified, not assumed

`pi5/app.py` names the slaves but not their models. Each slave was read with
both MPPT tables through the dashboard's read-only `/readreg` endpoint; the
wrong table gives an impossible answer, so the mapping is unambiguous:

| Slave | app.py name | Correct table | Lifetime PV | Today | Wrong table gives |
|---|---|---|---|---|---|
| 170 | `MPPT_80_ID` | **MPPT 80** | 4353.1 kWh | 10.38 kWh | 182.9 kWh lifetime; today read fails |
| 31 | `SOUTH_ARRAY_ID` | **MPPT 60** | 4960.9 kWh | 9.52 kWh | 1285 kWh "today" |
| 30 | `WEST_ARRAY_ID` | **MPPT 60** | 5329.0 kWh | 12.37 kWh | 1238 kWh "today" |

All 72 device counters plus 20 derived system totals read cleanly. Cross-check
on 2026-08-28: system PV today 32.275 kWh equals 10.384 + 9.521 + 12.370, and
generator input 4.505 kWh equals the two XW Pro units summed.

## Battery Monitor in/out

SPEC section 3 asks for "Battery Monitor in/out". The Battery Monitor (slave
191) exposes **no energy counters** — only live voltage, current and SOC. All
battery current flows through the XW units, so `counters.py` derives
`system/battery_in` and `system/battery_out` by summing the XW
`Energy To Battery` and `Energy From Battery` counters instead.
