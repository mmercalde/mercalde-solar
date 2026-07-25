# mercalde-solar

Monitoring and control for an off-grid solar power system in Rosarito, Baja
California (32.2910°N, 117.0015°W). In continuous operation since 2014.

A Raspberry Pi 5 polls Schneider Conext equipment over Modbus TCP, serves a
dashboard, and runs automatic generator start/stop against battery voltage.

---

## System

### Inverters

| Device | Slave ID | Notes |
|---|---|---|
| XW Pro 6848 Master | 10 | AC output, DC power, charger |
| XW+ 5548 | 11 | Fed by the Kubota generator |
| XW Pro 6848 Slave | 12 | Paralleled with the Master |

The two XW Pros run in parallel through a PDP. Large AC output voltage
calibration differences between them cause circulating current, so both are
held at near-identical values (Master 32840, Slave 32940).

### Solar

52 panels, 225 W each — 11.7 kW nominal — across three charge controllers.

| Channel | Slave ID | Array | Panels |
|---|---|---|---|
| MPPT 80 600 | 30 | Ground array (north of house) + terrace roof | 15 + 9 |
| MPPT 60 150 | 31 | House west hip + south hip, 3/2/1 pyramids | 6 + 6 |
| MPPT 60 150 | 170 | Generator building roof | 16 |

The house is 25 ft square with a pyramid hip roof at roughly 12°. The terrace
roof continues the south hip plane. The north ground array is shaded by the
house through the middle of winter, clearing only around solar noon.

### Battery

Roughly 96 kWh (~1790 Ah at 53.6 V), 15S configuration, three chemistries on a
common DC bus: LG NMC, Tesla 18650 modules, Chevy Bolt LG cells. Two BMS units
with **active** balancers, so trigger deltas of 10–15 mV are appropriate.

A Conext Battery Monitor (slave 191) measures true net battery current at the
shunt. Positive current is charging.

### Generators

| Generator | AGS ID | Feeds | Charge rate |
|---|---|---|---|
| MEP-803A | 51 | Both XW Pros | 100% |
| Kubota | 50 | XW+ 5548 only | 70% (parallel Magnum charger limit) |

Kubota is capped at 70% because of the parallel Magnum charger.

---

## Repository layout

```
pi5/     Raspberry Pi 5 — dashboard, Modbus layer, systemd unit, udev rule
vps/     Vultr VPS — nginx reverse proxy, Alexa webhook
```

`pi5/app.py` is deployed to `/home/michael/solar_dashboard/app.py` and run by
gunicorn as `app:app` on port 8080.

---

## Modbus notes

Schneider's implementation has two quirks that break standard clients:

- **MSW-first 32-bit word ordering.**
- **A new TCP connection is required per request.** pymodbus's connection reuse
  does not work; `schneider_modbus.py` opens a fresh socket each time.

Connect-phase timeout is 3.0 s with one retry and 100 ms backoff. Response
timeout stays at 1.0 s so a genuinely dead device still fails fast. The longer
connect timeout absorbs microbursts on the switch shared with the Conext
Gateway.

### Registers worth knowing

| Register | Device | Meaning |
|---|---|---|
| `0x0043` | AGS | Auto Generator Action — 9 = Running, 10 = Stopped |
| `0x004D` | AGS | Generator Mode — 0 off, 1 on, 2 auto |
| `0x0048` | Battery Monitor | Battery current, signed, positive = charging |
| `0x004C` | Battery Monitor | State of charge |
| `0x0054` | XW | DC Power, signed net |
| `0x005E` | XW | Charge DC Power — **AC-side charging only** |
| `0x016F` | XW | Max charge rate |
| `0x0164` | XW | Charger enable |
| `0x0050` | MPPT | PV power |
| `0x005C` | MPPT | DC output power (bus power, not net to battery) |

Two of these have bitten us:

`0x016F` rests at its configured maximum permanently, so it says nothing about
whether a generator is running. Use `0x0043`.

