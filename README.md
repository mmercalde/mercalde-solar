# termite-rf-gun

ESP32-S3 standalone driver for microwave inverter HV boards, powering an RF
applicator (launcher + copper-lined horn) for **termite control**. Goal: drive
a microwave inverter board with no oven main board (DPC), generating the HV the
magnetron needs, under ESP32 command.

Three inverter boards have been investigated. Status of each is below.

---

## Boards

### 1. Panasonic F606Y8X00AP — SINGLE-IGBT — ✅ SOLVED / PROVEN

The inverter board from a **Panasonic NN-SN778S** (120 V US oven).
Cross-references: replaces **F606Y8M00AP**; ABH-version variant is **F606Y9X90AP**
(this oven is the APH/USA version → F606Y8X00AP). Fits NN-SN7xx-series US ovens.

- **Driver: `panasonic_inverter_esp32s3/`** (tagged `working-standalone`)
- **Method that worked: CAPTURE & REPLICATE.** Built an ESP32 edge-logger
  (`log` mode, GPIO6 via divider off CN701 pin1), captured the real oven's
  command, then replicated it.
- **Measured command (the key result):**
  - **222 Hz** (period 4509 µs)
  - **~85 % HIGH duty**, idles HIGH, ~565–665 µs LOW notch
  - **NO soft-start** — full duty from cycle one; the inverter's own closed-loop
    current regulation handles magnetron inrush internally
  - **More HIGH duty = more power** (`INVERT_CMD=false` confirmed). The old
    "≤43 % duty to start" advice from a video was WRONG — under-driving gave
    "HV but no oscillation."
- Pin 3 (orange) tied to **1k → GND** sufficed on this board.
- **CONFIRMED WORKING** — magnetron oscillated under ESP32 drive.
- This board is borrowed from the kitchen oven. A twin (F606Y8X00AP) is the
  clean known-good path if the dual-IGBT effort stalls: runs on this firmware
  day one, no further tuning.

**Wiring (single-IGBT):**
```
GPIO4 -> CN701 pin1 (YELLOW)  command, push-pull TTL 222Hz/85%
GPIO5 -> CN701 pin3 (ORANGE)  optional 2nd signal (1k->GND also works)
GND   -> CN701 pin2 (BROWN)
GPIO7 <- zero-cross (monitor only)
GPIO6 <- capture tap (divider) for 'log' mode
GPIO1 = BTN ON/OFF ; GPIO2 = BTN power step
```

---

### 2. Panasonic F6645M301GP ("M3GP") — DUAL-IGBT — 🔧 TOPOLOGY PROVEN, TUNING PENDING

Dual-IGBT half-bridge board. **This is a 230–240 V board** (confirmed, see below).
Originally bought (×2) as a replacement for the NN-SN778S — that was a **mistake
on two axes**: wrong part family entirely (F606 vs F6645, single vs dual IGBT)
AND wrong voltage (230 V vs 120 V). Ironically these are the higher-power 240 V
boards that were actually wanted — accidentally the right thing.

- **Driver: `panasonic_dual_igbt_esp32s3/`**
- **Devices:** big switch **Toshiba GT50N322A** (1000 V / 50 A, FRD built in);
  small switch **Toshiba GT35J321** (600 V / 35 A). Half-bridge topology.
