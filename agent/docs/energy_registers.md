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

## MPPT 60 — check which of slave IDs 30, 31, 170 are MPPT 60 vs MPPT 80 (see pi5/app.py mapping)
| Counter | Hour | Today | Week | Month | Year | Lifetime |
|---|---|---|---|---|---|---|
| Energy From PV    | 0x0066 | 0x006A | 0x006E | 0x0072 | 0x0076 | 0x007A |
| Energy To Battery | 0x007E | 0x0082 | 0x0086 | 0x008A | 0x008E | 0x0092 |

## MPPT 80
| Counter | Hour | Today | Week | Month | Year | Lifetime |
|---|---|---|---|---|---|---|
| Energy From PV    | 0x0070 | 0x0074 | 0x0078 | 0x007C | 0x0080 | 0x0084 |
| Energy To Battery | 0x0088 | 0x008C | 0x0090 | 0x0094 | 0x0098 | 0x009C |

## Battery Monitor — slave ID 191
No energy counters. Live only: 0x0046 Voltage (V ×0.001), 0x0048 Current (sint32 A ×0.001), 0x004C SOC %.