`0x005E` only measures charging from generator AC. It correctly reads zero
while solar charges the bank. For true battery flow use the Battery Monitor.

---

## Generator control

Automatic start/stop runs against battery voltage with per-generator start and
stop thresholds, maximum runtime and cooldown. Input validation enforces a
start range of 45–60 V, a stop ceiling of 63 V and a minimum 1.5 V gap.

**Ramp-down on stop.** Abruptly dropping a 10 kW+ charging load causes
transients that reboot sensitive equipment. Lithium holds high charge
acceptance until nearly full, so Schneider's native ramp-down never engages.
Instead the software steps the charge rate 100 → 50 → 25 → 0%, disables the
chargers, then issues the AGS stop.

**K2 relay limitation.** Asynchronous K2 relay opening between parallel XW Pro
units is documented Schneider behaviour — the last relay to open carries the
full load current. An external contactor (TeSys LC1D150) would fix it but is
incompatible with Generator Support mode.

---

## Network

```
Pi 5      192.168.1.53   eth0
          192.168.3.10   eth1  (TP-Link USB adapter)
          192.168.6.1    wlan0 AP
Schneider 192.168.3.x    Gateway at 192.168.3.131:503
WiFi      192.168.6.x    ESP32 display at 192.168.6.73
VPS       45.32.131.224  nginx + Let's Encrypt, WireGuard 10.8.0.1
Pi via WireGuard         10.8.0.2
```

The VPS reverse-proxies `mercalde-solar.org`. Note the catch-all `location /`
forwards to InsightLocal, so **every dashboard endpoint needs its own explicit
location block** or it silently lands on InsightLocal instead.

---

## Deploying

```bash
scp app.py pi5:/tmp/app_new.py
ssh pi5 'wc -c /tmp/app_new.py && \
  python3 -c "import ast;ast.parse(open(\"/tmp/app_new.py\").read());print(\"syntax OK\")" && \
  cd /home/michael/solar_dashboard && \
  cp -p app.py app.py.backup_$(date +%Y%m%d-%H%M) && \
  cp /tmp/app_new.py app.py && \
  sudo systemctl restart solar-dashboard && sleep 5 && \
  systemctl is-active solar-dashboard'
```

Size and syntax are checked on the Pi *before* the live file is touched, so a
truncated transfer cannot take the dashboard down.

Rollback:

```bash
ssh pi5 'cd /home/michael/solar_dashboard && \
  cp $(ls -t app.py.backup_* | head -1) app.py && \
  sudo systemctl restart solar-dashboard'
```

Check a restart is safe first — both generators should read action 10:

```bash
ssh pi5 'curl -s localhost:8080/data | python3 -m json.tool | grep -Ei "Action|pollErrors"'
```

---

## Recovery

**TP-Link USB adapter re-enumerates as a CD-ROM after a power event.**
The Realtek 2357:8151 presents as mass storage instead of ethernet:

```bash
sudo usb_modeswitch -v 2357 -p 8151 -R
```

`pi5/99-tplink-usb-lan.rules` handles this automatically.

**WireGuard must be enabled, not just started**, or it will not survive a
reboot:

```bash
sudo systemctl enable wg-quick@wg0
```

---

## Configuration

`config.json` holds thresholds and the Telegram bot token and is **not**
committed. Copy `pi5/config.example.json` and fill it in.

---

## Endpoints

| Path | Purpose |
|---|---|
| `/` | Dashboard |
| `/data` | Live JSON |
| `/config` | Read and update settings |
| `/setgen`, `/stopgen` | Generator control |
| `/acdiag`, `/acdiag/stream`, `/acdiag/log` | Inverter sync diagnostics |
| `/registers`, `/readreg`, `/writereg` | Raw Modbus access |
| `/testtelegram` | Send a test alert |

The AC Diagnostic tool captures phase, voltage and frequency deltas between the
paralleled XW Pros — built for investigating generator stop transients.