- **Controller IC:** Panasonic **AN47054A** class (undocumented). Runs closed-loop
  off DC-link / resonant-voltage / input-current sensing; generates the
  20–40 kHz IGBT gate switching itself. CN701 takes the LOW-FREQ ~220 Hz command,
  NOT the 20–40 kHz (that's an internal gate-drive frequency — common myth).
- **Resonant cap C703 = WF 395J 250V = 3.9 µF film**, sits IGBT-emitter to
  transformer primary. Measured 3.9 µF out of circuit → GOOD. (A resonant cap
  reads ~0 V at idle whether good or bad — only develops voltage during
  oscillation, so an idle 0 V reading is NOT a fault indicator.)
- Other caps: 505J/250V (~5 µF), WFK 184J/550V (180 nF across IGBT collectors).
  **No bulk electrolytic bus cap** — Panasonic design uses a small/unfiltered
  pulsating rectified rail by design.

**240 V CONFIRMATION (multiple independent sources):**
- Every parts listing cross-refs F6645M301GP to **230 V ovens**: NN-GT548M,
  NN-GD376S/GD576M, NN-GS, NN-K series (EU/Asia/Australia). No US 120 V model.
- Big IGBT GT50N322A = **1000 V**.
- Bridge rectifier = **D20SB80 = 800 V / 560 V RMS / 20 A** — built for a 230 V
  line (RMS ~240 V, peak ~340 V). A 120 V board would use a ~400 V bridge.
- At 120 V input → bus only ~163 V (measured) = half of the ~325–340 V it needs
  → under-voltage → "fires briefly then trips." Explains every earlier symptom.

**RESULT AT 240 V (the breakthrough):**
- Fed proper 240 V from the off-grid solar system (240 V double-breaker).
- Loaded `panasonic_dual_igbt_esp32s3`, typed `on` → **IT OSCILLATED.**
- This proves: 230 V board confirmed, command correct, feedback correct, board
  CAN run standalone on ESP32 drive.
- BUT after ~2–4 s: **POP** — the small IGBT (GT35J321) shorted.

**Why it popped (analysis — NOT simple thermal soak; it only ran 2–4 s on a
large heatsink):** more likely one of:
1. **Shoot-through** — half-bridge dead-time/timing off → both IGBTs briefly on
   → bus short through both → smallest device dies first. (µs-fast.)
2. **Over-current off-resonance** — resonant inverter; if driven off the tank's
   resonant point the IGBT current exceeds rating → fast junction failure.
3. **Overvoltage on the 600 V small IGBT** — at 340 V bus + resonant ringing, a
   transient can push the GT35J321 (only 600 V) past its limit → avalanche.
- All three trace to driving **open-loop** without the full closed-loop
  protection the AN47054A normally applies. The single-IGBT board tolerates
  open-loop because it's simpler; the dual half-bridge has shoot-through as a
  failure mode that only exists with two switches.
- NOTE: the two boards on hand had been **cross-swapped with parts between two
  damaged boards** (one had both IGBTs blown, the other a blown HV diode), so
  the board that popped was a hybrid of uncertain part health → its result is
  not fully trustworthy. → decision: buy NEW known-good boards to tune against.

**STATUS / NEXT (when new boards arrive):**
- Ordered new known-good F6645M301GP boards (AliExpress, "100% test working",
  ~$24.69 ea).
- Before firing a new board: set up a proper TUNING RIG —
  1. **Current limiting on the 240 V feed** (so a bad signal trips, not detonates
     the IGBT) — variac / resistive heater element / fast fuse.
  2. **Scope on the small IGBT gate (and/or its Vce)** — to SEE shoot-through
     (gate overlap) or overvoltage (Vce past 600 V) instead of tuning blind.
- Tuning procedure: start `p 40`, short 1–2 s bursts, watch gate scope + input
  current, dial up only when stable. Sweep feedback: try `sig2off` (free-run)
  vs the 110 Hz emulation, vary timing.
- Keep spare **GT35J321** and **GT50N322A** on hand (~$5–10 ea) — IGBT casualties
  during tuning are expected; replacing one is a 10-min job, not a reorder.
- WHEN REPLACING A POPPED IGBT: also diode-check the partner IGBT AND the
  gate-driver components on that half-bridge leg BEFORE repowering — a shorted
  half-bridge IGBT often takes the driver/partner with it; powering into a
  damaged leg pops the new device instantly.

**Wiring (dual-IGBT):**
```
GPIO4 -> CN701 pin1 (YELLOW)        command, push-pull TTL 222Hz
GPIO5 -> 1k -> CN701 pin3 (ORANGE)  OPEN-DRAIN 110Hz/50% feedback emulation
GND   -> CN701 pin2 (BROWN)
GPIO7 <- zero-cross (monitor only)
```
Pin 3 is an open-collector line the board pulls to 5 V (measured 5 V HIGH on
start). GPIO5 only SINKS / releases (never sources). The 1k series protects the
non-5V-tolerant GPIO when it's released into the board's 5 V pull-up.

Console: `on` `off` `p<10-95>` `f<hz>` `ss <duty> <ms>` `sson` `ssoff`
`sig2on` `sig2off` `log` `logdump` `zc` `status`. Defaults: soft-start 35 %/2 s
→ ramp → 70 %, pin3 feedback ON, 222 Hz.

---

### 3. LG EBR82899411 / EBR82899402 — 🅿️ PARKED (revisit while waiting on dual boards)

Inverter from an **LG MSER2090S NeoChef** (120 VAC / 1200 W, magnetron 2M286-21).
Service manual: `mser2090s.pdf` (P/NO MFL69902612).

- **Architecture (per service manual + bench):** main board sends a command over
  **CN3** → the inverter's LOCAL hot-side MICOM **U1 (Renesas R5F1076C)** runs the
  closed loop and generates the 24–70 kHz IGBT switching itself. Same
  division of labor as the Panasonic (slow command in, fast switching local).
- **BLOCKER:** U1 stays **dead until mains is applied** — its 5 V/15 V bias derives
  from the rectified mains via the on-board regulator (U3). So the command path
  CANNOT be bench-tested on 15 V logic bias alone; U1 isn't running. This is the
  one structural difference from the Panasonic and why the LG is the hardest:
  it needs a **live-mains hot-side bring-up** before the command path is even
  testable, and then still needs a capture of CN3 to learn the command.

**CN3 pinout (bench-confirmed):**
```
Pin1 = U4 LED side via 10k (status opto, not our concern)
Pin2 = U4 LED other side (status, not our concern)
Pin3 = PWM cathode-side via on-board 330R to U2 LED cathode (drive LOW to enable)
Pin4 = PWM anode-side via on-board 330R to U2 LED anode (drive HIGH to enable)
Pin5 = key gap (unpopulated)
Pin6 = GND
Pin7 = +15V input
```
PWM control scheme: hold pin4 at +5 V, PWM pin3 (LOW = LED on = power).
F-11 test (svc man p5-26): CN3 pins 6–7 read OPEN(OL) normally; short = fault
(power-rail check, NOT a comms-pair check).

**Diagnostic IC values (svc man p5-27, ±10 % = OK):**
```
U1 MICOM (R5F1076C) pin2->GND = 15-20k ; pin10->GND = 4.5k
U6 gate driver (2EDL05I06PF, Infineon) = 1.0 Mohm
U7 op-amp (SN358) = 4.5k
U8 dual comparator (KIA393F) = 1.0 Mohm
TH91 thermistor ~20k @ room temp (F-16/17 if bad)
IGBT Q1: ~10k G-E ; C-E diode 0.4-0.6V
Bridge D5: open/short ; HV diodes D8,D9: open/short
```
Components: U1=RL78 MICOM, U2=PWM input opto, U3=onboard 5V reg, U4=status opto,
U6=Infineon half-bridge gate driver, U7=SN358 dual op-amp, U8=KIA393F dual comp,
Q1=IGBT, ZD201=trigger zener, TH91=B/D temp thermistor, D5=bridge, D8/D9=HV doubler.

**Bench results so far:** cold side draws 23 mA at 15 V idle; U3 outputs solid 5 V;
U2 LED conducts (5 mA bump) when pin4→5V AND pin3→GND (PWM input path functional).
U4 LED shows 1.16 V drop at idle (status output active). SPICE note: driving U2
via emitter-follower collapses to ~72 mV; open-collector (pull-up to 5V) gives a
proper 1.6–4.4 V swing, and response is INVERTED (higher duty = lower volts).

**NEXT for LG:** the only way forward is to confirm U1 wakes on mains. Plan:
(1) verify U2 phototransistor side crosses the isolation barrier (LED off vs on);
(2) live-mains hot-side power-up to bring U1 alive (DANGEROUS — mains-referenced
MICOM probing); (3) then capture CN3 during a real start to learn the command.
This is the most dangerous and least-certain path of the three — only worth it
as a "while waiting" exercise, with full mains-safety discipline.

---

## Firmware notes (shared)

- ESP32-S3 Dev Module, Arduino core 2.x/3.x, 4 MB, PSRAM off, USB CDC On Boot.
- Same ESP harness across all builds — only the command wire lands on a different
  connector/pin per board.
- `log` mode = capture an oven's real command on GPIO6 (via divider). This is the
  method that cracked the single-IGBT board and is the intended method for any
  board where a working oven is available to capture from.
- Serial 115200.

## Hard-won lessons

1. **Capture beats guessing.** The single-IGBT board was solved by capturing the
   real command, not theorizing. The dual board is hard precisely because there's
   no working dual-IGBT oven on hand to capture from.
2. **CN701/CN3 takes a LOW-FREQ (~220 Hz) command.** The 20–40 kHz is the IGBT
   gate switching the board generates internally — do NOT inject it into the
   command input.
3. **Input voltage first.** A 230 V board fed 120 V fires-then-dies on
   under-voltage. Check bridge rating (D20SB80=800V→230V) and bus voltage
   (163 V measured = half of needed) before chasing the command.
4. **An idle resonant cap reads ~0 V whether good or bad** — measure capacitance
   out of circuit, don't trust an in-circuit idle voltage.
5. **Don't tune on cross-swapped damaged boards** — parts that pass a DMM diode
   check can still be degraded under 340 V; use known-good boards for tuning so
   results are trustworthy.
6. **Dual half-bridge is less forgiving than single** — shoot-through is a
   failure mode that only exists with two switches; tune with current limiting
   and a gate scope.
