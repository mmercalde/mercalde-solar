# Battery Monitor sync settings, 2026-10-08

Conext Battery Monitor, Modbus slave 191, port 503 via gateway 192.168.3.131.
Spec: Conext Battery Monitor Modbus 503 spec 9906278A.

**Why:** the monitor read 98 % SOC at a 56.3 V rest, which is about 45 %. Its
sync point was 60.4 V. The monitor only resets to 100 % at that voltage, and
this lithium bank is charged below it.

Written from the Pi5 with `SchneiderModbusTCP.write_single_register_16`, one
register at a time, each read back. Then `0x00AC = 1` (Refresh Configuration).
`0x00AD` (Synchronize) was not written. No other slave was touched.

| Reg    | Setting                     | Before          | After           |
|--------|-----------------------------|-----------------|-----------------|
| 0x008A | Charger Float Voltage       | 60400 (60.4 V)  | 57500 (57.5 V)  |
| 0x008B | Charger Float Current       | 20 (2.0 %)      | 10 (1.0 %, 18 A)|
| 0x0094 | Charge Efficiency Factor    | 99 %            | 98 %            |

Unchanged (read before and after):

| Reg    | Setting                     | Value |
|--------|-----------------------------|-------|
| 0x0092 | Battery Capacity            | 1800 Ah |
| 0x008D | Auto Sync Time (enum)       | 10 (left alone) |
| 0x008E | Auto Sync Sensitivity       | 5 (left alone) |
| 0x0093 | Peukert Exponent            | 0 (= 1.00) |
| 0x007D | Charge Efficiency Factor Mode | 0 (Manual) |
| 0x007A | Self Discharge Rate         | 0 (off) |
| 0x0080 | Setup lock                  | 0 (unlocked) |
| 0x007B | Shunt amp rating            | 50 (raw, not decoded) |
| 0x007C | Shunt mV rating             | 0 (raw, not decoded) |

The shunt values do not read as a literal amp/mV pair. Check how the spec
encodes them before trusting the current reading.

SOC stays wrong until the pack next reaches 57.5 V and the charge current
falls below 18 A, which triggers the next sync.
