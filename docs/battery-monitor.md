# Conext Battery Monitor — Configuration & SOC Sync

Device: Conext Battery Monitor, slave **191**, port **503** (via gateway
`192.168.3.131`). Spec: 9906278A.

Battery bank: ~100 kWh NMC, 15S, 1800 Ah nominal. Operating window:
charge to **61.0 V**, generator/recharge trigger at **52.0 V**. The bank
is deliberately never taken to true cell-full; 61 V is operational 100%.

## The false-sync problem (fixed 2026-07-29)

The monitor is a coulomb counter, but it snaps SOC to 100% whenever all
three hold simultaneously:

1. Battery voltage > **Charged Voltage** (`0x008A`)
2. Charge current < **Charged/Tail Current** (`0x008B`, % of capacity)
3. Held for the **Auto Sync Time** (`0x008D`)

As shipped, Charged Voltage was **57.0 V** — mid-pack on this flat NMC
curve, 4 V below the real top. On any sunny day where the bank sat above
57 V while solar covered the loads (net shunt current tapering below the
tail threshold), the monitor synced to a false 100%. Every SOC reading
afterward was counted down from a lie, which is why SOC bore no relation
to whether the generator would run overnight.

Compounding it: CEF mode was **Automatic**, which "learns" charge
efficiency from sync events. Learning from false syncs walked the CEF to
a physically impossible **127%**, inflating every charged Ah by 1.27x.

## Current configuration

| Reg      | Name                  | Old value      | Current value  | Notes                                   |
|----------|-----------------------|----------------|----------------|-----------------------------------------|
| `0x008A` | Charged Voltage       | 57000 (57.0 V) | 60500 (60.5 V) | uint16, x0.001 V. Sync only at real top |
| `0x008B` | Charged/Tail Current  | 20 (2.0%)      | 20 (2.0%)      | x0.1 %. 2% of 1800 Ah = 36 A. Unchanged |
| `0x008D` | Auto Sync Time        | 10 (240 s)     | 10 (240 s)     | Enum (spec 2.6). Unchanged              |
| `0x008E` | Auto Sync Sensitivity | 5              | 5              | Unchanged                               |
| `0x0092` | Battery Capacity      | 1800 Ah        | 1800 Ah        | Unchanged                               |
| `0x0093` | Peukert Exponent      | 0 (1.000)      | 0 (1.000)      | 1.0 + raw x 0.002. Correct for lithium  |
| `0x0094` | Charge Efficiency     | 127 (127%!)    | 99 (99%)       | Corrupted by auto-learning; NMC is ~99% |
| `0x007D` | CEF Mode              | 1 (Automatic)  | 0 (Manual)     | Auto learns from syncs — keep manual    |

Sync-to-100% now requires: bank > 60.5 V with charge current < 36 A held
for 4 minutes — i.e. a genuine full charge to 61 V with taper.

## Operational consequences

- SOC between real syncs is pure coulomb math at 99% CEF. Expect slow
  drift (a percent or two over many cycles) during stretches with no
  61 V day. One deliberate full charge re-anchors it.
- `0x006F` (Battery Number of Synchronizations, uint16 ro) increments on
  each real sync — useful to confirm a sync actually happened.
- SOC readings prior to the first post-fix full charge are stale
  (descended from a false sync with 127% CEF inflation) and should be
  ignored.

## Read/write via dashboard register endpoints

```bash
# read
curl -s "http://192.168.1.53:8080/readreg?id=191&port=503&addr=$((0x008A))&type=u16"
# write (example: Charged Voltage = 60.5 V)
curl -s "http://192.168.1.53:8080/writereg?id=191&port=503&addr=$((0x008A))&value=60500&type=u16"
```

Settings live in the Battery Monitor itself, not in any repo file — if
the unit is ever replaced or factory-reset, re-apply the table above.
