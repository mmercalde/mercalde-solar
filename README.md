# mercalde-solar

Autonomous solar power management system for a Schneider Electric Conext off-grid inverter system with lithium battery bank.

## System Overview

- **3 Schneider Electric Conext inverters**: XW Pro 6848 Master (ID 10), XW Pro 6848 Slave (ID 12), XW+ 5548 (ID 11)
- **2 MPPT charge controllers** + Battery Monitor + Conext Gateway
- **2 backup generators**: MEP-803A and Kubota diesel, each with Conext AGS units
- **15S lithium battery pack** (Chevy Bolt LG cells + Tesla modules, ~54V nominal)
- **~52 solar panels** (~13kW total array)

## Architecture

```
Pi5 Flask Dashboard (primary controller)
    └── Modbus TCP → Conext Gateway (192.168.3.131:503)
            └── Xanbus → Inverters, MPPTs, AGS units, Battery Monitor

VPS (45.32.131.224)
    └── nginx reverse proxy + SSL termination → Pi5
    └── Alexa skill endpoint → Pi5 generator control

ESP8266 (backup controller, port 8081)
```

## Repository Structure

```
mercalde-solar/
├── pi5/                        # Raspberry Pi 5 Flask dashboard
│   ├── app.py                  # Main application (V2.2)
│   ├── schneider_modbus.py     # Custom Modbus TCP implementation
│   ├── requirements.txt        # Python dependencies
│   ├── setup.sh                # Setup script
│   └── solar-dashboard.service # systemd service file
├── vps/                        # Vultr VPS files
│   └── alexa_solar.py          # Alexa skill Flask endpoint
├── esp8266/                    # ESP8266 backup controller (archived)
└── docs/                       # Documentation
```

## Key Features

- **Real-time monitoring** of inverters, battery, solar arrays via Modbus TCP
- **Automatic generator control** with configurable voltage thresholds
- **Graceful ramp-down** before generator stop (prevents lithium battery transients)
- **Concurrent sequence protection** (V2.2) — prevents race conditions in auto generator control
- **Remote access** via mercalde-solar.org (HTTPS, WireGuard VPN)
- **Alexa voice control** for generator start/stop
- **Configurable settings** via web dashboard (charge rates, thresholds, ramp timing)

## Schneider Modbus Notes

Schneider Conext devices require a **custom Modbus TCP implementation**:
- MSW-first 32-bit word ordering (non-standard)
- New TCP connection per request (persistent connections fail)
- Standard libraries (pymodbus, etc.) do not work reliably

See `pi5/schneider_modbus.py` for the implementation.

## Network

| Device | IP | Role |
|--------|----|------|
| Pi5 | 192.168.3.x | Dashboard, WireGuard gateway |
| Conext Gateway | 192.168.3.131 | Modbus TCP bridge to Xanbus |
| VPS | 45.32.131.224 | SSL termination, public access |
| KAMRUI Mini PC | 192.168.3.152 | Alexa endpoint |

## Related

- [SchneiderModbusTCP](https://github.com/mmercalde/SchneiderModbusTCP) — Arduino library for Schneider Conext Modbus TCP (ESP32/ESP8266)
