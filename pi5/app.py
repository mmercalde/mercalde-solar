#!/usr/bin/env python3
"""
Solar Dashboard - Pi 5 Flask Application V2.8
Full autonomous control with persistent settings

FIXES in V2.4:
  - AC Diagnostic tool integrated into dashboard
  - /acdiag         - single snapshot (JSON)
  - /acdiag/stream  - N readings over time window (JSON)
  - /acdiag/log     - last N saved readings (JSON)
  - /acdiag/log/clear - clear log
  - Dashboard UI: AC Diag section with snapshot button,
    stream capture button, and log viewer table

FIXES in V2.3:
  - Telegram alerts for: auto-start, auto-stop, AUTO mode failed,
    battery low, AGS offline (transition only)
  - Poll errors trigger immediate Telegram alert
  - Scrollable error/event log in dashboard
  - Error messages visible in dashboard config panel
  - Token and chat ID stored in config.json

FIXES in V2.2:
  - Added sequence-in-progress guards (_stopping/_starting flags) to prevent
    concurrent generator start/stop sequences which caused AGS FC 0x83 faults

FIXES in V2.1:
  - Reads current charge rates from Schneider inverters via Modbus
  - Proper config file initialization and loading
  - Better error handling and debugging
  - Toggle buttons work correctly

Endpoints:
  /             - Dashboard HTML
  /data         - JSON API (system status)
  /setgen       - Generator control
  /stopgen      - Graceful generator stop with ramp-down
  /setmpptmode  - MPPT charge mode control
  /config       - Get/Set configuration
  /testtelegram - Send test Telegram message
  /registers    - Modbus Register Tool
  /readreg      - Read single register
  /writereg     - Write single register
  /readtransfer - Batch read transfer/ramp registers
  /readags      - Batch read AGS registers
  /acdiag       - AC diagnostic snapshot (V2.4)
  /acdiag/stream - AC diagnostic stream capture (V2.4)
  /acdiag/log   - AC diagnostic log viewer (V2.4)
  /acdiag/log/clear - Clear AC diagnostic log (V2.4)
"""

import os
import json
import time
import threading
import logging
import copy
import requests as http_requests
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string, send_file

from schneider_modbus import SchneiderModbusTCP

# --- Configuration ---
MODBUS_HOST = "192.168.3.131"
MODBUS_PORT = 503
POLL_INTERVAL = 5  # seconds
CONFIG_FILE = "/home/michael/solar_dashboard/config.json"

# Slave IDs
INVERTER_1_ID = 10   # XW Pro 6848 Master
INVERTER_2_ID = 12   # XW Pro 6848 Slave
INVERTER_3_ID = 11   # XW+ 5548 (Kubota system)
BATTERY_MONITOR_ID = 191
MPPT_80_ID = 170
SOUTH_ARRAY_ID = 31
WEST_ARRAY_ID = 30
AGS_MEP803A_ID = 51
AGS_KUBOTA_ID = 50

# Register addresses
REG_AC_POWER = 0x009A
REG_AC_CURRENT = 0x0096
REG_BATTERY_VOLTAGE = 0x0046
REG_BATTERY_SOC = 0x004C
REG_PV_VOLTAGE = 0x004C
REG_PV_CURRENT = 0x004E
REG_PV_POWER = 0x0050
REG_CHARGER_STATUS = 0x0049
REG_GENERATOR_MODE = 0x004D
REG_GENERATOR_ACTION = 0x0043   # AGS spec 2.3: 9=Running, 10=Stopped

# Conext Battery Monitor (slave 191, port 503) - shunt measurement.
# Positive current = charging, verified against InsightLocal 2026-07-25.
BATTERY_MONITOR_ID = 191
REG_BM_VOLTAGE   = 0x0046   # uint32, V  x0.001
REG_BM_CURRENT   = 0x0048   # sint32, A  x0.001
REG_BM_SOC       = 0x004C   # uint32, %
REG_BM_AH_REMAIN = 0x0058   # uint32, Ah
REG_BM_TIME_DISCH= 0x0060   # uint32, minutes
REG_CHARGE_MODE_FORCE = 0x00AA
REG_CHARGER_ENABLE = 0x0164
REG_MAX_CHARGE_RATE = 0x016F
REG_OPERATING_MODE = 0x0166
REG_FORCE_CHARGER_STATE = 0x0165
REG_CHARGE_DC_POWER = 0x005E

# V2.4: AC Diagnostic registers (503 spec)
REG_AC_LOAD_L1_VOLTAGE  = 0x008E  # uint32, scale 0.001 V
REG_AC_LOAD_L2_VOLTAGE  = 0x0090  # uint32, scale 0.001 V
REG_AC_LOAD_FREQUENCY   = 0x0098  # uint16, scale 0.01 Hz
# (REG_AC_POWER and REG_AC_CURRENT already defined above)

# V2.4: AC Diagnostic log
AC_DIAG_LOG_FILE = "/home/michael/solar_dashboard/ac_diag_log.json"
AC_DIAG_MAX_ENTRIES = 5000
ac_diag_lock = threading.Lock()

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# --- Default Configuration ---
DEFAULT_CONFIG = {
    "autoGenEnabled": True,
    "mep803a": {
        "startVoltage": 51.5,
        "stopVoltage": 55.0,
        "chargeRate": 100,
        "maxRuntime": 120,
        "cooldown": 5
    },
    "kubota": {
        "startVoltage": 52.3,
        "stopVoltage": 55.0,
        "chargeRate": 70,
        "maxRuntime": 120,
        "cooldown": 5
    },
    "rampDown": {
        "stepDelay": 15,
        "zeroHoldTime": 120
    },
    "autoRebootHours": 2,
    "telegram": {
        "token": "",
        "chatId": "",
        "enabled": False
    }
}

# --- Global State ---
config = copy.deepcopy(DEFAULT_CONFIG)
config_lock = threading.Lock()

system_data = {
    "acPower1": 0, "acCurrent1": 0.0,
    "acPower2": 0, "acCurrent2": 0.0,
    "batteryVoltage": 0.0, "batterySOC": 0,
    "mppt80PVPower": 0, "mppt80PVVoltage": 0.0, "mppt80PVCurrent": 0.0, "mppt80ChargeStatus": 0,
    "southArrayPVPower": 0, "southArrayPVVoltage": 0.0, "southArrayPVCurrent": 0.0, "southArrayChargeStatus": 0,
    "westArrayPVPower": 0, "westArrayPVVoltage": 0.0, "westArrayPVCurrent": 0.0, "westArrayChargeStatus": 0,
    "mep803aMode": 0, "kubotaMode": 0,
    "mep803aAction": 10, "kubotaAction": 10,
    "battPower": None, "battCurrent": None, "battSocBM": None,
    "battAhRemaining": None, "battMinToDischarge": None, "battMonitorOnline": False,
    "lastUpdate": "00:00:00", "clockTime": "--:--:--", "pollErrors": 0,
    "mepChargeRateLive": 0, "kubotaChargeRateLive": 0,
    "chargePower1": 0, "chargePower2": 0, "chargePower3": 0,
    "mepAgsOnline": True, "kubotaAgsOnline": True,
}
data_lock = threading.Lock()
start_time = time.time()

# Auto-gen state
auto_gen_state = {
    "mep803a_running": False, "mep803a_start_time": None,
    "mep803a_cooldown_until": 0, "mep803a_low_voltage_since": None,
    "mep803a_stopping": False, "mep803a_starting": False,
    "kubota_running": False, "kubota_start_time": None,
    "kubota_cooldown_until": 0, "kubota_low_voltage_since": None,
    "kubota_stopping": False, "kubota_starting": False,
    "last_event": "", "events": []
}
auto_gen_lock = threading.Lock()

# V2.3: Alert state tracking
alert_state = {
    "mep803a_offline": False,
    "kubota_offline": False,
    "poll_error_alerted": False,
    "battery_low_alerted": False,
}
alert_lock = threading.Lock()

modbus = SchneiderModbusTCP()

# --- Telegram ---
def send_telegram(message):
    """Send Telegram message in background thread."""
    def _send():
        with config_lock:
            tg = config.get("telegram", {})
            token = tg.get("token", "")
            chat_id = tg.get("chatId", "")
            enabled = tg.get("enabled", False)
        if not enabled or not token or not chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            resp = http_requests.post(url, json={
                "chat_id": chat_id,
                "text": f"☀️ Solar Monitor\n{message}",
                "parse_mode": "HTML"
            }, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"Telegram send failed: {resp.status_code}")
            else:
                logger.info(f"Telegram sent: {message[:60]}")
        except Exception as e:
            logger.warning(f"Telegram error: {e}")
    threading.Thread(target=_send, daemon=True).start()

def test_telegram():
    with config_lock:
        tg = config.get("telegram", {})
        token = tg.get("token", "")
        chat_id = tg.get("chatId", "")
    if not token or not chat_id:
        return False, "Token or Chat ID not configured"
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = http_requests.post(url, json={
            "chat_id": chat_id,
            "text": "☀️ Solar Monitor\n✅ Test message - Telegram alerts are working!"
        }, timeout=10)
        if resp.status_code == 200:
            return True, "Test message sent successfully"
        return False, f"Failed: {resp.text}"
    except Exception as e:
        return False, str(e)

# --- Config ---
def load_config():
    global config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                loaded = json.load(f)
            with config_lock:
                config = copy.deepcopy(DEFAULT_CONFIG)
                if 'autoGenEnabled' in loaded:
                    config['autoGenEnabled'] = loaded['autoGenEnabled']
                if 'autoRebootHours' in loaded:
                    config['autoRebootHours'] = loaded['autoRebootHours']
                for section in ['mep803a', 'kubota', 'rampDown']:
                    if section in loaded and isinstance(loaded[section], dict):
                        for key, value in loaded[section].items():
                            if key in config[section]:
                                config[section][key] = value
                if 'telegram' in loaded and isinstance(loaded['telegram'], dict):
                    for key, value in loaded['telegram'].items():
                        if key in config['telegram']:
                            config['telegram'][key] = value
            logger.info(f"Config loaded — autoGen:{config['autoGenEnabled']} telegram:{config['telegram']['enabled']}")
        else:
            with config_lock:
                config = copy.deepcopy(DEFAULT_CONFIG)
            save_config()
            logger.info("Created default config")
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        with config_lock:
            config = copy.deepcopy(DEFAULT_CONFIG)

def save_config():
    try:
        config_dir = os.path.dirname(CONFIG_FILE)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)
        with config_lock:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
        logger.info("Config saved")
        return True
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return False

def log_event(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    event = f"{timestamp} - {message}"
    with auto_gen_lock:
        auto_gen_state["last_event"] = event
        auto_gen_state["events"].append(event)
        if len(auto_gen_state["events"]) > 100:
            auto_gen_state["events"] = auto_gen_state["events"][-100:]
    logger.info(f"EVENT: {event}")

# --- Charger Control ---
def set_charge_rate_single(slave_id, rate):
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_MAX_CHARGE_RATE, rate)
    if success:
        logger.info(f"Inverter {slave_id} charge rate set to {rate}%")
    return success

def set_charger_enabled_single(slave_id, enabled):
    value = 1 if enabled else 0
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_CHARGER_ENABLE, value)
    if success:
        logger.info(f"Inverter {slave_id} charger {'enabled' if enabled else 'disabled'}")
    return success

def force_charger_state_single(slave_id, state):
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_FORCE_CHARGER_STATE, state)
    if success:
        logger.info(f"Inverter {slave_id} forced to state {state}")
    return success

def set_operating_mode_single(slave_id, mode):
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_OPERATING_MODE, mode)
    if success:
        logger.info(f"Inverter {slave_id} operating mode set to {mode}")
    return success

# --- MEP-803A ---
def ensure_mep_chargers_ready():
    with config_lock:
        rate = config["mep803a"]["chargeRate"]
    logger.info(f">>> Activating MEP-803A chargers at {rate}%...")
    set_operating_mode_single(INVERTER_1_ID, 3)
    set_operating_mode_single(INVERTER_2_ID, 3)
    time.sleep(0.3)
    set_charger_enabled_single(INVERTER_1_ID, True)
    set_charger_enabled_single(INVERTER_2_ID, True)
    time.sleep(0.3)
    set_charge_rate_single(INVERTER_1_ID, rate)
    set_charge_rate_single(INVERTER_2_ID, rate)
    time.sleep(0.3)
    force_charger_state_single(INVERTER_1_ID, 1)
    force_charger_state_single(INVERTER_2_ID, 1)
    log_event(f"MEP chargers enabled @ {rate}%")

def ramp_down_mep():
    with config_lock:
        step_delay = config["rampDown"]["stepDelay"]
        zero_hold = config["rampDown"]["zeroHoldTime"]
    logger.info(">>> Ramping down MEP-803A chargers...")
    log_event("MEP ramp-down started")
    for rate in [75, 50, 25, 10, 0]:
        set_charge_rate_single(INVERTER_1_ID, rate)
        set_charge_rate_single(INVERTER_2_ID, rate)
        time.sleep(step_delay)
    set_charger_enabled_single(INVERTER_1_ID, False)
    set_charger_enabled_single(INVERTER_2_ID, False)
    time.sleep(zero_hold)
    log_event("MEP ramp-down complete")

def restore_mep_chargers():
    with config_lock:
        rate = config["mep803a"]["chargeRate"]
    set_charger_enabled_single(INVERTER_1_ID, True)
    set_charger_enabled_single(INVERTER_2_ID, True)
    time.sleep(0.3)
    set_charge_rate_single(INVERTER_1_ID, rate)
    set_charge_rate_single(INVERTER_2_ID, rate)
    time.sleep(0.3)
    force_charger_state_single(INVERTER_1_ID, 1)
    force_charger_state_single(INVERTER_2_ID, 1)
    log_event(f"MEP chargers restored @ {rate}%")

# --- Kubota ---
def ensure_kubota_chargers_ready():
    with config_lock:
        rate = config["kubota"]["chargeRate"]
    logger.info(f">>> Activating Kubota charger at {rate}%...")
    set_operating_mode_single(INVERTER_3_ID, 3)
    time.sleep(0.3)
    set_charger_enabled_single(INVERTER_3_ID, True)
    time.sleep(0.3)
    set_charge_rate_single(INVERTER_3_ID, rate)
    time.sleep(0.3)
    force_charger_state_single(INVERTER_3_ID, 1)
    log_event(f"Kubota charger enabled @ {rate}%")

def ramp_down_kubota():
    with config_lock:
        step_delay = config["rampDown"]["stepDelay"]
        zero_hold = config["rampDown"]["zeroHoldTime"]
    logger.info(">>> Ramping down Kubota charger...")
    log_event("Kubota ramp-down started")
    for rate in [50, 25, 10, 0]:
        set_charge_rate_single(INVERTER_3_ID, rate)
        time.sleep(step_delay)
    set_charger_enabled_single(INVERTER_3_ID, False)
    time.sleep(zero_hold)
    log_event("Kubota ramp-down complete")

def restore_kubota_chargers():
    with config_lock:
        rate = config["kubota"]["chargeRate"]
    set_charger_enabled_single(INVERTER_3_ID, True)
    time.sleep(0.3)
    set_charge_rate_single(INVERTER_3_ID, rate)
    time.sleep(0.3)
    force_charger_state_single(INVERTER_3_ID, 1)
    log_event(f"Kubota charger restored @ {rate}%")

# --- Generator Control ---
def start_generator(gen_type):
    if gen_type == "mep803a":
        with auto_gen_lock:
            if auto_gen_state["mep803a_starting"] or auto_gen_state["mep803a_stopping"]:
                logger.warning("MEP-803A sequence already in progress, skipping start")
                return False
            auto_gen_state["mep803a_starting"] = True
        try:
            ensure_mep_chargers_ready()
            success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, AGS_MEP803A_ID, REG_GENERATOR_MODE, 1)
            if success:
                with auto_gen_lock:
                    auto_gen_state["mep803a_running"] = True
                    auto_gen_state["mep803a_start_time"] = time.time()
                log_event("MEP-803A started")
                send_telegram("🔧 <b>MEP-803A Generator STARTED</b>\nAuto-start triggered by low battery voltage.")
            return success
        finally:
            with auto_gen_lock:
                auto_gen_state["mep803a_starting"] = False

    elif gen_type == "kubota":
        with auto_gen_lock:
            if auto_gen_state["kubota_starting"] or auto_gen_state["kubota_stopping"]:
                logger.warning("Kubota sequence already in progress, skipping start")
                return False
            auto_gen_state["kubota_starting"] = True
        try:
            ensure_kubota_chargers_ready()
            success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, AGS_KUBOTA_ID, REG_GENERATOR_MODE, 1)
            if success:
                with auto_gen_lock:
                    auto_gen_state["kubota_running"] = True
                    auto_gen_state["kubota_start_time"] = time.time()
                log_event("Kubota started")
                send_telegram("🔧 <b>Kubota Generator STARTED</b>\nAuto-start triggered by low battery voltage.")
            return success
        finally:
            with auto_gen_lock:
                auto_gen_state["kubota_starting"] = False
    return False

def stop_generator(gen_type, graceful=True):
    if gen_type == "mep803a":
        with auto_gen_lock:
            if auto_gen_state["mep803a_stopping"] or auto_gen_state["mep803a_starting"]:
                logger.warning("MEP-803A sequence already in progress, skipping stop")
                return False
            auto_gen_state["mep803a_stopping"] = True
        try:
            if graceful:
                ramp_down_mep()
            success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, AGS_MEP803A_ID, REG_GENERATOR_MODE, 0)
            if success:
                log_event("MEP-803A stopped, setting to AUTO...")
                time.sleep(5)
                auto_success = False
                for attempt in range(3):
                    auto_success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, AGS_MEP803A_ID, REG_GENERATOR_MODE, 2)
                    if auto_success:
                        logger.info(f"MEP-803A set to AUTO on attempt {attempt + 1}")
                        break
                    logger.warning(f"MEP-803A AUTO attempt {attempt + 1} failed, retrying...")
                    time.sleep(2)
                if not auto_success:
                    log_event("MEP-803A FAILED to set AUTO after 3 attempts!")
                    send_telegram("⚠️ <b>MEP-803A AUTO Mode FAILED</b>\nCould not set to AUTO after 3 attempts.\nManual intervention required.")
                else:
                    send_telegram("✅ <b>MEP-803A Generator STOPPED</b>\nAuto-stop complete. Set to AUTO mode.")
                restore_mep_chargers()
                with auto_gen_lock:
                    auto_gen_state["mep803a_running"] = False
                    auto_gen_state["mep803a_start_time"] = None
                    with config_lock:
                        cooldown = config["mep803a"]["cooldown"]
                    auto_gen_state["mep803a_cooldown_until"] = time.time() + (cooldown * 60)
                log_event("MEP-803A stopped → AUTO" if auto_success else "MEP-803A stopped but AUTO FAILED")
            return success
        finally:
            with auto_gen_lock:
                auto_gen_state["mep803a_stopping"] = False

    elif gen_type == "kubota":
        with auto_gen_lock:
            if auto_gen_state["kubota_stopping"] or auto_gen_state["kubota_starting"]:
                logger.warning("Kubota sequence already in progress, skipping stop")
                return False
            auto_gen_state["kubota_stopping"] = True
        try:
            if graceful:
                ramp_down_kubota()
            success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, AGS_KUBOTA_ID, REG_GENERATOR_MODE, 0)
            if success:
                log_event("Kubota stopped, setting to AUTO...")
                time.sleep(5)
                auto_success = False
                for attempt in range(3):
                    auto_success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, AGS_KUBOTA_ID, REG_GENERATOR_MODE, 2)
                    if auto_success:
                        logger.info(f"Kubota set to AUTO on attempt {attempt + 1}")
                        break
                    logger.warning(f"Kubota AUTO attempt {attempt + 1} failed, retrying...")
                    time.sleep(2)
                if not auto_success:
                    log_event("Kubota FAILED to set AUTO after 3 attempts!")
                    send_telegram("⚠️ <b>Kubota AUTO Mode FAILED</b>\nCould not set to AUTO after 3 attempts.\nManual intervention required.")
                else:
                    send_telegram("✅ <b>Kubota Generator STOPPED</b>\nAuto-stop complete. Set to AUTO mode.")
                restore_kubota_chargers()
                with auto_gen_lock:
                    auto_gen_state["kubota_running"] = False
                    auto_gen_state["kubota_start_time"] = None
                    with config_lock:
                        cooldown = config["kubota"]["cooldown"]
                    auto_gen_state["kubota_cooldown_until"] = time.time() + (cooldown * 60)
                log_event("Kubota stopped → AUTO" if auto_success else "Kubota stopped but AUTO FAILED")
            return success
        finally:
            with auto_gen_lock:
                auto_gen_state["kubota_stopping"] = False
    return False

# --- Auto Generator Control ---
def check_auto_generator():
    with config_lock:
        if not config.get("autoGenEnabled", False):
            return
        mep_cfg = config["mep803a"]
        kub_cfg = config["kubota"]

    with data_lock:
        voltage = system_data.get("batteryVoltage", 0)
        mep_mode = system_data.get("mep803aMode", 0)
        kubota_mode = system_data.get("kubotaMode", 0)

    if voltage <= 0:
        return

    current_time = time.time()

    with alert_lock:
        if voltage <= min(mep_cfg["startVoltage"], kub_cfg["startVoltage"]) and mep_mode == 0 and kubota_mode == 0:
            if not alert_state["battery_low_alerted"]:
                alert_state["battery_low_alerted"] = True
                send_telegram(f"🔋 <b>Battery Low</b>\nVoltage: {voltage}V\nBoth generators not running. Auto-start pending.")
        else:
            alert_state["battery_low_alerted"] = False

    with auto_gen_lock:
        mep_is_running = (mep_mode == 1)

        if not mep_is_running and voltage <= mep_cfg["startVoltage"]:
            if current_time > auto_gen_state["mep803a_cooldown_until"]:
                if auto_gen_state["mep803a_low_voltage_since"] is None:
                    auto_gen_state["mep803a_low_voltage_since"] = current_time
                elif current_time - auto_gen_state["mep803a_low_voltage_since"] >= 60:
                    if not auto_gen_state["mep803a_starting"] and not auto_gen_state["mep803a_stopping"]:
                        logger.info(f"AUTO: Starting MEP-803A (voltage {voltage}V <= {mep_cfg['startVoltage']}V)")
                        threading.Thread(target=start_generator, args=("mep803a",), daemon=True).start()
                        auto_gen_state["mep803a_low_voltage_since"] = None
                    else:
                        logger.info("AUTO: MEP-803A sequence in progress, skipping start trigger")
        else:
            auto_gen_state["mep803a_low_voltage_since"] = None

        if mep_is_running:
            auto_gen_state["mep803a_running"] = True
            should_stop = False
            reason = ""
            if voltage >= mep_cfg["stopVoltage"]:
                should_stop = True
                reason = f"voltage {voltage}V >= {mep_cfg['stopVoltage']}V"
            elif auto_gen_state["mep803a_start_time"]:
                runtime_min = (current_time - auto_gen_state["mep803a_start_time"]) / 60
                if runtime_min >= mep_cfg["maxRuntime"]:
                    should_stop = True
                    reason = f"max runtime {mep_cfg['maxRuntime']}min reached"
            if should_stop:
                if not auto_gen_state["mep803a_stopping"] and not auto_gen_state["mep803a_starting"]:
                    logger.info(f"AUTO: Stopping MEP-803A ({reason})")
                    threading.Thread(target=stop_generator, args=("mep803a", True), daemon=True).start()
                else:
                    logger.info("AUTO: MEP-803A sequence in progress, skipping stop trigger")

        kubota_is_running = (kubota_mode == 1)

        if not kubota_is_running and not mep_is_running and voltage <= kub_cfg["startVoltage"]:
            if current_time > auto_gen_state["kubota_cooldown_until"]:
                if auto_gen_state["kubota_low_voltage_since"] is None:
                    auto_gen_state["kubota_low_voltage_since"] = current_time
                elif current_time - auto_gen_state["kubota_low_voltage_since"] >= 60:
                    if not auto_gen_state["kubota_starting"] and not auto_gen_state["kubota_stopping"]:
                        logger.info(f"AUTO: Starting Kubota (voltage {voltage}V <= {kub_cfg['startVoltage']}V)")
                        threading.Thread(target=start_generator, args=("kubota",), daemon=True).start()
                        auto_gen_state["kubota_low_voltage_since"] = None
                    else:
                        logger.info("AUTO: Kubota sequence in progress, skipping start trigger")
        else:
            auto_gen_state["kubota_low_voltage_since"] = None

        if kubota_is_running:
            auto_gen_state["kubota_running"] = True
            should_stop = False
            reason = ""
            if voltage >= kub_cfg["stopVoltage"]:
                should_stop = True
                reason = f"voltage {voltage}V >= {kub_cfg['stopVoltage']}V"
            elif auto_gen_state["kubota_start_time"]:
                runtime_min = (current_time - auto_gen_state["kubota_start_time"]) / 60
                if runtime_min >= kub_cfg["maxRuntime"]:
                    should_stop = True
                    reason = f"max runtime {kub_cfg['maxRuntime']}min reached"
            if should_stop:
                if not auto_gen_state["kubota_stopping"] and not auto_gen_state["kubota_starting"]:
                    logger.info(f"AUTO: Stopping Kubota ({reason})")
                    threading.Thread(target=stop_generator, args=("kubota", True), daemon=True).start()
                else:
                    logger.info("AUTO: Kubota sequence in progress, skipping stop trigger")

# --- V2.3: AGS offline detection ---
def check_ags_status(mep_ok, kubota_ok):
    with alert_lock:
        if not mep_ok and not alert_state["mep803a_offline"]:
            alert_state["mep803a_offline"] = True
            log_event("⚠️ MEP-803A AGS went OFFLINE (FC 0x83)")
            send_telegram("🚨 <b>MEP-803A AGS OFFLINE</b>\nModbus FC 0x83 error detected.\nXanbus node may need physical reconnect.")
        elif mep_ok and alert_state["mep803a_offline"]:
            alert_state["mep803a_offline"] = False
            log_event("✅ MEP-803A AGS back ONLINE")
            send_telegram("✅ <b>MEP-803A AGS back ONLINE</b>")

        if not kubota_ok and not alert_state["kubota_offline"]:
            alert_state["kubota_offline"] = True
            log_event("⚠️ Kubota AGS went OFFLINE (FC 0x83)")
            send_telegram("🚨 <b>Kubota AGS OFFLINE</b>\nModbus FC 0x83 error detected.\nXanbus node may need physical reconnect.")
        elif kubota_ok and alert_state["kubota_offline"]:
            alert_state["kubota_offline"] = False
            log_event("✅ Kubota AGS back ONLINE")
            send_telegram("✅ <b>Kubota AGS back ONLINE</b>")

# --- V2.4: AC Diagnostic Functions ---
def read_inverter_ac_diag(slave_id):
    """Read AC diagnostic registers for one inverter."""
    result = {"id": slave_id}

    # L1 Voltage - uint32, 0.001 V
    v = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, slave_id, REG_AC_LOAD_L1_VOLTAGE)
    result["voltage_l1"] = round(v * 0.001, 3) if v is not None else None

    # L2 Voltage - uint32, 0.001 V
    v = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, slave_id, REG_AC_LOAD_L2_VOLTAGE)
    result["voltage_l2"] = round(v * 0.001, 3) if v is not None else None

    # Frequency - uint16, 0.01 Hz
    v = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_AC_LOAD_FREQUENCY)
    result["frequency"] = round(v * 0.01, 2) if v is not None else None

    # AC Load Power - sint32, 1.0 W (already defined as REG_AC_POWER)
    v = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, slave_id, REG_AC_POWER)
    result["power_w"] = v

    # AC Load Current - sint32, 0.001 A (already defined as REG_AC_CURRENT)
    v = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, slave_id, REG_AC_CURRENT)
    result["current_a"] = round(v * 0.001, 3) if v is not None else None

    return result

def ac_diag_snapshot():
    """Read both XW Pro 6848 inverters and return timestamped diagnostic snapshot."""
    ts = datetime.now().isoformat()
    master = read_inverter_ac_diag(INVERTER_1_ID)
    slave  = read_inverter_ac_diag(INVERTER_2_ID)

    def delta(a, b):
        if a is not None and b is not None:
            return round(a - b, 4)
        return None

    return {
        "timestamp": ts,
        "master": master,
        "slave": slave,
        "delta": {
            "voltage_l1": delta(master.get("voltage_l1"), slave.get("voltage_l1")),
            "voltage_l2": delta(master.get("voltage_l2"), slave.get("voltage_l2")),
            "frequency":  delta(master.get("frequency"),  slave.get("frequency")),
            "power_w":    delta(master.get("power_w"),    slave.get("power_w")),
        }
    }

def ac_diag_save(reading):
    """Append a reading to the AC diagnostic log file (non-blocking, called in thread)."""
    with ac_diag_lock:
        entries = []
        if os.path.exists(AC_DIAG_LOG_FILE):
            try:
                with open(AC_DIAG_LOG_FILE, 'r') as f:
                    entries = json.load(f)
            except Exception:
                entries = []
        entries.append(reading)
        if len(entries) > AC_DIAG_MAX_ENTRIES:
            entries = entries[-AC_DIAG_MAX_ENTRIES:]
        try:
            with open(AC_DIAG_LOG_FILE, 'w') as f:
                json.dump(entries, f)
        except Exception as e:
            logger.warning(f"AC diag log write error: {e}")

# --- Polling Thread ---
def poll_modbus():
    global system_data, start_time
    while True:
        errors = 0
        new_data = {}
        mep_ok = True
        kubota_ok = True
        try:
            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_AC_POWER)
            new_data["acPower1"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_AC_CURRENT)
            new_data["acCurrent1"] = round(val / 1000.0, 3) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_2_ID, REG_AC_POWER)
            new_data["acPower2"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_2_ID, REG_AC_CURRENT)
            new_data["acCurrent2"] = round(val / 1000.0, 3) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, BATTERY_MONITOR_ID, REG_BATTERY_VOLTAGE)
            new_data["batteryVoltage"] = round(val / 1000.0, 2) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, BATTERY_MONITOR_ID, REG_BATTERY_SOC)
            new_data["batterySOC"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, MPPT_80_ID, REG_PV_VOLTAGE)
            new_data["mppt80PVVoltage"] = round(val / 1000.0, 2) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, MPPT_80_ID, REG_PV_CURRENT)
            new_data["mppt80PVCurrent"] = round(val / 1000.0, 3) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, MPPT_80_ID, REG_PV_POWER)
            new_data["mppt80PVPower"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, MPPT_80_ID, REG_CHARGER_STATUS)
            new_data["mppt80ChargeStatus"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, SOUTH_ARRAY_ID, REG_PV_VOLTAGE)
            new_data["southArrayPVVoltage"] = round(val / 1000.0, 2) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, SOUTH_ARRAY_ID, REG_PV_CURRENT)
            new_data["southArrayPVCurrent"] = round(val / 1000.0, 3) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, SOUTH_ARRAY_ID, REG_PV_POWER)
            new_data["southArrayPVPower"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, SOUTH_ARRAY_ID, REG_CHARGER_STATUS)
            new_data["southArrayChargeStatus"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, WEST_ARRAY_ID, REG_PV_VOLTAGE)
            new_data["westArrayPVVoltage"] = round(val / 1000.0, 2) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, WEST_ARRAY_ID, REG_PV_CURRENT)
            new_data["westArrayPVCurrent"] = round(val / 1000.0, 3) if val is not None else 0.0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, WEST_ARRAY_ID, REG_PV_POWER)
            new_data["westArrayPVPower"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, WEST_ARRAY_ID, REG_CHARGER_STATUS)
            new_data["westArrayChargeStatus"] = val if val is not None else 0
            errors += 0 if val is not None else 1

            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, AGS_MEP803A_ID, REG_GENERATOR_MODE)
            new_data["mep803aMode"] = val if val is not None else 0
            new_data["mepAgsOnline"] = val is not None
            if val is None:
                errors += 1
                mep_ok = False
            else:
                act = modbus.read_holding_register_16(
                    MODBUS_HOST, MODBUS_PORT, AGS_MEP803A_ID, REG_GENERATOR_ACTION)
                new_data["mep803aAction"] = act if act is not None else 255

            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, AGS_KUBOTA_ID, REG_GENERATOR_MODE)
            new_data["kubotaMode"] = val if val is not None else 0
            new_data["kubotaAgsOnline"] = val is not None
            if val is None:
                errors += 1
                kubota_ok = False
            else:
                act = modbus.read_holding_register_16(
                    MODBUS_HOST, MODBUS_PORT, AGS_KUBOTA_ID, REG_GENERATOR_ACTION)
                new_data["kubotaAction"] = act if act is not None else 255

            mep_rate = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_MAX_CHARGE_RATE)
            new_data["mepChargeRateLive"] = mep_rate if mep_rate is not None else 0

            kubota_rate = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, INVERTER_3_ID, REG_MAX_CHARGE_RATE)
            new_data["kubotaChargeRateLive"] = kubota_rate if kubota_rate is not None else 0

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_CHARGE_DC_POWER)
            new_data["chargePower1"] = val if val is not None else 0

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, INVERTER_2_ID, REG_CHARGE_DC_POWER)
            new_data["chargePower2"] = val if val is not None else 0

            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, INVERTER_3_ID, REG_CHARGE_DC_POWER)
            new_data["chargePower3"] = val if val is not None else 0

            # --- Conext Battery Monitor: true net battery power from the shunt.
            # Deliberately excluded from `errors`; if the monitor is absent the
            # dashboard simply falls back to the inverter-derived figures.
            try:
                bm_i = modbus.read_holding_register_32s(
                    MODBUS_HOST, MODBUS_PORT, BATTERY_MONITOR_ID, REG_BM_CURRENT)
                bm_v = modbus.read_holding_register_32(
                    MODBUS_HOST, MODBUS_PORT, BATTERY_MONITOR_ID, REG_BM_VOLTAGE)
                if bm_i is not None and bm_v is not None:
                    amps = bm_i / 1000.0
                    volts = bm_v / 1000.0
                    new_data["battCurrent"] = round(amps, 1)
                    new_data["battPower"] = int(round(amps * volts))
                    new_data["battMonitorOnline"] = True
                    for key, reg in (("battSocBM", REG_BM_SOC),
                                     ("battAhRemaining", REG_BM_AH_REMAIN),
                                     ("battMinToDischarge", REG_BM_TIME_DISCH)):
                        val = modbus.read_holding_register_32(
                            MODBUS_HOST, MODBUS_PORT, BATTERY_MONITOR_ID, reg)
                        if val is not None:
                            new_data[key] = val
                else:
                    new_data["battMonitorOnline"] = False
            except Exception as bm_err:
                logger.debug(f"Battery monitor read failed: {bm_err}")
                new_data["battMonitorOnline"] = False

            elapsed = int(time.time() - start_time)
            # lastUpdate is really uptime; kept as-is for the ESP32 display and
            # Alexa webhook. clockTime is the actual wall clock of this poll.
            new_data["lastUpdate"] = f"{(elapsed//3600)%24:02d}:{(elapsed//60)%60:02d}:{elapsed%60:02d}"
            new_data["clockTime"] = datetime.now().strftime("%H:%M:%S")
            new_data["pollErrors"] = errors

            with data_lock:
                system_data.update(new_data)

            check_ags_status(mep_ok, kubota_ok)

            non_ags_errors = errors - (0 if mep_ok else 1) - (0 if kubota_ok else 1)
            with alert_lock:
                if non_ags_errors > 0 and not alert_state["poll_error_alerted"]:
                    alert_state["poll_error_alerted"] = True
                    send_telegram(f"⚠️ <b>Modbus Poll Errors</b>\n{non_ags_errors} device(s) not responding.\nCheck system connectivity.")
                elif non_ags_errors == 0:
                    alert_state["poll_error_alerted"] = False

            check_auto_generator()

            if errors > 0:
                logger.warning(f"Poll completed with {errors} errors")

        except Exception as e:
            logger.error(f"Poll exception: {e}")

        time.sleep(POLL_INTERVAL)

# --- Flask App ---
app = Flask(__name__)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<meta name='theme-color' content='#0b0f14'>
<title>Solar Dashboard - Pi 5</title>
<style>
:root{
  --bg:#0b0f14; --bg2:#111823; --panel:rgba(255,255,255,0.035);
  --panel-hi:rgba(255,255,255,0.06); --line:rgba(255,255,255,0.08);
  --txt:#e8eef5; --dim:#8fa0b3; --dim2:#7d8da1;
  --solar:#ffb020; --batt:#3ddc97; --ac:#4cc9f0; --gen:#c084fc;
  --bad:#ff5d5d; --warn:#ffb020; --good:#3ddc97;
  --r:16px; --ease:cubic-bezier(.22,.61,.36,1);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:var(--bg); color:var(--txt); min-height:100vh; padding:18px 14px 40px;
  -webkit-font-smoothing:antialiased;
}
body::before{
  content:''; position:fixed; inset:0; z-index:-1; pointer-events:none;
  background:
    radial-gradient(900px 500px at 12% -8%, rgba(255,176,32,.10), transparent 60%),
    radial-gradient(800px 500px at 92% 4%, rgba(76,201,240,.09), transparent 60%),
    linear-gradient(180deg,#0d131b,#080b10 70%);
}
.wrap{max-width:1120px;margin:0 auto}
.num{font-variant-numeric:tabular-nums;font-feature-settings:'tnum'}

/* ---------- header ---------- */
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:11px;margin-right:auto}
.sunmark{width:34px;height:34px;flex:0 0 34px}
.sunmark .core{fill:var(--solar)}
.sunmark .rays{stroke:var(--solar);stroke-width:2.4;stroke-linecap:round;transform-origin:50% 50%;opacity:.9}
.sunmark.spin .rays{animation:spin 14s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.brand h1{margin:0;font-size:1.18rem;font-weight:650;letter-spacing:-.015em}
.brand span{display:block;font-size:.72rem;color:var(--dim2);font-weight:450;letter-spacing:.04em}
.pill{
  display:inline-flex;align-items:center;gap:8px;padding:7px 13px;border-radius:999px;
  background:var(--panel);border:1px solid var(--line);font-size:.78rem;color:var(--dim2);
}
.pill a{color:var(--batt);text-decoration:none}
.pill a:hover{text-decoration:underline}
.dot{width:8px;height:8px;border-radius:50%;background:var(--good);flex:0 0 8px}
.dot.beat{animation:beat .9s var(--ease)}
@keyframes beat{0%{box-shadow:0 0 0 0 rgba(61,220,151,.55)}100%{box-shadow:0 0 0 11px rgba(61,220,151,0)}}
.dot.err{background:var(--bad);animation:blink 1.1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.dot.stale{background:var(--warn)}
.dot.off{background:var(--dim2)}
.askbox{display:inline-flex;align-items:center;gap:4px}
.askbox input{width:11rem;background:rgba(255,255,255,.06);color:var(--txt);
  border:1px solid var(--line,#2a2f3a);border-radius:7px;padding:3px 7px;
  font:inherit;font-size:.72rem}
.askbox input::placeholder{color:var(--dim2)}
.askbox input:focus{outline:none;border-color:var(--batt)}
/* An answer is a block in the log, not a line: it keeps its own paragraphs. */
.ask-q{color:var(--batt);padding-top:6px!important;white-space:pre-wrap;
  word-break:break-word}
.ask-a{color:var(--txt);white-space:pre-wrap;word-break:break-word;
  border-left:2px solid var(--line,#2a2f3a);padding-left:8px!important;
  margin:2px 0 6px}
.ask-wait{color:var(--dim2);font-style:italic}
.agentb{cursor:pointer;user-select:none;position:relative}
.agentb:hover{color:var(--batt)}
/* The plan popover hangs off the badge; the badge is its positioning parent. */
.agentpop{position:absolute;top:calc(100% + 8px);right:0;z-index:60;
  width:min(30rem,calc(100vw - 2rem));max-height:min(32rem,70vh);overflow:auto;
  background:var(--panel,#12151b);color:var(--txt);border:1px solid var(--line,#2a2f3a);
  border-radius:10px;padding:.7rem .8rem;text-align:left;cursor:default;
  box-shadow:0 10px 30px rgba(0,0,0,.45);font-size:.72rem;line-height:1.45}
.agentpop h4{margin:0 0 .4rem;font-size:.68rem;letter-spacing:.08em;
  text-transform:uppercase;color:var(--dim2)}
.agentpop .pl{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,
  SFMono-Regular,Menlo,monospace}
.agentpop .pl.fire{color:var(--warn)}
.agentpop .pl.rec{color:var(--batt)}
.agentpop .acts{margin-top:.7rem;border-top:1px solid var(--line,#2a2f3a);
  padding-top:.5rem}
.agentpop .act{display:flex;gap:.5rem;padding:.15rem 0}
.agentpop .act .at{color:var(--dim2);flex:0 0 5.2rem}
.agentpop .act .rs{flex:0 0 4.2rem}
.agentpop .act .rs.no{color:var(--bad)}
.agentpop .act .rs.yes{color:var(--good)}
.agentpop .act .why{flex:1 1 auto;color:var(--dim2);word-break:break-word}

.banner{
  display:none;align-items:center;gap:10px;padding:13px 16px;border-radius:13px;margin-bottom:16px;
  background:linear-gradient(90deg,rgba(255,93,93,.16),rgba(255,93,93,.05));
  border:1px solid rgba(255,93,93,.34);color:#ffc9c9;font-size:.88rem;font-weight:500;
  animation:slideDown .35s var(--ease);
}
@keyframes slideDown{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}

/* ---------- cards ---------- */
.card{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;
  position:relative;overflow:hidden;
  transition:border-color .3s var(--ease),transform .3s var(--ease),background .3s var(--ease);
}
.card:hover{border-color:var(--panel-hi);background:rgba(255,255,255,.05)}
.card::after{
  content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--accent,transparent),transparent);opacity:.55;
}
.lbl{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--dim2);
  display:flex;align-items:center;gap:7px;margin-bottom:12px;font-weight:600}
.val{font-size:2.05rem;font-weight:660;line-height:1;letter-spacing:-.03em}
.val .u{font-size:.9rem;font-weight:500;color:var(--dim2);margin-left:4px;letter-spacing:0}
.sub{font-size:.8rem;color:var(--dim2);margin-top:8px;min-height:1.1em}

.hero{display:grid;grid-template-columns:1.45fr 1fr 1fr;gap:14px;margin-bottom:14px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;margin-bottom:14px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
@media(max-width:880px){.hero,.grid3,.grid4{grid-template-columns:1fr 1fr}.hero>:first-child{grid-column:1/-1}}
@media(max-width:560px){.hero,.grid2,.grid3,.grid4{grid-template-columns:1fr}}

h2.sec{
  font-size:.76rem;text-transform:uppercase;letter-spacing:.13em;color:var(--dim2);
  font-weight:650;margin:26px 0 12px;display:flex;align-items:center;gap:12px;
}
h2.sec::after{content:'';flex:1;height:1px;background:var(--line)}

/* ---------- bars ---------- */
.track{height:6px;border-radius:99px;background:rgba(255,255,255,.07);overflow:hidden;margin-top:13px;position:relative}
.fill{height:100%;border-radius:99px;width:0;transition:width .9s var(--ease);position:relative;
  background:linear-gradient(90deg,var(--c1,#3ddc97),var(--c2,#4cc9f0))}
.fill.live::after{
  content:'';position:absolute;inset:0;border-radius:99px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent);
  animation:sweep 2.1s linear infinite;
}
@keyframes sweep{from{transform:translateX(-100%)}to{transform:translateX(100%)}}

/* ---------- big hero solar ---------- */
.hero-main{display:flex;flex-direction:column;justify-content:space-between}
.hero-main .val{font-size:3.1rem}
.spark{display:flex;align-items:flex-end;gap:3px;height:34px;margin-top:14px}
.spark i{flex:1;background:linear-gradient(180deg,var(--solar),rgba(255,176,32,.25));
  border-radius:2px 2px 0 0;height:4%;transition:height .6s var(--ease);min-height:2px;opacity:.85}

/* ---------- soc ring ---------- */
.ring-wrap{display:flex;align-items:center;gap:16px}
.ring{width:96px;height:96px;flex:0 0 96px;transform:rotate(-90deg)}
.ring .bg{fill:none;stroke:rgba(255,255,255,.08);stroke-width:9}
.ring .fg{fill:none;stroke:url(#socGrad);stroke-width:9;stroke-linecap:round;
  stroke-dasharray:295.3;stroke-dashoffset:295.3;transition:stroke-dashoffset 1s var(--ease)}
.ring-txt{font-size:1.85rem;font-weight:660;letter-spacing:-.03em}

/* ---------- status chips ---------- */
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:99px;
  font-size:.7rem;font-weight:650;letter-spacing:.04em;text-transform:uppercase}
.chip.ok{background:rgba(61,220,151,.13);color:var(--good);border:1px solid rgba(61,220,151,.28)}
.chip.off{background:rgba(255,255,255,.05);color:var(--dim2);border:1px solid var(--line)}
.chip.bad{background:rgba(255,93,93,.14);color:var(--bad);border:1px solid rgba(255,93,93,.3)}
.chip.run{background:rgba(192,132,252,.14);color:var(--gen);border:1px solid rgba(192,132,252,.32)}
.chip.run .dot{background:var(--gen);animation:pulse 1.6s infinite}
.chip.warn{background:rgba(255,176,32,.14);color:var(--warn);border:1px solid rgba(255,176,32,.32)}
.chip.warn .dot{background:var(--warn);animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(192,132,252,.6)}70%{box-shadow:0 0 0 8px rgba(192,132,252,0)}100%{box-shadow:0 0 0 0 rgba(192,132,252,0)}}

/* ---------- gen card ---------- */
.gen-card .row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:14px}
select,input,button{font-family:inherit;font-size:.85rem}
select,input{background:rgba(0,0,0,.3);border:1px solid var(--line);color:var(--txt);
  padding:9px 11px;border-radius:10px;outline:none;transition:border-color .2s,box-shadow .2s}
select:focus,input:focus{border-color:var(--ac);box-shadow:0 0 0 3px rgba(76,201,240,.13)}
select{flex:1}
.btn{border:none;border-radius:10px;padding:9px 16px;font-weight:600;cursor:pointer;
  transition:transform .15s var(--ease),filter .2s,box-shadow .2s;color:#08111a}
.btn:hover{filter:brightness(1.1)}
.btn:active{transform:scale(.96)}
.btn.p{background:var(--ac)}
.btn.s{background:rgba(255,255,255,.09);color:var(--txt);border:1px solid var(--line)}
.btn.g{background:var(--good)}
.btn.d{background:var(--bad);color:#fff}
.btn.v{background:var(--gen);color:#160c22}
.btn.sm{padding:7px 13px;font-size:.78rem}

/* ---------- settings ---------- */
.acc{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);margin-bottom:12px;overflow:hidden}
.acc>summary{
  padding:16px 18px;cursor:pointer;font-weight:600;font-size:.92rem;list-style:none;
  display:flex;align-items:center;gap:10px;transition:background .2s;
}
.acc>summary::-webkit-details-marker{display:none}
.acc>summary:hover{background:rgba(255,255,255,.03)}
.acc>summary .caret{margin-left:auto;color:var(--dim2);transition:transform .3s var(--ease)}
.acc[open]>summary .caret{transform:rotate(90deg)}
.acc[open]>summary{border-bottom:1px solid var(--line)}
.acc-body{padding:18px;animation:fadeIn .3s var(--ease)}
@keyframes fadeIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.fieldset{background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:13px;padding:16px}
.fieldset h4{margin:0 0 13px;font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;color:var(--solar);font-weight:650}
.frow{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.frow label{font-size:.83rem;color:var(--dim2)}
.frow input{width:92px;text-align:right}
.frow input[type=text]{width:100%;max-width:190px;text-align:left}
.togrow{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:13px 15px;background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:13px;margin-bottom:16px}
.togrow .t{font-size:.88rem}

/* ---------- log ---------- */
.log{background:rgba(0,0,0,.42);border:1px solid var(--line);border-radius:13px;padding:13px;
  height:230px;overflow-y:auto;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:.76rem;line-height:1.65}
.log::-webkit-scrollbar{width:7px}
.log::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14);border-radius:9px}
.log div{padding:1px 0;animation:logIn .3s var(--ease)}
@keyframes logIn{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}
.e-err{color:#ff8f8f}.e-warn{color:#ffcb6b}.e-ok{color:#7ce8b4}.e-info{color:#94a6bb}

/* ---------- toast ---------- */
#toasts{position:fixed;bottom:22px;right:22px;z-index:99;display:flex;flex-direction:column;gap:9px;max-width:330px}
.toast{padding:13px 17px;border-radius:12px;font-size:.85rem;font-weight:500;color:#fff;
  background:#1b2532;border:1px solid var(--line);box-shadow:0 14px 34px rgba(0,0,0,.5);
  animation:toastIn .34s var(--ease)}
.toast.ok{border-color:rgba(61,220,151,.4);background:linear-gradient(90deg,rgba(61,220,151,.15),#1b2532)}
.toast.bad{border-color:rgba(255,93,93,.4);background:linear-gradient(90deg,rgba(255,93,93,.15),#1b2532)}
.toast.out{animation:toastOut .3s var(--ease) forwards}
@keyframes toastIn{from{opacity:0;transform:translateX(40px) scale(.96)}to{opacity:1;transform:none}}
@keyframes toastOut{to{opacity:0;transform:translateX(40px) scale(.96)}}
@media(max-width:560px){ #toasts{left:14px;right:14px;bottom:14px;max-width:none}}

.foot{margin-top:26px;text-align:center;font-size:.75rem;color:#5f6f83}

/* entrance stagger */
.rise{opacity:0;animation:rise .55s var(--ease) forwards}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}

/* ---------- AC diagnostic ---------- */
.mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px}
.metric{background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:11px;padding:12px 13px}
.metric .k{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim2);font-weight:600;margin-bottom:6px}
.metric .v{font-size:1.22rem;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.metric .s{font-size:.7rem;color:#5f6f83;margin-top:3px;font-variant-numeric:tabular-nums}
.delta-ok{color:var(--good)}
.delta-warn{color:var(--warn)}
.dbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;align-items:center}
.dstat{font-size:.78rem;color:var(--dim2);margin-top:11px;min-height:1.2em}
.dstat.running{color:var(--warn)}
.dstat.done{color:var(--good)}
.dstat.error{color:var(--bad)}
.dwrap{max-height:250px;overflow-y:auto;border:1px solid var(--line);border-radius:11px;margin-top:13px}
.dwrap::-webkit-scrollbar{width:7px}
.dwrap::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14);border-radius:9px}
.dtable{width:100%;border-collapse:collapse;font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:.72rem}
.dtable th{position:sticky;top:0;background:#141c26;color:var(--dim2);font-weight:600;
  text-transform:uppercase;letter-spacing:.05em;font-size:.64rem;padding:9px 8px;text-align:right;
  border-bottom:1px solid var(--line);white-space:nowrap}
.dtable th:first-child,.dtable td:first-child{text-align:left}
.dtable td{padding:6px 8px;text-align:right;border-bottom:1px solid rgba(255,255,255,.04);color:var(--txt);white-space:nowrap}
.dtable tbody tr:hover{background:rgba(255,255,255,.03)}
.dtable td[colspan]{text-align:center;color:#5f6f83;padding:18px}

input[type=range]{-webkit-appearance:none;appearance:none;background:transparent;height:18px;padding:0}
input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:9px;background:rgba(255,255,255,.16)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;
  background:var(--solar);margin-top:-5px;cursor:pointer;border:none}
input[type=range]::-moz-range-track{height:4px;border-radius:9px;background:rgba(255,255,255,.16)}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:var(--solar);
  border:none;cursor:pointer}
.suncap{position:absolute;top:11px;left:13px;right:13px;display:flex;gap:11px;align-items:center;
  background:rgba(10,15,21,.72);border:1px solid var(--line);border-radius:11px;padding:8px 12px}
.suncap .lab{font-size:.72rem;color:var(--dim2);white-space:nowrap;font-variant-numeric:tabular-nums}

/* ---------- 3D hero + floating HUD ---------- */
.stage{position:relative;height:72vh;min-height:430px;margin:-18px -14px 22px;
  overflow:hidden;background:linear-gradient(180deg,#0e1620,#080b10)}
.stage #stage3d{position:absolute;top:0;left:0;right:0;bottom:0}
.hud{position:absolute;z-index:4;background:rgba(9,13,19,.62);
  border:1px solid rgba(255,255,255,.09);border-radius:14px;
  backdrop-filter:blur(11px);-webkit-backdrop-filter:blur(11px);
  padding:13px 16px;pointer-events:auto}
.hud-tl{top:16px;left:18px;display:flex;align-items:center;gap:11px}
.hudstack{display:contents}
.hud-tr{top:16px;right:18px;display:flex;align-items:center;gap:14px;font-size:.76rem;
  color:var(--dim2)}
.hud-tr a{color:var(--batt);text-decoration:none}
.hud-bl{bottom:18px;left:18px;display:flex;gap:26px;align-items:flex-end}
.hud-br{bottom:18px;right:18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  max-width:min(460px,52vw)}
.hud-gen{top:50%;right:18px;transform:translateY(-50%);display:flex;
  flex-direction:column;gap:9px;align-items:flex-end}
.hud .k{font-size:.63rem;text-transform:uppercase;letter-spacing:.1em;
  color:var(--dim2);font-weight:650;margin-bottom:5px}
.hud .v{font-size:1.85rem;font-weight:660;letter-spacing:-.03em;line-height:1;
  font-variant-numeric:tabular-nums}
.hud .v .u{font-size:.72rem;font-weight:500;color:var(--dim2);margin-left:3px}
.hud .s{font-size:.7rem;color:var(--dim2);margin-top:5px;font-variant-numeric:tabular-nums}
.hudring{width:62px;height:62px;transform:rotate(-90deg);flex:0 0 62px}
.hudring .bg{fill:none;stroke:rgba(255,255,255,.1);stroke-width:7}
.hudring .fg{fill:none;stroke:url(#socGrad);stroke-width:7;stroke-linecap:round;
  stroke-dasharray:295.3;stroke-dashoffset:295.3;transition:stroke-dashoffset 1s var(--ease)}
.scrollhint{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);
  font-size:.66rem;letter-spacing:.14em;text-transform:uppercase;color:#5f6f83;
  z-index:3;pointer-events:none;animation:bob 2.4s ease-in-out infinite}
@keyframes bob{0%,100%{transform:translateX(-50%) translateY(0)}
  50%{transform:translateX(-50%) translateY(4px)}}
@media(max-width:820px){
  .stage{height:58vh}
  .hud-gen{display:none}
  .hud-tr{display:none}
  .hud-tl{display:none}
  .scrollhint{display:none}
  /* both cards stack in flow at the top - they cannot overlap */
  .hudstack{display:flex;flex-direction:column;gap:8px;
    position:absolute;top:10px;left:10px;right:10px;z-index:4}
  .hud-bl,.hud-br{position:static;top:auto;left:auto;right:auto;bottom:auto}
  .hud-bl{gap:10px;padding:8px 12px;justify-content:space-between;align-items:center}
  .hud .v{font-size:1.12rem}
  .hud .k{font-size:.56rem;margin-bottom:2px}
  .hud .s{font-size:.62rem;margin-top:2px}
  .hudring{width:38px;height:38px;flex:0 0 38px}
  .hud-br{align-self:flex-end;padding:6px 9px;gap:6px;max-width:none}
  .hud-br .lab:first-child{display:none}
  #sunTime{width:78px}
  #sunLabel{display:none}
  .hud-br .btn{padding:4px 8px;font-size:.66rem}
}  .hud-br .btn{padding:4px 8px;font-size:.66rem}
}

@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}
}
</style>
</head>
<body>
<svg width='0' height='0' style='position:absolute'>
  <defs>
    <linearGradient id='socGrad' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#3ddc97'/><stop offset='100%' stop-color='#4cc9f0'/>
    </linearGradient>
  </defs>
</svg>

<div class='wrap'>

<section class='stage' id='stage3dCard'>
  <div id='stage3d'></div>
  <div id='stage3dMsg' style='position:absolute;top:50%;left:22px;right:22px;text-align:center;
    transform:translateY(-50%);color:var(--dim2);font-size:.82rem;line-height:1.6;
    white-space:pre-wrap;word-break:break-word;user-select:text;font-family:ui-monospace,
    Menlo,monospace'>Loading renderer...</div>

  <div class='hud hud-tl'>
    <svg class='sunmark spin' id='sunmark' viewBox='0 0 40 40' style='width:26px;height:26px'>
      <circle class='core' cx='20' cy='20' r='7.5'/>
      <g class='rays'>
        <line x1='20' y1='2.5' x2='20' y2='8'/><line x1='20' y1='32' x2='20' y2='37.5'/>
        <line x1='2.5' y1='20' x2='8' y2='20'/><line x1='32' y1='20' x2='37.5' y2='20'/>
        <line x1='7.6' y1='7.6' x2='11.5' y2='11.5'/><line x1='28.5' y1='28.5' x2='32.4' y2='32.4'/>
        <line x1='32.4' y1='7.6' x2='28.5' y2='11.5'/><line x1='11.5' y1='28.5' x2='7.6' y2='32.4'/>
      </g>
    </svg>
    <div>
      <div style='font-weight:650;font-size:.94rem;letter-spacing:-.015em'>Mercalde Solar</div>
      <div style='font-size:.68rem;color:var(--dim2);letter-spacing:.05em'>Rosarito &middot; Off-Grid</div>
    </div>
  </div>

  <div class='hud hud-tr'>
    <span><span class='dot' id='liveDot'></span>&nbsp;<span class='num' id='lastUpdate_value'>--:--:--</span></span>
    <span>Errors <strong class='num' id='pollErrors_value' style='color:var(--txt)'>0</strong></span>
    <span class='agentb' id='agentBadge' onclick='toggleAgentPlan(event)' title='Show the latest agent plan'><span class='dot off' id='agentDot'></span>&nbsp;<span id='agentText'>Agent &hellip;</span><span class='agentpop' id='agentPop' hidden onclick='event.stopPropagation()'></span></span>
    <span class='askbox'>
      <input id='askInput' type='text' placeholder='Ask the agent&hellip;' autocomplete='off'
             onkeydown='if(event.key===\"Enter\"){event.preventDefault();askAgent();}'>
      <button class='btn s sm' onclick='askAgent()'>Ask</button>
      <button class='btn s sm' onclick='askAgent(\"plan\")'>Plan</button>
      <button class='btn s sm' onclick='askAgent(\"what is the system doing right now\")'>Status</button>
    </span>
    <a href='/registers'>Registers &rarr;</a>
  </div>

  <div class='hudstack'>
<div class='hud hud-bl'>
    <div>
      <div class='k'>Solar</div>
      <div class='v num'><span id='hudPv'>0</span><span class='u'>W</span></div>
      <div class='s'><span id='hudPvPct'>0</span>% of <span id='capKw_value'>--</span> kW</div>
    </div>
    <div style='display:flex;align-items:center;gap:11px'>
      <svg class='hudring' viewBox='0 0 108 108'>
        <circle class='bg' cx='54' cy='54' r='47'/>
        <circle class='fg' id='hudRing' cx='54' cy='54' r='47'/>
      </svg>
      <div>
        <div class='k'>Battery</div>
        <div class='v num'><span id='hudSoc'>0</span><span class='u'>%</span></div>
        <div class='s'><span class='num' id='hudVolts'>0</span> V &middot; <span id='hudRunway'>--</span></div>
      </div>
    </div>
    <div>
      <div class='k'>Load</div>
      <div class='v num'><span id='hudLoad'>0</span><span class='u'>W</span></div>
      <div class='s' id='hudBatt'>--</div>
    </div>
  </div>
<div class='hud hud-br'>
    <span class='lab' style='font-size:.7rem;color:var(--dim2)'>Sun</span>
    <input type='range' id='sunTime' min='0' max='23.75' step='0.25' value='12'
      oninput='onSunTime(this.value)' style='width:150px'>
    <span class='lab' id='sunLabel' style='font-size:.7rem;color:var(--dim2);
      white-space:nowrap;font-variant-numeric:tabular-nums'>--</span>
    <button class='btn s sm' onclick='sunNow()' style='padding:5px 10px'>Now</button>
    <button class='btn s sm' id='shadowBtn' onclick='toggleShadows()' style='padding:5px 10px'>Shadows</button>
    <button class='btn s sm' onclick='resetView()' style='padding:5px 10px'>Reset</button>
    <button class='btn s sm' id='toggle3d' onclick='toggle3D()' style='padding:5px 10px'>3D off</button>
  </div>
</div>

  <div class='hud hud-gen'>
    <span id='hud_mep' class='chip off'>MEP --</span>
    <span id='hud_kub' class='chip off'>KUB --</span>
  </div>

  <div class='scrollhint'>Detail below &darr;</div>
</section>

<div class='banner' id='errorBanner'></div>

<!-- HERO -->
<h2 class='sec'>Overview</h2>
<div class='hero'>
  <div class='card hero-main rise' style='--accent:var(--solar);animation-delay:.02s'>
    <div>
      <div class='lbl'>&#9728;&#65039; Total Solar Production</div>
      <div class='val num'><span id='totalPV_value'>0</span><span class='u'>W</span></div>
      <div class='sub'>Peak <span id='capKw_value'>--</span> kW &middot; <span id='pvPct_value'>0</span>% of array</div>
    </div>
    <div>
      <div class='track'><div class='fill' id='totalPV_bar' style='--c1:#ff8a00;--c2:#ffd54a'></div></div>
      <div class='spark' id='spark'></div>
    </div>
  </div>

  <div class='card rise' style='--accent:var(--batt);animation-delay:.08s'>
    <div class='lbl'>&#128267; Battery</div>
    <div class='ring-wrap'>
      <svg class='ring' viewBox='0 0 108 108'>
        <circle class='bg' cx='54' cy='54' r='47'/>
        <circle class='fg' id='socRing' cx='54' cy='54' r='47'/>
      </svg>
      <div>
        <div class='ring-txt num'><span id='batterySOC_value'>0</span>%</div>
        <div class='sub' style='margin-top:4px'><span class='num' id='batteryVoltage_value'>0</span> V &middot; <span id='battFlow_value'>--</span></div>
      </div>
    </div>
  </div>

  <div class='card rise' style='--accent:var(--ac);animation-delay:.14s'>
    <div class='lbl'>&#9889; AC Load</div>
    <div class='val num'><span id='totalAC_value'>0</span><span class='u'>W</span></div>
    <div class='sub'>AC charge <span class='num' id='totalCharge_value'>0</span> W</div>
    <div class='track'><div class='fill' id='totalAC_bar' style='--c1:#4cc9f0;--c2:#7ee0ff'></div></div>
  </div>
</div>

<h2 class='sec'>Inverters</h2>
<div class='grid2'>
  <div class='card rise' style='--accent:var(--ac);animation-delay:.18s'>
    <div class='lbl'>&#128268; XW Pro Master &middot; ID 10</div>
    <div class='val num'><span id='acPower1_value'>0</span><span class='u'>W</span></div>
    <div class='sub'><span class='num' id='acCurrent1_value'>0</span> A &middot; charge <span class='num' id='chargePower1_value'>0</span> W</div>
  </div>
  <div class='card rise' style='--accent:var(--ac);animation-delay:.22s'>
    <div class='lbl'>&#128268; XW Pro Slave &middot; ID 12</div>
    <div class='val num'><span id='acPower2_value'>0</span><span class='u'>W</span></div>
    <div class='sub'><span class='num' id='acCurrent2_value'>0</span> A &middot; charge <span class='num' id='chargePower2_value'>0</span> W</div>
  </div>
</div>

<h2 class='sec'>Solar Arrays</h2>
<div class='grid3'>
  <div class='card rise' style='--accent:var(--solar);animation-delay:.26s'>
    <div class='lbl'>&#9728;&#65039; MPPT 80 &middot; Ground + terrace</div>
    <div class='val num'><span id='mppt80PVPower_value'>0</span><span class='u'>W</span></div>
    <div class='sub'><span id='mppt80ChargeStatus_value'>--</span> &middot; <span class='num' id='mppt80PVVoltage_value'>0</span> V</div>
    <div class='track'><div class='fill' id='mppt80_bar' style='--c1:#ff8a00;--c2:#ffd54a'></div></div>
  </div>
  <div class='card rise' style='--accent:var(--solar);animation-delay:.3s'>
    <div class='lbl'>&#9728;&#65039; MPPT 150 &middot; Gen building</div>
    <div class='val num'><span id='southArrayPVPower_value'>0</span><span class='u'>W</span></div>
    <div class='sub'><span id='southArrayChargeStatus_value'>--</span> &middot; <span class='num' id='southArrayPVVoltage_value'>0</span> V</div>
    <div class='track'><div class='fill' id='south_bar' style='--c1:#ff8a00;--c2:#ffd54a'></div></div>
  </div>
  <div class='card rise' style='--accent:var(--solar);animation-delay:.34s'>
    <div class='lbl'>&#9728;&#65039; MPPT 150 &middot; House (west)</div>
    <div class='val num'><span id='westArrayPVPower_value'>0</span><span class='u'>W</span></div>
    <div class='sub'><span id='westArrayChargeStatus_value'>--</span> &middot; <span class='num' id='westArrayPVVoltage_value'>0</span> V</div>
    <div class='track'><div class='fill' id='west_bar' style='--c1:#ff8a00;--c2:#ffd54a'></div></div>
  </div>
</div>

<h2 class='sec'>Generators</h2>
<div class='grid2'>
  <div class='card gen-card rise' style='--accent:var(--gen);animation-delay:.38s'>
    <div class='lbl'>&#128295; MEP-803A <span id='mep_chip' class='chip off'>--</span></div>
    <div class='val' style='font-size:1.5rem'><span id='mep803aMode_value'>--</span></div>
    <div class='sub'><span id='mep_action'>--</span> &middot; rate <strong class='num' id='mepChargeRateLive_value' style='color:var(--solar)'>0</strong>% &middot; <span id='mep_ags_status'>AGS --</span></div>
    <div class='track'><div class='fill' id='mepRate_bar' style='--c1:#c084fc;--c2:#e9d5ff'></div></div>
    <div class='row'>
      <select id='mep803a_select'><option value='0'>OFF</option><option value='1'>ON</option><option value='2'>AUTO</option></select>
      <button class='btn v' onclick='setGeneratorMode(51,document.getElementById("mep803a_select").value)'>Set</button>
    </div>
  </div>
  <div class='card gen-card rise' style='--accent:var(--gen);animation-delay:.42s'>
    <div class='lbl'>&#128295; Kubota <span id='kub_chip' class='chip off'>--</span></div>
    <div class='val' style='font-size:1.5rem'><span id='kubotaMode_value'>--</span></div>
    <div class='sub'><span id='kub_action'>--</span> &middot; rate <strong class='num' id='kubotaChargeRateLive_value' style='color:var(--solar)'>0</strong>% &middot; <span id='kubota_ags_status'>AGS --</span></div>
    <div class='track'><div class='fill' id='kubRate_bar' style='--c1:#c084fc;--c2:#e9d5ff'></div></div>
    <div class='row'>
      <select id='kubota_select'><option value='0'>OFF</option><option value='1'>ON</option><option value='2'>AUTO</option></select>
      <button class='btn v' onclick='setGeneratorMode(50,document.getElementById("kubota_select").value)'>Set</button>
    </div>
  </div>
</div>

<h2 class='sec'>AC Diagnostic &mdash; Inverter Sync</h2>
<div class='card' style='--accent:var(--gen)'>
  <div class='mgrid'>
    <div class='metric'><div class='k'>Master ID10 &middot; L1</div><div class='v' id='diag_m_v1'>--</div><div class='s'>L2 <span id='diag_m_v2'>--</span></div></div>
    <div class='metric'><div class='k'>Slave ID12 &middot; L1</div><div class='v' id='diag_s_v1'>--</div><div class='s'>L2 <span id='diag_s_v2'>--</span></div></div>
    <div class='metric'><div class='k'>&Delta; Voltage (M&minus;S)</div><div class='v' id='diag_dv'>--</div><div class='s'>target &plusmn;0.0&ndash;0.6 V</div></div>
    <div class='metric'><div class='k'>Master frequency</div><div class='v' id='diag_m_hz'>--</div><div class='s'>Hz</div></div>
    <div class='metric'><div class='k'>Slave frequency</div><div class='v' id='diag_s_hz'>--</div><div class='s'>Hz</div></div>
    <div class='metric'><div class='k'>&Delta; Frequency (M&minus;S)</div><div class='v' id='diag_dhz'>--</div><div class='s'>target 0.00 Hz</div></div>
    <div class='metric'><div class='k'>Master power</div><div class='v' id='diag_m_pw'>--</div><div class='s'>W</div></div>
    <div class='metric'><div class='k'>Slave power</div><div class='v' id='diag_s_pw'>--</div><div class='s'>W</div></div>
    <div class='metric'><div class='k'>Last reading</div><div class='v' id='diag_ts' style='font-size:1.05rem'>--</div><div class='s' id='diag_log_count'>Log: 0 entries</div></div>
  </div>
  <div class='dbar'>
    <button class='btn g sm' onclick='diagSnapshot()'>Snapshot</button>
    <button class='btn v sm' onclick='diagStream()'>Capture 20s</button>
    <button class='btn s sm' onclick='diagLoadLog()'>Refresh log</button>
    <button class='btn s sm' onclick='diagClearLog()' style='margin-left:auto'>Clear log</button>
  </div>
  <div class='dstat' id='diag_status'>Ready &mdash; Snapshot for one reading, Capture 20s to record a stream.</div>
  <div class='dwrap'>
    <table class='dtable'>
      <thead><tr><th>Time</th><th>M&nbsp;V1</th><th>S&nbsp;V1</th><th>&Delta;V</th><th>M&nbsp;Hz</th><th>S&nbsp;Hz</th><th>&Delta;Hz</th><th>M&nbsp;W</th><th>S&nbsp;W</th></tr></thead>
      <tbody id='diag_log_body'><tr><td colspan='9'>No log data &mdash; click Snapshot or Capture</td></tr></tbody>
    </table>
  </div>
</div>

<h2 class='sec'>Control &amp; Settings</h2>

<details class='acc' open>
  <summary>&#9889; Automatic Generator Control <span class='caret'>&#10095;</span></summary>
  <div class='acc-body'>
    <div class='togrow'>
      <span class='t'>Auto control: <strong id='autoGenStatus' style='color:var(--bad)'>DISABLED</strong></span>
      <button id='autoGenToggle' class='btn d' onclick='toggleAutoGen()'>ENABLE</button>
    </div>
    <div class='grid2' style='margin-bottom:0'>
      <div class='fieldset'>
        <h4>MEP-803A Thresholds</h4>
        <div class='frow'><label>Start voltage (V)</label><input type='number' id='mepStartV' step='0.1'></div>
        <div class='frow'><label>Stop voltage (V)</label><input type='number' id='mepStopV' step='0.1'></div>
        <div class='frow'><label>Charge rate (%)</label><input type='number' id='mepChargeRate' min='10' max='100'></div>
        <div class='frow'><label>Max runtime (min)</label><input type='number' id='mepMaxRuntime' min='10' max='480'></div>
        <div class='frow'><label>Cooldown (min)</label><input type='number' id='mepCooldown' min='1' max='60'></div>
        <button class='btn p sm' onclick='saveMepSettings()'>Save MEP</button>
      </div>
      <div class='fieldset'>
        <h4>Kubota Thresholds</h4>
        <div class='frow'><label>Start voltage (V)</label><input type='number' id='kubStartV' step='0.1'></div>
        <div class='frow'><label>Stop voltage (V)</label><input type='number' id='kubStopV' step='0.1'></div>
        <div class='frow'><label>Charge rate (%)</label><input type='number' id='kubChargeRate' min='10' max='100'></div>
        <div class='frow'><label>Max runtime (min)</label><input type='number' id='kubMaxRuntime' min='10' max='480'></div>
        <div class='frow'><label>Cooldown (min)</label><input type='number' id='kubCooldown' min='1' max='60'></div>
        <button class='btn p sm' onclick='saveKubSettings()'>Save Kubota</button>
      </div>
    </div>
  </div>
</details>

<details class='acc'>
  <summary>&#128295; System Settings <span class='caret'>&#10095;</span></summary>
  <div class='acc-body'>
    <div class='grid2' style='margin-bottom:0'>
      <div class='fieldset'>
        <h4>Ramp-Down</h4>
        <div class='frow'><label>Step delay (sec)</label><input type='number' id='rampStepDelay' min='5' max='60'></div>
        <div class='frow'><label>Zero hold (sec)</label><input type='number' id='rampZeroHold' min='30' max='300'></div>
        <button class='btn p sm' onclick='saveRampSettings()'>Save Ramp</button>
      </div>
      <div class='fieldset'>
        <h4>&#128241; Telegram Alerts</h4>
        <div class='togrow' style='margin-bottom:12px;padding:10px 13px'>
          <span class='t' style='font-size:.83rem'>Alerts: <strong id='telegramStatus' style='color:var(--bad)'>OFF</strong></span>
          <button id='telegramToggle' class='btn d sm' onclick='toggleTelegram()'>ENABLE</button>
        </div>
        <div class='frow'><label>Bot token</label><input type='text' id='tgToken' placeholder='123456:ABC...'></div>
        <div class='frow'><label>Chat ID</label><input type='text' id='tgChatId' placeholder='123456789'></div>
        <button class='btn p sm' onclick='saveTelegramSettings()'>Save</button>
        <button class='btn s sm' onclick='testTelegram()'>Test</button>
      </div>
    </div>
  </div>
</details>

<details class='acc' open id='logPanel'>
  <summary>&#128203; Event &amp; Error Log <span class='caret'>&#10095;</span></summary>
  <div class='acc-body'>
    <div class='log' id='eventLog'><div class='e-info'>Loading...</div></div>
  </div>
</details>

<div class='foot'>Pi 5 &middot; Dashboard V2.8 &middot; uptime <span class='num' id='uptime_value'>--:--:--</span></div>
</div>

<div id='toasts'></div>

<script>
var FT=0.3048;
/* 25 x 25 ft footprint -> square plan, so the hip roof is a true pyramid */
var HOUSE={ w:25*FT, d:25*FT, wall:20*FT, oh:2*FT, pitch:12 };
HOUSE.dx=HOUSE.w/2+HOUSE.oh;
HOUSE.dz=HOUSE.d/2+HOUSE.oh;
HOUSE.rise=HOUSE.dz*Math.tan(HOUSE.pitch*Math.PI/180);
HOUSE.ridge=2*(HOUSE.dx-HOUSE.dz);          /* 0 -> pyramid */

var TERRACE={ w:5.5, d:4.0, cx:0, deck:10*FT };
TERRACE.cz=HOUSE.d/2+TERRACE.d/2;

var GEN={ w:7.3, d:5.2, cx:1.0, cz:14.5, wallS:2.9, pitch:12 };
GEN.wallN=GEN.wallS+GEN.d*Math.tan(GEN.pitch*Math.PI/180);
GEN.wall=(GEN.wallN+GEN.wallS)/2;

var SITE={
  lat:32.2910479, lon:-117.0015129, bearing:15, wattsPerPanel:225,
  /* MPPT 80    = north ground array + terrace roof
     MPPT 150-W = house west hip + house south hip  (3/2/1 pyramids)
     MPPT 150-S = generator building                                    */
  clusters:[
    {key:'ground',  channel:'mppt80', cols:5, rows:3, tilt:25, azimuth:180,
     pos:[-1,1.6,-11], mount:'ground'},
    {key:'houseW',  channel:'west',   pyramid:[3,2,1], tilt:0, azimuth:270,
     pos:[0,0,0], mount:'flush'},
    {key:'houseS',  channel:'west',   pyramid:[3,2,1], tilt:0, azimuth:180,
     pos:[0,0,0], mount:'flush'},
    {key:'terrace', channel:'mppt80', cols:3, rows:3, tilt:0, azimuth:180,
     pos:[0,0,0], mount:'flush'},
    {key:'genbld',  channel:'south',  cols:4, rows:4, tilt:0, azimuth:180,
     pos:[0,0,0], mount:'flush'}
  ]
};
function clusterCount(c){
  if(c.pyramid){var n=0;for(var i=0;i<c.pyramid.length;i++)n+=c.pyramid[i];return n;}
  return c.cols*c.rows;
}
function southPlaneY(z){
  return HOUSE.wall+HOUSE.rise*(1-Math.abs(z)/HOUSE.dz);
}
function westPlaneY(x){
  return HOUSE.wall+HOUSE.rise*((x+HOUSE.dx)/(HOUSE.dx-HOUSE.ridge/2));
}
function placeClusters(){
  var lift=0.14, pr=HOUSE.pitch*Math.PI/180, vlift=lift/Math.cos(pr);
  for(var i=0;i<SITE.clusters.length;i++){
    var c=SITE.clusters[i];
    if(c.key==='houseS'){
      var z=HOUSE.dz*0.55; c.tilt=HOUSE.pitch; c.pos=[0,southPlaneY(z)+vlift,z];
    }else if(c.key==='houseW'){
      var x=-HOUSE.dx*0.62; c.tilt=HOUSE.pitch; c.pos=[x,westPlaneY(x)+vlift,0];
    }else if(c.key==='terrace'){
      c.tilt=HOUSE.pitch;
      c.pos=[TERRACE.cx,
             HOUSE.wall-(TERRACE.cz-HOUSE.dz)*Math.tan(pr)+vlift,TERRACE.cz];
    }else if(c.key==='genbld'){
      var gp=GEN.pitch*Math.PI/180;
      c.tilt=GEN.pitch;
      c.pos=[GEN.cx,GEN.wall+0.07/Math.cos(gp)+0.07+lift/Math.cos(gp),GEN.cz];
    }
  }
}
/* per-channel ceilings from real panel counts */
const CAPS=(function(){
  var c={mppt80:0,south:0,west:0};
  for(var i=0;i<SITE.clusters.length;i++){
    var cl=SITE.clusters[i];
    c[cl.channel]+=clusterCount(cl)*SITE.wattsPerPanel;
  }
  c.total=c.mppt80+c.south+c.west;
  return c;
})();

const chargeStatusMap={0:'Not Charging',768:'Not Charging',769:'Bulk',770:'Absorption',773:'Float',774:'No Float',776:'Disabled',1025:'AC Pass-Thru'};
const genModeMap={0:'OFF',1:'ON',2:'AUTO',3:'Force On'};
/* AGS spec section 2.3 - Auto Generator Action */
const genActionMap={0:'Preheating',1:'Start delay',2:'Cranking',3:'Starter cooling',
  4:'Warming up',5:'Cooling down',6:'Spinning down',7:'Shutdown bypass',8:'Stopping',
  9:'Running',10:'Stopped',11:'Crank delay',255:'Unknown'};
const ACT_STARTING=[0,1,2,3,4,11], ACT_STOPPING=[5,6,7,8];
const PV_MAX=CAPS.total, AC_MAX=12000;
let currentConfig=null;
const shown={};
const history=new Array(28).fill(0);

/* ---- animated counters ---- */
function setNum(id,target,dec){
  const el=document.getElementById(id); if(!el)return;
  target=Number(target)||0; dec=dec||0;
  const from=shown[id]===undefined?target:shown[id];
  if(Math.abs(target-from)<Math.pow(10,-dec-1)){el.textContent=target.toFixed(dec);shown[id]=target;return;}
  const t0=performance.now(), dur=650;
  function step(now){
    const p=Math.min(1,(now-t0)/dur), e=1-Math.pow(1-p,3);
    const v=from+(target-from)*e;
    el.textContent=v.toFixed(dec);
    if(p<1)requestAnimationFrame(step); else shown[id]=target;
  }
  shown[id]=target; requestAnimationFrame(step);
}
function setBar(id,pct,live){
  const el=document.getElementById(id); if(!el)return;
  el.style.width=Math.max(0,Math.min(100,pct))+'%';
  el.classList.toggle('live',!!live && pct>1);
  el.style.opacity=live?1:0.32;
}
function toast(msg,kind){
  const box=document.getElementById('toasts');
  const t=document.createElement('div');
  t.className='toast '+(kind||'');
  t.textContent=msg; box.appendChild(t);
  setTimeout(()=>{t.classList.add('out');setTimeout(()=>t.remove(),320);},3400);
}

/* ---- sparkline ---- */
(function buildSpark(){
  const s=document.getElementById('spark');
  for(let i=0;i<history.length;i++)s.appendChild(document.createElement('i'));
})();
function pushSpark(v){
  history.push(v); history.shift();
  const peak=Math.max(PV_MAX*0.15,...history);
  const bars=document.getElementById('spark').children;
  for(let i=0;i<bars.length;i++)bars[i].style.height=Math.max(3,(history[i]/peak)*100)+'%';
}

function updateUI(data){
  const soc0=+data.batterySOC||0;
  const p1=+data.acPower1||0, p2=+data.acPower2||0;
  const mp=+data.mppt80PVPower||0, sp=+data.southArrayPVPower||0, wp=+data.westArrayPVPower||0;
  const totalPV=mp+sp+wp, totalAC=p1+p2;
  const totalCharge=(+data.chargePower1||0)+(+data.chargePower2||0)+(+data.chargePower3||0);

  setNum('totalPV_value',totalPV);
  setNum('pvPct_value',(totalPV/PV_MAX)*100,0);
  setNum('hudPv',totalPV);
  setNum('hudPvPct',(totalPV/PV_MAX)*100,0);
  setNum('hudLoad',totalAC);
  const bp=(data.battPower===undefined||data.battPower===null)?null:+data.battPower;
  const hb=document.getElementById('hudBatt');
  if(hb){
    hb.textContent=battText(bp);
    hb.style.color=(bp===null)?'':(bp>15?'var(--good)':(bp<-15?'var(--warn)':''));
  }
  const bf=document.getElementById('battFlow_value');
  if(bf)bf.textContent=battText(bp);
  const run=document.getElementById('hudRunway');
  if(run){
    const m=data.battMinToDischarge;
    if(bp!==null&&bp>15)run.textContent='charging';
    else if(m===undefined||m===null||m===0)run.textContent='--';
    else run.textContent=Math.floor(m/60)+'h '+(m%60)+'m left';
  }
  setNum('hudSoc',soc0);
  setNum('hudVolts',+data.batteryVoltage||0,1);
  const hr=document.getElementById('hudRing');
  if(hr)hr.style.strokeDashoffset=(2*Math.PI*47)*(1-Math.min(100,soc0)/100);
  setBar('totalPV_bar',(totalPV/PV_MAX)*100,totalPV>50);
  pushSpark(totalPV);
  document.getElementById('sunmark').classList.toggle('spin',totalPV>50);

  setNum('totalAC_value',totalAC);
  setNum('totalCharge_value',totalCharge);
  setBar('totalAC_bar',(totalAC/AC_MAX)*100,totalAC>50);

  setNum('acPower1_value',p1); setNum('acCurrent1_value',+data.acCurrent1||0,1);
  setNum('acPower2_value',p2); setNum('acCurrent2_value',+data.acCurrent2||0,1);
  setNum('chargePower1_value',+data.chargePower1||0);
  setNum('chargePower2_value',+data.chargePower2||0);

  const soc=+data.batterySOC||0;
  setNum('batterySOC_value',soc);
  setNum('batteryVoltage_value',+data.batteryVoltage||0,1);
  const C=2*Math.PI*47;
  document.getElementById('socRing').style.strokeDashoffset=C*(1-Math.min(100,soc)/100);

  setNum('mppt80PVPower_value',mp); setNum('mppt80PVVoltage_value',+data.mppt80PVVoltage||0,1);
  setNum('southArrayPVPower_value',sp); setNum('southArrayPVVoltage_value',+data.southArrayPVVoltage||0,1);
  setNum('westArrayPVPower_value',wp); setNum('westArrayPVVoltage_value',+data.westArrayPVVoltage||0,1);
  setBar('mppt80_bar',(mp/CAPS.mppt80)*100,mp>20);
  setBar('south_bar',(sp/CAPS.south)*100,sp>20);
  setBar('west_bar',(wp/CAPS.west)*100,wp>20);
  document.getElementById('mppt80ChargeStatus_value').textContent=chargeStatusMap[data.mppt80ChargeStatus]||'Unknown';
  document.getElementById('southArrayChargeStatus_value').textContent=chargeStatusMap[data.southArrayChargeStatus]||'Unknown';
  document.getElementById('westArrayChargeStatus_value').textContent=chargeStatusMap[data.westArrayChargeStatus]||'Unknown';

  /* generators */
  const mepMode=+data.mep803aMode||0, kubMode=+data.kubotaMode||0;
  const mepAct=(data.mep803aAction===undefined)?255:+data.mep803aAction;
  const kubAct=(data.kubotaAction===undefined)?255:+data.kubotaAction;
  const mepRate=+data.mepChargeRateLive||0, kubRate=+data.kubotaChargeRateLive||0;
  document.getElementById('mep803aMode_value').textContent=genModeMap[mepMode]||'Unknown';
  document.getElementById('kubotaMode_value').textContent=genModeMap[kubMode]||'Unknown';
  setNum('mepChargeRateLive_value',mepRate);
  setNum('kubotaChargeRateLive_value',kubRate);
  /* fill shows the configured rate; shimmer and full opacity only when the
     AGS reports the generator actually turning */
  setBar('mepRate_bar',mepRate,mepAct===9);
  setBar('kubRate_bar',kubRate,kubAct===9);
  chipFor('mep_chip',mepMode,mepAct);
  chipFor('kub_chip',kubMode,kubAct);
  hudChip('hud_mep','MEP',mepMode,mepAct);
  hudChip('hud_kub','KUB',kubMode,kubAct);
  document.getElementById('mep_action').textContent=genActionMap[mepAct]||'Unknown';
  document.getElementById('kub_action').textContent=genActionMap[kubAct]||'Unknown';

  agsFor('mep_ags_status',data.mepAgsOnline);
  agsFor('kubota_ags_status',data.kubotaAgsOnline);

  /* status */
  const errors=+data.pollErrors||0;
  document.getElementById('pollErrors_value').textContent=errors;
  document.getElementById('lastUpdate_value').textContent=data.clockTime||data.lastUpdate||'--:--:--';
  const up=document.getElementById('uptime_value');
  if(up)up.textContent=data.lastUpdate||'--:--:--';
  const dot=document.getElementById('liveDot');
  if(errors>0){dot.className='dot err';}
  else{dot.className='dot';void dot.offsetWidth;dot.classList.add('beat');}
  const banner=document.getElementById('errorBanner');
  if(errors>0){banner.style.display='flex';banner.textContent='\\u26a0\\ufe0f '+errors+' Modbus read error(s) in last poll cycle';}
  else{banner.style.display='none';}
  if(window.Site3D&&Site3D.isReady())Site3D.update(data);
}

/* Driven by the AGS Auto Generator Action register, not by charge rate:
   charge rate rests at its configured maximum (100% MEP / 70% Kubota) at all
   times, so it says nothing about whether the generator is turning. */
function battText(w){
  if(w===null||w===undefined)return '--';
  const a=Math.abs(w);
  if(a<15)return 'idle';
  return (w>0?'+':'-')+a+' W '+(w>0?'charging':'discharging');
}
function hudChip(id,tag,mode,action){
  const el=document.getElementById(id);
  if(!el)return;
  let cls='off', txt=tag+' idle';
  if(action===9){cls='run';txt=tag+' running';}
  else if(ACT_STARTING.indexOf(action)>=0){cls='run';txt=tag+' '+genActionMap[action];}
  else if(ACT_STOPPING.indexOf(action)>=0){cls='warn';txt=tag+' '+genActionMap[action];}
  else if(mode===2){cls='ok';txt=tag+' auto';}
  else if(mode===0){txt=tag+' off';}
  el.className='chip '+cls;
  el.textContent=txt;
}
function chipFor(id,mode,action){
  const el=document.getElementById(id);
  if(action===9){el.className='chip run';el.innerHTML="<span class='dot'></span>Running";}
  else if(ACT_STARTING.indexOf(action)>=0){
    el.className='chip run';el.innerHTML="<span class='dot'></span>"+genActionMap[action];}
  else if(ACT_STOPPING.indexOf(action)>=0){
    el.className='chip warn';el.innerHTML="<span class='dot'></span>"+genActionMap[action];}
  else if(mode===2){el.className='chip ok';el.textContent='Auto \u00b7 stopped';}
  else if(mode===0){el.className='chip off';el.textContent='Off';}
  else{el.className='chip off';el.textContent=genModeMap[mode]||'Idle';}
}
function agsFor(id,online){
  const el=document.getElementById(id);
  if(online){el.textContent='AGS online';el.style.color='var(--good)';}
  else{el.textContent='AGS OFFLINE';el.style.color='var(--bad)';el.style.fontWeight='700';}
}

function updateConfigUI(cfg){
  if(!cfg)return;
  currentConfig=cfg;
  const enabled=cfg.autoGenEnabled===true;
  const st=document.getElementById('autoGenStatus');
  st.textContent=enabled?'ENABLED':'DISABLED';
  st.style.color=enabled?'var(--good)':'var(--bad)';
  const btn=document.getElementById('autoGenToggle');
  btn.textContent=enabled?'DISABLE':'ENABLE';
  btn.className=enabled?'btn g':'btn d';
  if(cfg.mep803a){
    setIf('mepStartV',cfg.mep803a.startVoltage,51.5);
    setIf('mepStopV',cfg.mep803a.stopVoltage,55.0);
    setIf('mepChargeRate',cfg.mep803a.chargeRate,100);
    setIf('mepMaxRuntime',cfg.mep803a.maxRuntime,120);
    setIf('mepCooldown',cfg.mep803a.cooldown,5);
  }
  if(cfg.kubota){
    setIf('kubStartV',cfg.kubota.startVoltage,52.3);
    setIf('kubStopV',cfg.kubota.stopVoltage,55.0);
    setIf('kubChargeRate',cfg.kubota.chargeRate,70);
    setIf('kubMaxRuntime',cfg.kubota.maxRuntime,120);
    setIf('kubCooldown',cfg.kubota.cooldown,5);
  }
  if(cfg.rampDown){
    setIf('rampStepDelay',cfg.rampDown.stepDelay,15);
    setIf('rampZeroHold',cfg.rampDown.zeroHoldTime,120);
  }
  if(cfg.telegram){
    const tgOn=cfg.telegram.enabled===true;
    const ts=document.getElementById('telegramStatus');
    ts.textContent=tgOn?'ON':'OFF';
    ts.style.color=tgOn?'var(--good)':'var(--bad)';
    const tb=document.getElementById('telegramToggle');
    tb.textContent=tgOn?'DISABLE':'ENABLE';
    tb.className=tgOn?'btn g sm':'btn d sm';
    setIf('tgToken',cfg.telegram.token,'');
    setIf('tgChatId',cfg.telegram.chatId,'');
  }
}
/* don't clobber a field the user is typing in */
function setIf(id,val,dflt){
  const el=document.getElementById(id);
  if(!el||el===document.activeElement)return;
  el.value=(val===undefined||val===null)?dflt:val;
}

/* The event log holds two things now: the dashboard's own events, and the
   answers the agent has given. The config poll refreshes the events every few
   seconds, so the answers are kept here and re-rendered with them rather than
   written into the panel once and wiped by the next poll. */
let lastEvents=[],askBlocks=[];

function eventLine(e){
  let cls='e-info';
  const low=e.toLowerCase();
  if(e.indexOf('\\u26a0')>=0||low.indexOf('failed')>=0||low.indexOf('offline')>=0||low.indexOf('error')>=0)cls='e-err';
  else if(e.indexOf('\\u2705')>=0||low.indexOf('started')>=0||low.indexOf('online')>=0||low.indexOf('restored')>=0)cls='e-ok';
  else if(low.indexOf('warn')>=0||low.indexOf('ramp')>=0)cls='e-warn';
  const div=document.createElement('div');
  div.className=cls; div.textContent=e;
  return div;
}

function renderEventLog(){
  const log=document.getElementById('eventLog');
  if(!log)return;
  log.innerHTML='';
  askBlocks.forEach(function(b,i){
    const q=document.createElement('div');
    q.className='ask-q'; q.id='askBlock'+i;
    q.textContent='\\u276f '+b.q;
    log.appendChild(q);
    const a=document.createElement('div');
    a.className=b.pending?'ask-a ask-wait':'ask-a';
    a.textContent=b.pending?'thinking\\u2026':b.a;
    log.appendChild(a);
  });
  if(!lastEvents||lastEvents.length===0){
    if(!askBlocks.length)log.innerHTML="<div class='e-info'>No events yet...</div>";
    return;
  }
  lastEvents.slice().reverse().forEach(e=>log.appendChild(eventLine(e)));
}

function updateEventLog(events){
  lastEvents=events||[];
  renderEventLog();
}

/* ---- ask the agent ---- */
function scrollToAsk(i){
  const panel=document.getElementById('logPanel');
  if(panel)panel.open=true;
  const log=document.getElementById('eventLog'),el=document.getElementById('askBlock'+i);
  if(log&&el)log.scrollTop=Math.max(0,el.offsetTop-log.offsetTop);
}

async function askAgent(preset){
  const input=document.getElementById('askInput');
  const text=(preset||(input?input.value:'')||'').trim();
  if(!text)return;
  if(!preset&&input)input.value='';
  /* Newest first, to match the events below it. */
  askBlocks.unshift({q:text,a:'',pending:true});
  if(askBlocks.length>6)askBlocks.pop();
  renderEventLog();
  scrollToAsk(0);
  const block=askBlocks[0];
  try{
    const r=await fetch('/agent/ask',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})});
    const d=r.ok?await r.json():null;
    block.a=(d&&d.online&&d.reply)?d.reply:'The agent is not answering right now.';
  }catch(e){
    block.a='The agent is not answering right now.';
  }
  block.pending=false;
  renderEventLog();
  scrollToAsk(askBlocks.indexOf(block));
}

/* ---- AC diagnostic (ported from V2.4) ---- */
function fmtV(v){return v!=null?v.toFixed(3)+'V':'--';}
function fmtHz(v){return v!=null?v.toFixed(2)+'Hz':'--';}
function fmtW(v){return v!=null?v+'W':'--';}
function fmtDelta(v,warn){
  if(v==null)return'--';
  const s=(v>=0?'+':'')+v.toFixed(4);
  return'<span class="'+(Math.abs(v)>warn?'delta-warn':'delta-ok')+'">'+s+'</span>';
}

function diagUpdateCards(d){
  const m=d.master, s=d.slave, dt=d.delta;
  document.getElementById('diag_m_v1').textContent=fmtV(m.voltage_l1);
  document.getElementById('diag_m_v2').textContent=fmtV(m.voltage_l2);
  document.getElementById('diag_s_v1').textContent=fmtV(s.voltage_l1);
  document.getElementById('diag_s_v2').textContent=fmtV(s.voltage_l2);
  document.getElementById('diag_dv').innerHTML=fmtDelta(dt.voltage_l1, 0.1);
  document.getElementById('diag_m_hz').textContent=fmtHz(m.frequency);
  document.getElementById('diag_s_hz').textContent=fmtHz(s.frequency);
  document.getElementById('diag_dhz').innerHTML=fmtDelta(dt.frequency, 0.05);
  document.getElementById('diag_m_pw').textContent=fmtW(m.power_w);
  document.getElementById('diag_s_pw').textContent=fmtW(s.power_w);
  // Show short timestamp HH:MM:SS
  const ts=d.timestamp?d.timestamp.substring(11,19):'--';
  document.getElementById('diag_ts').textContent=ts;
}

function diagAddRows(readings){
  const tbody=document.getElementById('diag_log_body');
  // Prepend rows (newest first)
  const rows=[...readings].reverse().map(d=>{
    const m=d.master, s=d.slave, dt=d.delta;
    const ts=d.timestamp?d.timestamp.substring(11,19):'?';
    return '<tr>'+
      '<td>'+ts+'</td>'+
      '<td>'+(m.voltage_l1!=null?m.voltage_l1.toFixed(3):'--')+'</td>'+
      '<td>'+(s.voltage_l1!=null?s.voltage_l1.toFixed(3):'--')+'</td>'+
      '<td>'+fmtDelta(dt.voltage_l1,0.1)+'</td>'+
      '<td>'+(m.frequency!=null?m.frequency.toFixed(2):'--')+'</td>'+
      '<td>'+(s.frequency!=null?s.frequency.toFixed(2):'--')+'</td>'+
      '<td>'+fmtDelta(dt.frequency,0.05)+'</td>'+
      '<td>'+(m.power_w!=null?m.power_w:'--')+'</td>'+
      '<td>'+(s.power_w!=null?s.power_w:'--')+'</td>'+
      '</tr>';
  }).join('');
  // If placeholder row exists, clear it
  if(tbody.querySelector('td[colspan]'))tbody.innerHTML='';
  tbody.insertAdjacentHTML('afterbegin',rows);
}

async function diagSnapshot(){
  const st=document.getElementById('diag_status');
  st.textContent='Reading...';st.className='dstat running';
  try{
    const r=await fetch('/acdiag');
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    diagUpdateCards(d);
    diagAddRows([d]);
    st.textContent='Snapshot complete at '+d.timestamp.substring(11,19);
    st.className='dstat done';
  }catch(e){
    st.textContent='Error: '+e;st.className='dstat error';
  }
}

async function diagStream(){
  const st=document.getElementById('diag_status');
  st.textContent='Capturing 20 readings over 10 seconds...';st.className='dstat running';
  try{
    const r=await fetch('/acdiag/stream?n=20&interval=500');
    if(!r.ok)throw new Error('HTTP '+r.status);
    const readings=await r.json();
    if(readings.length>0){
      diagUpdateCards(readings[readings.length-1]);
      diagAddRows(readings);
    }
    st.textContent='Stream capture complete — '+readings.length+' readings saved to log.';
    st.className='dstat done';
  }catch(e){
    st.textContent='Error: '+e;st.className='dstat error';
  }
}

async function diagLoadLog(){
  const st=document.getElementById('diag_status');
  st.textContent='Loading log...';st.className='dstat running';
  try{
    const r=await fetch('/acdiag/log?n=50');
    if(!r.ok)throw new Error('HTTP '+r.status);
    const entries=await r.json();
    const tbody=document.getElementById('diag_log_body');
    tbody.innerHTML='';
    if(entries.length===0){
      tbody.innerHTML='<tr><td colspan="9" style="text-align:center;color:#5f6f83;">No log entries</td></tr>';
    }else{
      diagAddRows(entries);
      document.getElementById('diag_log_count').textContent='Log: '+entries.length+' entries shown';
    }
    st.textContent='Log loaded — '+entries.length+' entries.';
    st.className='dstat done';
  }catch(e){
    st.textContent='Error: '+e;st.className='dstat error';
  }
}

async function diagClearLog(){
  if(!confirm('Clear all AC diagnostic log entries?'))return;
  const st=document.getElementById('diag_status');
  try{
    await fetch('/acdiag/log/clear');
    document.getElementById('diag_log_body').innerHTML=
      '<tr><td colspan="9" style="text-align:center;color:#5f6f83;">Log cleared</td></tr>';
    document.getElementById('diag_log_count').textContent='Log: 0 entries';
    st.textContent='Log cleared.';st.className='dstat done';
  }catch(e){
    st.textContent='Error: '+e;st.className='dstat error';
  }
}

async function fetchData(){
  try{const r=await fetch('/data');if(r.ok)updateUI(await r.json());}
  catch(e){console.error('fetchData:',e);}
}
async function fetchConfig(){
  try{
    const r=await fetch('/config');
    if(r.ok){const d=await r.json();if(d.config)updateConfigUI(d.config);if(d.events)updateEventLog(d.events);}
  }catch(e){console.error('fetchConfig:',e);}
}
async function toggleAutoGen(){
  if(!currentConfig){toast('Config not loaded yet','bad');return;}
  const r=await fetch('/config?autoGenEnabled='+(currentConfig.autoGenEnabled?'0':'1'));
  if(r.ok){toast('Auto control '+(currentConfig.autoGenEnabled?'disabled':'enabled'),'ok');fetchConfig();}
  else toast('Toggle failed','bad');
}
async function toggleTelegram(){
  if(!currentConfig){toast('Config not loaded yet','bad');return;}
  const r=await fetch('/config?tg.enabled='+(currentConfig.telegram.enabled?'0':'1'));
  if(r.ok){toast('Telegram alerts updated','ok');fetchConfig();}
  else toast('Toggle failed','bad');
}
async function saveGroup(params,label){
  const r=await fetch('/config?'+new URLSearchParams(params));
  if(r.ok){toast(label+' saved','ok');fetchConfig();}else toast(label+' save failed','bad');
}
function gv(id){return document.getElementById(id).value;}
function saveMepSettings(){saveGroup({'mep.startVoltage':gv('mepStartV'),'mep.stopVoltage':gv('mepStopV'),'mep.chargeRate':gv('mepChargeRate'),'mep.maxRuntime':gv('mepMaxRuntime'),'mep.cooldown':gv('mepCooldown')},'MEP settings');}
function saveKubSettings(){saveGroup({'kub.startVoltage':gv('kubStartV'),'kub.stopVoltage':gv('kubStopV'),'kub.chargeRate':gv('kubChargeRate'),'kub.maxRuntime':gv('kubMaxRuntime'),'kub.cooldown':gv('kubCooldown')},'Kubota settings');}
function saveRampSettings(){saveGroup({'ramp.stepDelay':gv('rampStepDelay'),'ramp.zeroHoldTime':gv('rampZeroHold')},'Ramp settings');}
function saveTelegramSettings(){saveGroup({'tg.token':gv('tgToken'),'tg.chatId':gv('tgChatId')},'Telegram settings');}
async function testTelegram(){
  toast('Sending test message...');
  try{const r=await fetch('/testtelegram');const d=await r.json();toast(d.message,d.success?'ok':'bad');}
  catch(e){toast('Test failed: '+e,'bad');}
}
function setGeneratorMode(slaveId,mode){
  const modeText={0:'OFF',1:'ON',2:'AUTO'}[mode]||'Unknown';
  if(!confirm('Set generator to '+modeText+'?'))return;
  const endpoint=(mode==0)?'/stopgen?id='+slaveId:'/setgen?id='+slaveId+'&state='+mode;
  fetch(endpoint).then(r=>{
    toast(r.ok?'Command sent: '+modeText:'Command failed',r.ok?'ok':'bad');
    setTimeout(fetchData,1000);
  }).catch(e=>toast('Error: '+e,'bad'));
}

/* ---- solar agent status badge ---- */
let agentPlan=null,agentPlanShown=false;
async function fetchAgent(){
  try{
    const r=await fetch('/agent/plan');
    renderAgent(r.ok?await r.json():null);
  }catch(e){renderAgent(null);}
}
function renderAgent(d){
  agentPlan=d;
  const dot=document.getElementById('agentDot'),txt=document.getElementById('agentText');
  if(!dot||!txt)return;
  if(!d||d.online===false){
    dot.className='dot off';txt.textContent='Agent offline';
    hideAgentPlan();
    return;
  }
  if(!d.ts){dot.className='dot stale';txt.textContent='Agent \\u00b7 no plan yet';return;}
  const mins=Math.max(0,Math.floor((Date.now()/1000-d.ts)/60));
  dot.className='dot'+(mins>30?' stale':'');
  /* 12-hour with am/pm, as everywhere else the owner reads a time. */
  const at=new Date(d.ts*1000).toLocaleTimeString('en-US',
    {hour:'numeric',minute:'2-digit'}).toLowerCase();
  let s='Agent \\u00b7 tick '+at+' ('+mins+' min ago)';
  if(d.learning&&d.learning.open===false)s+=' \\u00b7 learning';
  txt.textContent=s;
  if(agentPlanShown)showAgentPlan();
}
/* The plan goes in a popover anchored to the badge, not into the event log:
   the log is for the dashboard's own events, and a plan record buried in it
   scrolls away as soon as anything else happens. */
function showAgentPlan(){
  const pop=document.getElementById('agentPop');
  if(!pop)return;
  pop.innerHTML='';
  const head=document.createElement('h4');
  head.textContent='Latest plan';
  pop.appendChild(head);
  const body=(agentPlan&&agentPlan.text)?agentPlan.text:'No plan has been recorded yet.';
  body.split('\\n').forEach(function(line){
    const d=document.createElement('div');
    d.className='pl'+(/:\\s*FIRES\\b/.test(line)?' fire':'')
                   +(/^(recommend|applied):/i.test(line)?' rec':'');
    d.textContent=line;
    pop.appendChild(d);
  });
  const acts=(agentPlan&&agentPlan.actions)||[];
  const wrap=document.createElement('div');
  wrap.className='acts';
  const ah=document.createElement('h4');
  ah.textContent='Last '+(acts.length||5)+' actions';
  wrap.appendChild(ah);
  if(!acts.length){
    const d=document.createElement('div');
    d.className='pl';
    d.textContent='Nothing has been attempted yet.';
    wrap.appendChild(d);
  }
  acts.forEach(function(a){
    const row=document.createElement('div');
    row.className='act';
    const at=document.createElement('span');
    at.className='at';at.textContent=a.at||'';
    const rs=document.createElement('span');
    rs.className='rs'+(a.result==='allowed'?' yes':' no');
    rs.textContent=a.result||'';
    const why=document.createElement('span');
    why.className='why';why.textContent=a.reason||'';
    row.appendChild(at);row.appendChild(rs);row.appendChild(why);
    wrap.appendChild(row);
  });
  pop.appendChild(wrap);
  pop.hidden=false;
}
function hideAgentPlan(){
  agentPlanShown=false;
  const pop=document.getElementById('agentPop');
  if(pop)pop.hidden=true;
}
function toggleAgentPlan(e){
  if(e)e.stopPropagation();
  agentPlanShown=!agentPlanShown;
  if(agentPlanShown)showAgentPlan();else hideAgentPlan();
}
document.addEventListener('click',function(){if(agentPlanShown)hideAgentPlan();});
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'&&agentPlanShown)hideAgentPlan();
});

/* pause polling when tab hidden, resume immediately on return */
let dataTimer,cfgTimer,agentTimer;
function startPolling(){
  clearInterval(dataTimer);clearInterval(cfgTimer);clearInterval(agentTimer);
  dataTimer=setInterval(fetchData,5000);
  cfgTimer=setInterval(fetchConfig,15000);
  agentTimer=setInterval(fetchAgent,60000);
}
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){
    clearInterval(dataTimer);clearInterval(cfgTimer);clearInterval(agentTimer);
    if(window.Site3D)Site3D.stop();
    return;
  }
  /* on return: ALWAYS restart data polling, then resume the 3D if it is on.
     The old logic put these in an if/else, so with the 3D active the polling
     restart never ran and every value froze until a manual refresh. */
  fetchData();fetchConfig();fetchAgent();startPolling();
  if(s3dOn&&window.Site3D&&Site3D.isReady())Site3D.start();
});
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('capKw_value').textContent=(CAPS.total/1000).toFixed(1);
  fetchData();fetchConfig();fetchAgent();startPolling();
});
/* ---- sun position scrubber ---- */
let sunTimer=null;
function fmtSun(){
  if(!window.Site3D||!Site3D.isReady())return;
  const s=Site3D.sunInfo();
  const el=document.getElementById('sunLabel');
  const pad=n=>String(n).padStart(2,'0');
  let t;
  if(s.override===null){
    const d=new Date();
    t=pad(d.getHours())+':'+pad(d.getMinutes())+' live';
  }else{
    t=pad(Math.floor(s.override))+':'+pad(Math.round((s.override%1)*60));
  }
  el.textContent=t+'  \u00b7  '+s.elevation.toFixed(0)+'\u00b0 el  '
    +s.azimuth.toFixed(0)+'\u00b0 az'+(s.night?'  (night)':'');
}
function onSunTime(v){
  if(!window.Site3D)return;
  Site3D.setTime(parseFloat(v));
  fmtSun();
}
function sunNow(){
  if(!window.Site3D)return;
  Site3D.setTime(null);
  const d=new Date();
  document.getElementById('sunTime').value=(d.getHours()+d.getMinutes()/60).toFixed(2);
  fmtSun();
}

function toggleShadows(){
  if(!window.Site3D||!Site3D.isReady())return;
  const on=!Site3D.shadowsOn();
  Site3D.setShadows(on);
  document.getElementById('shadowBtn').textContent=on?'Shadows':'Shadows off';
}
function resetView(){ if(window.Site3D&&Site3D.isReady())Site3D.resetView(); }

/* ---- 3D lazy loader + lifecycle ---- */
let s3dLoaded=false,s3dOn=false,s3dObs=null;
function libSrc(list,done,fail){
  if(!list.length){fail();return;}
  const s=document.createElement('script');
  s.src=list[0];
  s.onload=done;
  s.onerror=()=>libSrc(list.slice(1),done,fail);
  document.head.appendChild(s);
}
function toggle3D(){
  const card=document.getElementById('stage3dCard');
  const btn=document.getElementById('toggle3d');
  s3dOn=!s3dOn;
  try{localStorage.setItem('solar3d',s3dOn?'1':'0');}catch(e){}
  const canvasHost=document.getElementById('stage3d');
  if(!s3dOn){
    if(canvasHost)canvasHost.style.display='none';
    btn.textContent='3D on';
    if(sunTimer){clearInterval(sunTimer);sunTimer=null;}
    if(window.Site3D)Site3D.stop();
    return;
  }
  if(canvasHost)canvasHost.style.display='block';
  btn.textContent='3D off';
  if(s3dLoaded){boot3D();return;}
  libSrc(['/vendor/three.min.js',
          'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js'],
    ()=>{s3dLoaded=true;boot3D();},
    ()=>{document.getElementById('stage3dMsg').textContent=
      'Renderer unavailable - no local copy and no internet route.';});
}
function boot3D(){
  const msg=document.getElementById('stage3dMsg');
  msg.style.display='block';
  if(typeof Site3D==='undefined'){
    msg.textContent=['Scene module failed to initialise.',
      'Check the console for an error thrown while the page loaded.'].join(NL);
    return;
  }
  if(typeof THREE==='undefined'){
    msg.textContent='three.js did not load (THREE is undefined).';return;
  }
  const rev=parseInt(THREE.REVISION,10);
  if(!(rev>=125)){
    msg.textContent=['three.js r'+THREE.REVISION+' is too old.',
      'This scene needs r125 or newer (Matrix4.invert, Float32BufferAttribute).',
      'Re-vendor with: curl -o vendor/three.min.js '
        +'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js'].join(NL);
    return;
  }
  let gl=null;
  try{ gl=document.createElement('canvas').getContext('webgl2')
          ||document.createElement('canvas').getContext('webgl'); }catch(e){}
  if(!gl){ msg.textContent='No WebGL context available in this browser.'; return; }
  try{
    if(!Site3D.isReady())Site3D.init(document.getElementById('stage3d'));
    msg.style.display='none';
    sunNow();
    if(!sunTimer)sunTimer=setInterval(fmtSun,20000);
    Site3D.start();
    if(!s3dObs&&'IntersectionObserver' in window){
      s3dObs=new IntersectionObserver(es=>{
        if(!s3dOn)return;
        es[0].isIntersecting?Site3D.start():Site3D.stop();
      },{threshold:0.05});
      s3dObs.observe(document.getElementById('stage3dCard'));
    }
  }catch(e){
    const frames=(e.stack||'').split(NL).slice(1,3).join(NL).trim();
    msg.style.display='block';
    msg.textContent=['Renderer failed','',
      (e.name||'Error')+': '+e.message,'',
      'three.js r'+THREE.REVISION,
      frames||'(no stack)','',
      'Full stack is in the browser console.'].join(NL);
    console.error('[Site3D] init failed',e);
  }
}

/* restore last choice */
document.addEventListener('DOMContentLoaded',()=>{
  let want='1'; try{want=localStorage.getItem('solar3d')||'1';}catch(e){}
  if(want==='1')toggle3D();
});
</script>

<script>
/* ===== 3D SITE MODEL =======================================================
   Geometry derives from HOUSE / TERRACE / GEN / SITE. Correct a number there
   and the model, the power ceilings and the flow paths all follow.

   north = -Z, east = +X, up = +Y, metres.
   azimuth = degrees clockwise from north (180 south, 270 west)

   NOTE: nothing in this module may construct a THREE object at parse time.
   three.js is loaded on demand, long after these scripts run.
========================================================================== */
var Site3D=(function(){
  var R,S,CAM,W,H,host,root,raf=null,running=false,ready=false;
  var M={},panelMats={mppt80:[],south:[],west:[]},caps3={mppt80:0,south:0,west:0};
  var sun,sunMesh,hemi,battModules=[],invM,invS,genMep,genKub;
  var ctrl={},flows=[],pts,ptGeo,ptPos,ptCol;
  var last=0,tPrev=0,FRAME=1000/30,timeOverride=null,shadowsOn=true;
  var live={mppt80:0,south:0,west:0,soc:0,acM:0,acS:0,gMep:0,gKub:0,batt:null};
  var yaw=-0.7,pitch=0.44,dist=52,target=null,fitR=26,drag=false,lx=0,ly=0;
  var D=Math.PI/180;
  var C={solar:0xffb020,batt:0x3ddc97,ac:0x4cc9f0,gen:0xc084fc,
         panel:0x0e1a30,panelHot:0x2b4266,frame:0xb9c2cc,
         wall:0x9a9ea4,trim:0xe9e6df,roof:0xc9d1d8,post:0x6b3f2a,
         steel:0x5a3a2b,deck:0x9c8a6f,ground:0x8a7355,scrub:0x4a5535,
         box:0xe6e9ec,dark:0x2b3644,red:0x8e4130};

  /* ---------- textures, failing soft ---------- */
  function cv(w,h){var c=document.createElement('canvas');c.width=w;c.height=h;return c;}
  function tex(fn,rx,ry){
    try{
      var c=fn(); if(!c)return null;
      var t=new THREE.CanvasTexture(c);
      t.wrapS=t.wrapT=THREE.RepeatWrapping;
      t.repeat.set(rx||1,ry||1);
      return t;
    }catch(e){return null;}
  }
  function corrugated(){
    var c=cv(64,8),g=c.getContext('2d'); if(!g)return null;
    for(var x=0;x<64;x++){
      var v=0.62+0.38*Math.abs(Math.sin(x/64*Math.PI*8));
      g.fillStyle='rgb('+(v*196|0)+','+(v*205|0)+','+(v*212|0)+')';
      g.fillRect(x,0,1,8);
    }
    return c;
  }
  function panelFace(){
    var c=cv(96,144),g=c.getContext('2d'); if(!g)return null;
    g.fillStyle='#0e1a30'; g.fillRect(0,0,96,144);
    for(var i=0;i<6;i++)for(var j=0;j<9;j++){
      g.fillStyle='rgba(26,52,96,1)';
      g.fillRect(i*16+2,j*16+2,13,13);
      g.strokeStyle='rgba(150,180,220,.18)'; g.lineWidth=1;
      g.beginPath(); g.moveTo(i*16+8,j*16+2); g.lineTo(i*16+8,j*16+15); g.stroke();
    }
    g.strokeStyle='rgba(180,200,225,.3)'; g.lineWidth=2; g.strokeRect(1,1,94,142);
    return c;
  }
  function stucco(){
    var c=cv(96,96),g=c.getContext('2d'); if(!g)return null;
    g.fillStyle='#9a9ea4'; g.fillRect(0,0,96,96);
    for(var i=0;i<1400;i++){
      g.fillStyle='rgba(0,0,0,'+(Math.random()*0.09).toFixed(3)+')';
      g.fillRect(Math.random()*96|0,Math.random()*96|0,2,2);
    }
    return c;
  }
  function dirtTex(){
    var c=cv(128,128),g=c.getContext('2d'); if(!g)return null;
    g.fillStyle='#8a7355'; g.fillRect(0,0,128,128);
    for(var i=0;i<2200;i++){
      var t=Math.random();
      g.fillStyle='rgba('+(120+t*50|0)+','+(100+t*42|0)+','+(74+t*30|0)+',.5)';
      g.fillRect(Math.random()*128|0,Math.random()*128|0,3,3);
    }
    return c;
  }

  /* ---------- shared materials: ~12 total, not one per mesh ---------- */
  function buildMaterials(){
    var TXc=tex(corrugated,1,18), TXs=tex(stucco,2,2),
        TXd=tex(dirtTex,18,18), TXp=tex(panelFace,1,1);
    function sm(o){return new THREE.MeshStandardMaterial(o);}
    M.wall =sm({color:C.wall,map:TXs,roughness:.92});
    M.wallD=sm({color:C.wall,map:TXs,roughness:.92,side:THREE.DoubleSide});
    M.roof =sm({color:C.roof,map:TXc,roughness:.55,metalness:.35});
    M.roofD=sm({color:C.roof,map:TXc,roughness:.55,metalness:.35,
                side:THREE.DoubleSide});
    M.trim =sm({color:C.trim,roughness:.78});
    M.post =sm({color:C.post,roughness:.7,metalness:.15});
    M.steel=sm({color:C.steel,roughness:.55,metalness:.45});
    M.deck =sm({color:C.deck,roughness:.9});
    M.glass=sm({color:0x1a2733,roughness:.08,metalness:.9});
    M.frame=sm({color:C.frame,roughness:.4,metalness:.75});
    M.case =sm({color:C.box,roughness:.42,metalness:.12});
    M.dark =sm({color:C.dark,roughness:.6,metalness:.3});
    M.red  =sm({color:C.red,roughness:.88});
    M.cap  =sm({color:0xc4cbd2,roughness:.7,metalness:.2});
    M.ground=sm({color:C.ground,map:TXd,roughness:.98});
    M.scrub=sm({color:C.scrub,roughness:.96});
    /* one panel material per channel, shared by every panel on it */
    M.pv={};
    M.pv.mppt80=sm({color:0xffffff,map:TXp,roughness:.22,metalness:.35});
    M.pv.south =sm({color:0xffffff,map:TXp,roughness:.22,metalness:.35});
    M.pv.west  =sm({color:0xffffff,map:TXp,roughness:.22,metalness:.35});
  }

  /* ---------- primitives ---------- */
  function box(w,h,d,x,y,z,m,cast,recv){
    var o=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),m);
    o.position.set(x,y,z);
    o.castShadow=cast!==false; o.receiveShadow=recv!==false;
    root.add(o); return o;
  }
  function pyramidRoof(w,d,rise,ridge,x,y){
    var hw=w/2,hd=d/2,hr=ridge/2,v,f;
    if(Math.abs(ridge)<0.02){
      v=[-hw,0,-hd, hw,0,-hd, hw,0,hd, -hw,0,hd, 0,rise,0];
      f=[0,1,4, 1,2,4, 2,3,4, 3,0,4];
    }else{
      v=[-hw,0,-hd, hw,0,-hd, hw,0,hd, -hw,0,hd, -hr,rise,0, hr,rise,0];
      f=[0,1,5, 0,5,4, 1,2,5, 2,3,4, 2,4,5, 3,0,4];
    }
    var pos=[],uv=[];
    for(var i=0;i<f.length;i++){
      pos.push(v[f[i]*3],v[f[i]*3+1],v[f[i]*3+2]);
      uv.push((v[f[i]*3]+hw)/w,(v[f[i]*3+2]+hd)/d);
    }
    var g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
    g.setAttribute('uv',new THREE.Float32BufferAttribute(uv,2));
    g.computeVertexNormals();
    var o=new THREE.Mesh(g,M.roofD);
    o.position.set(x,y,0); o.castShadow=true; o.receiveShadow=true;
    root.add(o);
    box(w,0.2,d,x,y-0.1,0,M.trim,true,true);
    return o;
  }
  function skirtRing(wi,di,wo,dou,yi,yo){
    var IN=[[-wi,yi,-di],[wi,yi,-di],[wi,yi,di],[-wi,yi,di]];
    var OUT=[[-wo,yo,-dou],[wo,yo,-dou],[wo,yo,dou],[-wo,yo,dou]];
    var pos=[],uv=[];
    function p(v,u,w2){pos.push(v[0],v[1],v[2]); uv.push(u,w2);}
    for(var i=0;i<4;i++){
      var j=(i+1)%4;
      p(IN[i],0,0); p(IN[j],1,0); p(OUT[j],1,1);
      p(IN[i],0,0); p(OUT[j],1,1); p(OUT[i],0,1);
    }
    var g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
    g.setAttribute('uv',new THREE.Float32BufferAttribute(uv,2));
    g.computeVertexNormals();
    var o=new THREE.Mesh(g,M.roofD);
    o.castShadow=true; o.receiveShadow=true; root.add(o);
    return o;
  }
  function shedBox(w,d,hN,hS,x,z){
    var hw=w/2,hd=d/2;
    var v=[[-hw,0,-hd],[hw,0,-hd],[hw,0,hd],[-hw,0,hd],
           [-hw,hN,-hd],[hw,hN,-hd],[hw,hS,hd],[-hw,hS,hd]];
    var f=[[0,4,5],[0,5,1],[3,2,6],[3,6,7],[0,3,7],[0,7,4],
           [1,5,6],[1,6,2],[4,7,6],[4,6,5]];
    var pos=[],uv=[];
    for(var i=0;i<f.length;i++)for(var k=0;k<3;k++){
      var p=v[f[i][k]];
      pos.push(p[0],p[1],p[2]); uv.push((p[0]+hw)/w,p[1]/Math.max(hN,hS));
    }
    var g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
    g.setAttribute('uv',new THREE.Float32BufferAttribute(uv,2));
    g.computeVertexNormals();
    var o=new THREE.Mesh(g,M.wallD);
    o.position.set(x,0,z); o.castShadow=true; o.receiveShadow=true;
    root.add(o); return o;
  }
  function pane(w,h,x,y,z,rotY){
    var f=box(w+0.14,h+0.14,0.08,x,y,z,M.trim,false,true);
    f.rotation.y=rotY||0;
    var p=new THREE.Mesh(new THREE.PlaneGeometry(w,h),M.glass);
    p.position.set(x,y,z); p.rotation.y=rotY||0; p.translateZ(0.05);
    root.add(p);
  }

  /* ---------- panels ---------- */
  function panelCluster(cfg){
    var outer=new THREE.Group();
    outer.rotation.y=-cfg.azimuth*D;
    var inner=new THREE.Group();
    inner.rotation.x=-cfg.tilt*D;
    outer.add(inner);
    var pw=1.65,ph=0.99,gap=0.045;
    var faceGeo=new THREE.PlaneGeometry(pw-0.07,ph-0.07);
    var frGeo=new THREE.BoxGeometry(pw,0.055,ph);
    var pm=M.pv[cfg.channel];
    var n=0,spanW=0,spanD=0;
    function put(px,pz){
      var fr=new THREE.Mesh(frGeo,M.frame);
      fr.position.set(px,0,pz); fr.castShadow=true; fr.receiveShadow=true;
      inner.add(fr);
      var fc=new THREE.Mesh(faceGeo,pm);
      fc.rotation.x=-Math.PI/2; fc.position.set(px,0.032,pz);
      inner.add(fc); n++;
    }
    if(cfg.pyramid){
      var rows=cfg.pyramid,nr=rows.length;
      for(var r=0;r<nr;r++){
        var nc=rows[r], pz=(r-(nr-1)/2)*(ph+gap);
        for(var i=0;i<nc;i++) put((i-(nc-1)/2)*(pw+gap),pz);
        spanW=Math.max(spanW,nc*(pw+gap));
      }
      spanD=nr*(ph+gap);
    }else{
      for(var ci=0;ci<cfg.cols;ci++)for(var rj=0;rj<cfg.rows;rj++)
        put((ci-(cfg.cols-1)/2)*(pw+gap),(rj-(cfg.rows-1)/2)*(ph+gap));
      spanW=cfg.cols*(pw+gap); spanD=cfg.rows*(ph+gap);
    }
    if(cfg.mount==='ground'){
      for(var s=-1;s<=1;s+=2){
        var rail=new THREE.Mesh(new THREE.BoxGeometry(spanW,0.08,0.08),M.frame);
        rail.position.set(0,-0.09,s*spanD*0.3); rail.castShadow=true;
        inner.add(rail);
        var leg=new THREE.Mesh(new THREE.BoxGeometry(0.13,cfg.pos[1]*2,0.13),M.frame);
        leg.position.set(s*spanW*0.42,-cfg.pos[1],0.2); leg.castShadow=true;
        inner.add(leg);
      }
    }
    outer.position.set(cfg.pos[0],cfg.pos[1],cfg.pos[2]);
    root.add(outer);
    cfg.count=n;
    caps3[cfg.channel]+=n*SITE.wattsPerPanel;
  }

  /* ---------- energy flows ---------- */
  function flow(pts2,color,chan){
    flows.push({curve:new THREE.CatmullRomCurve3(pts2.map(function(p){
      return new THREE.Vector3(p[0],p[1],p[2]);})),
      color:new THREE.Color(color),chan:chan,n:10});
  }
  function buildFlows(){
    var bus=[-1.15,2.4,-4.71];
    for(var i=0;i<SITE.clusters.length;i++){
      var cl=SITE.clusters[i], cp=ctrl[cl.channel];
      flow([[cl.pos[0],cl.pos[1]+1.0,cl.pos[2]],
            [(cl.pos[0]+cp[0])/2,Math.max(cl.pos[1],cp[1])+1.4,(cl.pos[2]+cp[2])/2],
            cp],C.solar,cl.channel);
    }
    flow([ctrl.mppt80,[-2.4,3.2,-1.0],bus],C.batt,'mppt80');
    flow([ctrl.west,  [-1.8,3.1,-1.0],bus],C.batt,'west');
    flow([ctrl.south, [-1.2,3.1,-1.0],bus],C.batt,'south');
    flow([bus,[0.0,3.0,0.5],[0.5,2.6,3.83]],C.batt,'dcM');
    flow([bus,[0.8,3.0,0.5],[1.3,2.6,3.83]],C.batt,'dcS');
    flow([[GEN.cx-1.6,1.7,GEN.cz-0.6],[0.5,3.2,8.0],[0.5,2.6,3.9]],C.gen,'gMep');
    flow([[GEN.cx+2.0,1.5,GEN.cz-0.6],[1.6,3.0,8.0],[1.3,2.6,3.9]],C.gen,'gKub');
    var total=0,i2;
    for(i2=0;i2<flows.length;i2++)total+=flows[i2].n;
    ptPos=new Float32Array(total*3); ptCol=new Float32Array(total*3);
    ptGeo=new THREE.BufferGeometry();
    ptGeo.setAttribute('position',new THREE.BufferAttribute(ptPos,3));
    ptGeo.setAttribute('color',new THREE.BufferAttribute(ptCol,3));
    pts=new THREE.Points(ptGeo,new THREE.PointsMaterial({size:0.42,
      vertexColors:true,transparent:true,opacity:.95}));
    root.add(pts);
    var o=0;
    for(i2=0;i2<flows.length;i2++){
      flows[i2].off=o; flows[i2].t=[];
      for(var j=0;j<flows[i2].n;j++)flows[i2].t.push(j/flows[i2].n);
      o+=flows[i2].n;
    }
  }

  /* ---------- solar position, corrected ---------- */
  function clockDate(){
    if(timeOverride===null)return new Date();
    var d=new Date();
    d.setHours(Math.floor(timeOverride),Math.round((timeOverride%1)*60),0,0);
    return d;
  }
  function sunPos(date){
    var start=new Date(date.getFullYear(),0,0);
    var doy=Math.floor((date-start)/86400000);
    var utcH=date.getUTCHours()+date.getUTCMinutes()/60+date.getUTCSeconds()/3600;
    var B=2*Math.PI*(doy-81)/364;
    var eot=9.87*Math.sin(2*B)-7.53*Math.cos(B)-1.5*Math.sin(B);
    var solar=utcH+SITE.lon/15+eot/60;
    var dec=23.45*D*Math.sin(2*Math.PI*(284+doy)/365);
    var Ha=(solar-12)*15*D, la=SITE.lat*D;
    var el=Math.asin(Math.sin(la)*Math.sin(dec)+
           Math.cos(la)*Math.cos(dec)*Math.cos(Ha));
    var az=Math.atan2(-Math.sin(Ha)*Math.cos(dec),
           Math.cos(la)*Math.sin(dec)-Math.sin(la)*Math.cos(dec)*Math.cos(Ha));
    return {el:el,az:(az+2*Math.PI)%(2*Math.PI),eot:eot,
            solar:((solar%24)+24)%24};
  }

  /* ---------- build ---------- */
  function build(){
    S=new THREE.Scene();
    S.background=new THREE.Color(0x7ba0c4);
    S.fog=new THREE.Fog(0x9fb2c4,90,220);
    buildMaterials();
    root=new THREE.Group(); root.rotation.y=SITE.bearing*D; S.add(root);

    hemi=new THREE.HemisphereLight(0xbcd6f0,0x6b5a41,0.55); S.add(hemi);
    sun=new THREE.DirectionalLight(0xfff2d8,2.2);
    sun.castShadow=true;
    sun.shadow.mapSize.set(1024,1024);      /* half the standalone's 2048 */
    var sc=sun.shadow.camera;
    sc.left=-28; sc.right=28; sc.top=28; sc.bottom=-28; sc.near=1; sc.far=140;
    sun.shadow.bias=-0.0007; sun.shadow.normalBias=0.02;
    S.add(sun);
    sunMesh=new THREE.Mesh(new THREE.SphereGeometry(1.4,14,10),
      new THREE.MeshBasicMaterial({color:0xfff0c0}));
    S.add(sunMesh);

    /* terrain */
    var g=new THREE.PlaneGeometry(180,180,40,40), p=g.attributes.position;
    for(var i=0;i<p.count;i++){
      var x=p.getX(i),y=p.getY(i),r=Math.sqrt(x*x+y*y);
      var h=1.4*Math.sin(x/26)*Math.cos(y/22)+0.7*Math.sin(x/11+1.3);
      p.setZ(i,h*Math.min(1,Math.max(0,(r-16)/16)));
    }
    g.computeVertexNormals();
    var gm=new THREE.Mesh(g,M.ground);
    gm.rotation.x=-Math.PI/2; gm.position.y=-0.05; gm.receiveShadow=true; S.add(gm);
    var sb=new THREE.Mesh(new THREE.PlaneGeometry(70,180),M.scrub);
    sb.rotation.x=-Math.PI/2; sb.position.set(56,0.01,0); sb.receiveShadow=true;
    S.add(sb);

    /* perimeter wall */
    var WX=11,WZ=21;
    box(WX*2,1.25,0.4,0,0.62,-WZ,M.wall);
    box(WX*2,1.25,0.4,0,0.62, WZ,M.wall);
    box(0.4,1.25,WZ*2,-WX,0.62,0,M.wall);
    box(0.4,1.25,WZ*2, WX,0.62,0,M.wall);
    for(var z=-WZ;z<=WZ;z+=5.25){
      box(0.5,1.5,0.5,-WX,0.75,z,M.wall);
      box(0.5,1.5,0.5, WX,0.75,z,M.wall);
    }

    buildHouse(); buildTerrace(); buildGen(); buildEquipment();
    placeClusters();
    for(var c=0;c<SITE.clusters.length;c++)panelCluster(SITE.clusters[c]);
    buildFlows();

    CAM=new THREE.PerspectiveCamera(42,W/H,0.5,400);
    target=new THREE.Vector3(0,3,3);
    fitView(); place();
  }

  function buildHouse(){
    var W2=HOUSE.w,D2=HOUSE.d,WALL=HOUSE.wall;
    box(W2,WALL,D2,0,WALL/2,0,M.wall);
    /* second roof, all the way round, as a mitred ring */
    var sy=3.55,sd=1.2,sp=10*D;
    skirtRing(W2/2,D2/2,W2/2+sd,D2/2+sd,sy,sy-Math.tan(sp)*sd);
    box(W2+2*sd+0.2,0.18,0.14,0,sy-Math.tan(sp)*sd-0.1, D2/2+sd,M.trim,false,true);
    box(W2+2*sd+0.2,0.18,0.14,0,sy-Math.tan(sp)*sd-0.1,-(D2/2+sd),M.trim,false,true);
    box(0.14,0.18,D2+2*sd+0.2, W2/2+sd,sy-Math.tan(sp)*sd-0.1,0,M.trim,false,true);
    box(0.14,0.18,D2+2*sd+0.2,-(W2/2+sd),sy-Math.tan(sp)*sd-0.1,0,M.trim,false,true);
    var pe=W2/2+sd-0.15;
    for(var k=-1;k<=1;k+=2)for(var m2=-1;m2<=1;m2+=2)
      box(0.2,3.2,0.2,k*pe,1.6,m2*pe,M.post);
    for(var k2=-1;k2<=1;k2+=2){
      box(0.2,3.2,0.2,k2*pe,1.6,0,M.post);
      box(0.2,3.2,0.2,0,1.6,k2*pe,M.post);
    }
    /* main pyramid roof */
    pyramidRoof(HOUSE.dx*2,HOUSE.dz*2,HOUSE.rise,HOUSE.ridge,0,WALL);
    box(2.4,0.7,2.4,0,WALL+HOUSE.rise+0.35,0,M.roof);
    box(2.7,0.12,2.7,0,WALL+HOUSE.rise+0.76,0,M.roof);
    var wx=W2/2+0.02,wz=D2/2+0.02;
    pane(1.15,1.35,-wx,4.65,-1.9,Math.PI/2);
    pane(1.15,1.35,-wx,4.65, 1.9,Math.PI/2);
    pane(1.15,1.35, wx,4.65,-1.9,Math.PI/2);
    pane(1.15,1.35, wx,4.65, 1.9,Math.PI/2);
    pane(1.25,1.35,-1.9,4.65,wz,0);
    pane(1.25,1.35, 1.9,4.65,wz,0);
    pane(1.25,1.35, 0,4.65,-wz,0);
    pane(1.05,1.05,-wx,1.75,-1.4,Math.PI/2);
    pane(1.05,1.05,-wx,1.75, 1.4,Math.PI/2);
    box(1.0,2.1,0.12,0,1.05,wz+0.04,M.post,false,true);
  }

  function buildTerrace(){
    var TW=TERRACE.w,TD=TERRACE.d,cx=TERRACE.cx,cz=TERRACE.cz;
    var pr=HOUSE.pitch*D;
    box(TW,0.3,TD,cx,TERRACE.deck,cz,M.deck,false,true);
    var yMid=HOUSE.wall-(cz-HOUSE.dz)*Math.tan(pr);
    var tr=box(TW+0.5,0.13,TD+0.6,cx,yMid,cz,M.roof);
    tr.rotation.x=pr;
    var yOut=HOUSE.wall-(cz+TD/2-HOUSE.dz)*Math.tan(pr);
    box(TW+0.6,0.2,0.16,cx,yOut-0.12,cz+TD/2+0.3,M.trim,false,true);
    for(var h=0;h<2;h++){
      var y=TERRACE.deck+0.55+h*0.62;
      box(TW,0.07,0.07,cx,y,cz+TD/2,M.steel);
      box(0.07,0.07,TD,cx-TW/2,y,cz,M.steel);
      box(0.07,0.07,TD,cx+TW/2,y,cz,M.steel);
    }
    for(var x=cx-TW/2;x<=cx+TW/2+0.01;x+=1.4)
      box(0.07,1.25,0.07,x,TERRACE.deck+0.77,cz+TD/2,M.steel);
    for(var q=-1;q<=1;q+=2){
      box(0.18,TERRACE.deck,0.18,cx+q*TW/2,TERRACE.deck/2,cz+TD/2,M.steel);
      box(0.18,yOut-TERRACE.deck-0.3,0.18,cx+q*TW/2,
          (yOut+TERRACE.deck)/2,cz+TD/2,M.steel);
    }
  }

  function buildGen(){
    var pr=GEN.pitch*D, oh=0.35, dpt=GEN.d+2*oh;
    shedBox(GEN.w,GEN.d,GEN.wallN,GEN.wallS,GEN.cx,GEN.cz);
    var r=box(GEN.w+2*oh,0.14,dpt,GEN.cx,GEN.wall+0.07/Math.cos(pr),GEN.cz,M.roof);
    r.rotation.x=pr;
    var yOut=GEN.wall-Math.tan(pr)*dpt/2;
    box(GEN.w+2*oh+0.2,0.2,0.16,GEN.cx,yOut-0.11,GEN.cz+dpt/2,M.trim,false,true);
    box(2.8,2.3,0.1,GEN.cx-0.9,1.15,GEN.cz-GEN.d/2-0.05,M.steel,false,true);
    pane(0.8,0.7,GEN.cx+2.6,2.5,GEN.cz-GEN.d/2-0.05,0);
    box(3.6,2.4,3.0,7.6,1.2,9.5,M.red);
    box(4.0,0.16,3.4,7.6,2.48,9.5,M.cap);
    box(0.12,4.8,0.12,8.6,2.4,4.5,M.frame);
    box(0.8,0.6,0.6,8.6,5.0,4.5,M.cap);
  }

  function buildEquipment(){
    var z=HOUSE.d/2+0.04;
    ctrl.mppt80=[-3.3,2.4,z]; ctrl.west=[-2.4,2.3,z]; ctrl.south=[-1.7,2.3,z];
    box(0.7,1.2,0.34,-3.3,1.7,z,M.case);
    box(0.6,1.05,0.3,-2.4,1.62,z,M.case);
    box(0.6,1.05,0.3,-1.7,1.62,z,M.case);
    box(1.15,1.45,0.4,-0.7,1.78,z,M.case);
    invM=box(0.7,1.3,0.36,0.5,1.7,z,M.case);
    invS=box(0.7,1.3,0.36,1.3,1.7,z,M.case);
    box(0.65,1.15,0.34,2.2,1.62,z,M.case);
    for(var b=0;b<3;b++)for(var s=0;s<4;s++)
      battModules.push(box(1.1,0.34,1.0,-2.4+b*1.25,0.3+s*0.37,
                           -HOUSE.d/2-0.9,M.dark));
    genMep=box(2.4,1.4,1.15,GEN.cx-1.6,0.7,GEN.cz-0.6,M.dark);
    genKub=box(1.7,1.1,0.95,GEN.cx+2.0,0.55,GEN.cz-0.6,M.dark);
  }

  /* ---------- camera ---------- */
  function fitView(){
    root.updateMatrixWorld(true);
    var bb=new THREE.Box3().setFromObject(root);
    var sph=bb.getBoundingSphere(new THREE.Sphere());
    target.copy(sph.center); fitR=sph.radius;
    var vF=CAM.fov*D, hF=2*Math.atan(Math.tan(vF/2)*CAM.aspect);
    dist=Math.max(18,Math.min(160,fitR/Math.sin(Math.min(vF,hF)/2)*1.02));
  }
  function place(){
    CAM.position.set(target.x+dist*Math.cos(pitch)*Math.sin(yaw),
                     target.y+dist*Math.sin(pitch),
                     target.z+dist*Math.cos(pitch)*Math.cos(yaw));
    CAM.lookAt(target);
  }

  /* ---------- live state ---------- */
  function apply(){
    var ch=['mppt80','south','west'];
    for(var i=0;i<ch.length;i++){
      var k=ch[i], f=Math.max(0,Math.min(1,(live[k]||0)/(caps3[k]||1)));
      var m=M.pv[k];
      m.color.copy(new THREE.Color(0xffffff)
        .lerp(new THREE.Color(C.panelHot),f*0.55));
      m.emissive.copy(new THREE.Color(C.solar).multiplyScalar(0.42*f));
    }
    var lit=Math.round(battModules.length*Math.min(100,live.soc)/100);
    var hue=live.soc<25?0xff5d5d:(live.soc<50?0xffb020:C.batt);
    for(var b=0;b<battModules.length;b++){
      var on=b<lit;
      if(battModules[b].material===M.dark){
        battModules[b].material=M.dark.clone();     /* first touch only */
      }
      battModules[b].material.color.setHex(on?hue:C.dark);
      battModules[b].material.emissive.setHex(on?hue:0x000000)
        .multiplyScalar(on?0.3:0);
    }
    function tint(o,val,base,ref){
      if(o.material===M.case||o.material===M.dark)o.material=o.material.clone();
      var f=Math.min(1,val/ref);
      o.material.color.copy(new THREE.Color(C.box).lerp(new THREE.Color(base),f*0.7));
      o.material.emissive.copy(new THREE.Color(base).multiplyScalar(0.38*f));
    }
    tint(invM,live.acM,C.ac,6000); tint(invS,live.acS,C.ac,6000);
    tint(genMep,live.gMep,C.gen,100); tint(genKub,live.gKub,C.gen,100);
  }
  function rate(chan){
    if(chan==='dcM')return live.acM/6000;
    if(chan==='dcS')return live.acS/6000;
    if(chan==='gMep')return live.gMep/100;
    if(chan==='gKub')return live.gKub/100;
    return (live[chan]||0)/(caps3[chan]||1);
  }
  function actLevel(a){
    a=(a===undefined||a===null)?255:+a;
    if(a===9)return 1;
    if([0,1,2,3,4,11].indexOf(a)>=0)return 0.4;
    if([5,6,7,8].indexOf(a)>=0)return 0.55;
    return 0;
  }

  /* ---------- loop ---------- */
  function frame(now){
    if(!running)return;
    raf=requestAnimationFrame(frame);
    if(now-last<FRAME)return;
    var dt=Math.min(0.2,(now-(tPrev||now))/1000); tPrev=now; last=now;

    var sp=sunPos(clockDate()), night=sp.el<0, elc=Math.max(sp.el,0.02);
    var r=72;
    var vx=r*Math.cos(elc)*Math.sin(sp.az),
        vy=r*Math.sin(elc),
        vz=-r*Math.cos(elc)*Math.cos(sp.az);
    sun.position.set(vx,vy,vz);
    sunMesh.position.set(vx,vy,vz); sunMesh.visible=!night;
    var f=Math.sin(elc);
    sun.intensity=night?0.10:(0.45+2.0*f);
    hemi.intensity=night?0.26:(0.3+0.4*f);
    S.fog.color.setHSL(0.58-0.12*f,0.2+0.1*f,night?0.10:(0.3+0.4*f));
    S.background.copy(S.fog.color);

    var v=new THREE.Vector3();
    for(var i=0;i<flows.length;i++){
      var fl=flows[i], rr=Math.max(0,Math.min(1,rate(fl.chan)));
      var spd=0.05+rr*0.45, a=rr<0.012?0:1;
      for(var j=0;j<fl.n;j++){
        fl.t[j]+=spd*dt; if(fl.t[j]>1)fl.t[j]-=1;
        fl.curve.getPoint(fl.t[j],v);
        var o=(fl.off+j)*3;
        ptPos[o]=v.x; ptPos[o+1]=v.y; ptPos[o+2]=v.z;
        ptCol[o]=fl.color.r*a; ptCol[o+1]=fl.color.g*a; ptCol[o+2]=fl.color.b*a;
      }
    }
    ptGeo.attributes.position.needsUpdate=true;
    ptGeo.attributes.color.needsUpdate=true;
    R.render(S,CAM);
  }

  /* ---------- input ---------- */
  function bind(cv2){
    function dn(e){drag=true;lx=e.touches?e.touches[0].clientX:e.clientX;
      ly=e.touches?e.touches[0].clientY:e.clientY;}
    function mv(e){
      if(!drag)return;
      var cx=e.touches?e.touches[0].clientX:e.clientX;
      var cy=e.touches?e.touches[0].clientY:e.clientY;
      yaw-=(cx-lx)*0.006; pitch+=(cy-ly)*0.005;
      pitch=Math.max(0.08,Math.min(1.42,pitch)); lx=cx; ly=cy; place();
      if(e.touches)e.preventDefault();
    }
    function up(){drag=false;}
    cv2.addEventListener('mousedown',dn); cv2.addEventListener('touchstart',dn,{passive:true});
    window.addEventListener('mousemove',mv); cv2.addEventListener('touchmove',mv,{passive:false});
    window.addEventListener('mouseup',up); window.addEventListener('touchend',up);
    cv2.addEventListener('wheel',function(e){
      dist=Math.max(fitR*0.4,Math.min(fitR*4,dist+e.deltaY*0.05));
      place(); e.preventDefault();
    },{passive:false});
  }

  /* ---------- public ---------- */
  function init(el){
    host=el; W=host.clientWidth||640; H=host.clientHeight||430;
    R=new THREE.WebGLRenderer({antialias:false,alpha:false,
      powerPreference:'low-power'});
    R.setPixelRatio(Math.min(1.5,window.devicePixelRatio||1));
    R.setSize(W,H);
    R.shadowMap.enabled=shadowsOn;
    R.shadowMap.type=THREE.PCFSoftShadowMap;
    R.outputEncoding=THREE.sRGBEncoding;
    R.toneMapping=THREE.ACESFilmicToneMapping;
    R.toneMappingExposure=1.05;
    host.appendChild(R.domElement);
    build(); bind(R.domElement); apply(); ready=true;
    window.addEventListener('resize',function(){
      if(!ready||!host.clientWidth)return;
      W=host.clientWidth; H=host.clientHeight;
      CAM.aspect=W/H; CAM.updateProjectionMatrix(); R.setSize(W,H);
      fitView(); place();
    });
  }
  function start(){if(!ready||running)return;running=true;tPrev=0;last=0;
    raf=requestAnimationFrame(frame);}
  function stop(){running=false;if(raf)cancelAnimationFrame(raf);raf=null;}
  function update(d){
    live.mppt80=+d.mppt80PVPower||0;
    live.south=+d.southArrayPVPower||0;
    live.west=+d.westArrayPVPower||0;
    live.soc=+d.batterySOC||0;
    live.acM=+d.acPower1||0; live.acS=+d.acPower2||0;
    live.batt=(d.battPower===undefined||d.battPower===null)?null:+d.battPower;
    live.gMep=actLevel(d.mep803aAction)*(+d.mepChargeRateLive||0);
    live.gKub=actLevel(d.kubotaAction)*(+d.kubotaChargeRateLive||0);
    if(ready)apply();
  }
  return {
    init:init,start:start,stop:stop,update:update,
    caps:function(){return caps3;},
    setTime:function(h){timeOverride=(h===null||h===undefined)?null:h;},
    sunInfo:function(){
      var sp=sunPos(clockDate());
      return {elevation:sp.el/D,azimuth:sp.az/D,night:sp.el<0,
              eot:sp.eot,solar:sp.solar,override:timeOverride};
    },
    setShadows:function(on){
      shadowsOn=!!on;
      if(R){R.shadowMap.enabled=shadowsOn; sun.castShadow=shadowsOn;
        S.traverse(function(o){if(o.material)o.material.needsUpdate=true;});}
    },
    shadowsOn:function(){return shadowsOn;},
    resetView:function(){yaw=-0.7;pitch=0.44;fitView();place();},
    isReady:function(){return ready;},
    isRunning:function(){return running;}
  };
})();
</script>
</body>
</html>"""

REGISTERS_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>Modbus Register Tool</title>
<style>
body{font-family:sans-serif;background:linear-gradient(135deg,#1a252f,#2c3e50);color:#ecf0f1;margin:0;padding:15px;}
.container{background:#3b5167;border-radius:8px;padding:15px;max-width:900px;margin:0 auto;}
h2{color:#82e0aa;margin:0 0 15px 0;text-align:center;}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:15px;}
@media(max-width:700px){.grid{grid-template-columns:1fr;}}
.panel{background:#4a6582;border-radius:6px;padding:12px;}
.panel h3{color:#f39c12;margin:0 0 10px 0;}
.form-row{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap;}
.form-row label{color:#b0c4de;width:60px;display:flex;align-items:center;}
.form-row input,.form-row select{flex:1;padding:6px;border-radius:4px;border:1px solid #5d7a96;background:#3b5167;color:#ecf0f1;}
.btn{padding:8px 12px;border-radius:4px;border:none;cursor:pointer;font-weight:bold;margin:2px;}
.btn-read{background:#3498db;color:white;}
.btn-write{background:#e67e22;color:white;}
.btn-batch{background:#9b59b6;color:white;}
.log{background:#2c3e50;border-radius:4px;padding:10px;height:300px;overflow-y:auto;font-family:monospace;font-size:0.8em;margin-top:15px;}
.back-link{display:block;text-align:center;margin-top:15px;color:#82e0aa;}
</style>
</head>
<body>
<div class='container'>
<h2>🔧 Modbus Register Tool</h2>
<div class='grid'>
  <div class='panel'>
    <h3>Read Register</h3>
    <div class='form-row'><label>Slave ID</label><input type='number' id='readId' value='10'></div>
    <div class='form-row'><label>Port</label><input type='number' id='readPort' value='503'></div>
    <div class='form-row'><label>Address</label><input type='text' id='readAddr' value='0x0046'></div>
    <div class='form-row'><label>Type</label><select id='readType'><option value='u16'>uint16</option><option value='s16'>sint16</option><option value='u32'>uint32</option><option value='s32'>sint32</option></select></div>
    <button class='btn btn-read' onclick='readReg()'>READ</button>
    <button class='btn btn-batch' onclick='readTransferRegs()'>Transfer/Ramp</button>
    <button class='btn btn-batch' onclick='readAGSRegs()'>AGS Registers</button>
  </div>
  <div class='panel'>
    <h3>Write Register</h3>
    <div class='form-row'><label>Slave ID</label><input type='number' id='writeId' value='10'></div>
    <div class='form-row'><label>Port</label><input type='number' id='writePort' value='503'></div>
    <div class='form-row'><label>Address</label><input type='text' id='writeAddr' value='0x016F'></div>
    <div class='form-row'><label>Value</label><input type='number' id='writeValue' value='100'></div>
    <div class='form-row'><label>Type</label><select id='writeType'><option value='u16'>uint16</option><option value='s32'>sint32</option></select></div>
    <button class='btn btn-write' onclick='writeReg()'>WRITE</button>
  </div>
</div>
<div class='log' id='log'></div>
<a href='/' class='back-link'>\u2190 Back to Dashboard</a>
</div>
<script>
function log(msg,type='info'){
  const d=document.getElementById('log');const e=document.createElement('div');
  e.style.color=type==='success'?'#27ae60':type==='error'?'#e74c3c':'#3498db';
  e.textContent=new Date().toLocaleTimeString()+' '+msg;d.appendChild(e);d.scrollTop=d.scrollHeight;
}
function parseAddr(v){return v.startsWith('0x')?parseInt(v,16):parseInt(v,10);}
async function readReg(){
  const id=document.getElementById('readId').value,port=document.getElementById('readPort').value;
  const addr=parseAddr(document.getElementById('readAddr').value),type=document.getElementById('readType').value;
  log('Reading ID='+id+' Addr=0x'+addr.toString(16).toUpperCase());
  try{const r=await fetch('/readreg?id='+id+'&port='+port+'&addr='+addr+'&type='+type);const d=await r.json();
    if(d.success)log('Value: '+d.value+' (0x'+d.hex+')','success');else log('Failed: '+(d.error||'Unknown'),'error');
  }catch(e){log('Error: '+e,'error');}
}
async function writeReg(){
  const id=document.getElementById('writeId').value,port=document.getElementById('writePort').value;
  const addr=parseAddr(document.getElementById('writeAddr').value),val=document.getElementById('writeValue').value;
  const type=document.getElementById('writeType').value;
  log('Writing ID='+id+' Addr=0x'+addr.toString(16).toUpperCase()+' Value='+val);
  try{const r=await fetch('/writereg?id='+id+'&port='+port+'&addr='+addr+'&value='+val+'&type='+type);const d=await r.json();
    if(d.success)log('Write OK','success');else log('Failed: '+(d.error||'Unknown'),'error');
  }catch(e){log('Error: '+e,'error');}
}
async function readTransferRegs(){
  log('=== Transfer/Ramp Registers ===');
  try{const r=await fetch('/readtransfer');const d=await r.json();
    if(d.success){for(const dev of d.data){log('--- '+dev.dev+' (ID:'+dev.id+') ---');for(const reg of dev.regs){if(reg.ok)log('  '+reg.n+': '+reg.v,'success');else log('  '+reg.n+': FAILED','error');}}}
  }catch(e){log('Error: '+e,'error');}
}
async function readAGSRegs(){
  log('=== AGS Registers ===');
  try{const r=await fetch('/readags');const d=await r.json();
    if(d.success){for(const dev of d.data){log('--- '+dev.dev+' (ID:'+dev.id+') ---');for(const reg of dev.regs){if(reg.ok)log('  '+reg.n+': '+reg.v,'success');else log('  '+reg.n+': FAILED','error');}}}
  }catch(e){log('Error: '+e,'error');}
}
</script>
</body>
</html>"""

@app.route('/')
def index():
    # Static HTML - bypass Jinja so CSS/JS braces are never interpreted
    return DASHBOARD_HTML

@app.route('/vendor/<path:name>')
def vendor_file(name):
    """Serve locally vendored JS so the 3D view works without an internet route."""
    if name not in ('three.min.js',):
        return jsonify({'error': 'not found'}), 404
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor', name)
    if not os.path.isfile(path):
        return jsonify({'error': 'not vendored'}), 404
    return send_file(path, mimetype='application/javascript', max_age=604800)


@app.route('/data')
def data_endpoint():
    with data_lock:
        response = dict(system_data)
    with config_lock:
        response['autoGenEnabled'] = config.get('autoGenEnabled', True)
    return jsonify(response)

@app.route('/config')
def config_endpoint():
    global config
    changed = False

    if 'autoGenEnabled' in request.args:
        val = request.args.get('autoGenEnabled')
        with config_lock:
            config['autoGenEnabled'] = (val == '1' or val.lower() == 'true')
        changed = True
        log_event(f"Auto-gen {'enabled' if config['autoGenEnabled'] else 'disabled'}")

    for key in ['startVoltage', 'stopVoltage', 'chargeRate', 'maxRuntime', 'cooldown']:
        param = f'mep.{key}'
        if param in request.args:
            with config_lock:
                if key == 'startVoltage':
                    config['mep803a'][key] = max(45.0, min(60.0, float(request.args.get(param))))
                elif key == 'stopVoltage':
                    val = max(48.0, min(63.0, float(request.args.get(param))))
                    if val <= config['mep803a']['startVoltage'] + 1.5:
                        val = config['mep803a']['startVoltage'] + 2.0
                    config['mep803a'][key] = min(63.0, val)
                elif key == 'chargeRate':
                    config['mep803a'][key] = max(0, min(100, int(request.args.get(param))))
                else:
                    config['mep803a'][key] = int(request.args.get(param))
            changed = True

    for key in ['startVoltage', 'stopVoltage', 'chargeRate', 'maxRuntime', 'cooldown']:
        param = f'kub.{key}'
        if param in request.args:
            with config_lock:
                if key == 'startVoltage':
                    config['kubota'][key] = max(45.0, min(60.0, float(request.args.get(param))))
                elif key == 'stopVoltage':
                    val = max(48.0, min(63.0, float(request.args.get(param))))
                    if val <= config['kubota']['startVoltage'] + 1.5:
                        val = config['kubota']['startVoltage'] + 2.0
                    config['kubota'][key] = min(63.0, val)
                elif key == 'chargeRate':
                    config['kubota'][key] = max(0, min(100, int(request.args.get(param))))
                else:
                    config['kubota'][key] = int(request.args.get(param))
            changed = True

    for key in ['stepDelay', 'zeroHoldTime']:
        param = f'ramp.{key}'
        if param in request.args:
            with config_lock:
                config['rampDown'][key] = int(request.args.get(param))
            changed = True

    if 'tg.enabled' in request.args:
        val = request.args.get('tg.enabled')
        with config_lock:
            config['telegram']['enabled'] = (val == '1' or val.lower() == 'true')
        changed = True
        log_event(f"Telegram {'enabled' if config['telegram']['enabled'] else 'disabled'}")

    if 'tg.token' in request.args:
        with config_lock:
            config['telegram']['token'] = request.args.get('tg.token', '').strip()
        changed = True

    if 'tg.chatId' in request.args:
        with config_lock:
            config['telegram']['chatId'] = request.args.get('tg.chatId', '').strip()
        changed = True

    if changed:
        save_config()
        log_event("Config updated")

    with config_lock:
        cfg_copy = copy.deepcopy(config)
    with auto_gen_lock:
        events = list(auto_gen_state["events"][-50:])

    return jsonify({"config": cfg_copy, "events": events})

# Solar agent on the KAMRUI. Read-only proxy so the dashboard can show an
# agent status badge without the browser reaching across the LAN itself.
AGENT_PLAN_URL = "http://192.168.3.152:8090/plan"
AGENT_PLAN_TIMEOUT = 3
AGENT_ASK_URL = "http://192.168.3.152:8090/ask"
AGENT_ASK_TIMEOUT = 90


@app.route('/agent/plan')
def agent_plan_endpoint():
    """Proxy the agent's /plan. Never fails hard: the dashboard must render
    whether or not the agent is running."""
    try:
        resp = http_requests.get(AGENT_PLAN_URL, timeout=AGENT_PLAN_TIMEOUT)
        if resp.status_code != 200:
            return jsonify({"online": False})
        return jsonify(resp.json())
    except Exception as e:
        logger.debug(f"agent plan unavailable: {e}")
        return jsonify({"online": False})


@app.route('/agent/ask', methods=['POST'])
def agent_ask_endpoint():
    """Forward a question to the agent. Never fails hard, same as /agent/plan.

    The agent answers in plain text and may take most of a minute over it: it
    calls a tool, waits on a local model, and sometimes calls another. Ninety
    seconds is the model's ceiling, not an expectation.

    A question can end in the agent proposing a threshold write, which the
    guard then judges, so on the VPS this path sits behind the cookie gate
    while /agent/plan stays open.
    """
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({"online": True, "reply": ""}), 400
    try:
        resp = http_requests.post(AGENT_ASK_URL, json={"text": text},
                                  timeout=AGENT_ASK_TIMEOUT)
        if resp.status_code != 200:
            logger.debug(f"agent ask returned {resp.status_code}")
            return jsonify({"online": False})
        return jsonify({"online": True, "reply": resp.text})
    except Exception as e:
        logger.debug(f"agent ask unavailable: {e}")
        return jsonify({"online": False})

@app.route('/testtelegram')
def test_telegram_endpoint():
    success, message = test_telegram()
    return jsonify({"success": success, "message": message})

@app.route('/stopgen')
def stop_gen_endpoint():
    slave_id = request.args.get('id', type=int)
    if slave_id is None:
        return "Missing parameters", 400
    if slave_id == AGS_MEP803A_ID:
        threading.Thread(target=stop_generator, args=("mep803a", True), daemon=True).start()
        return "MEP-803A stop initiated", 200
    elif slave_id == AGS_KUBOTA_ID:
        threading.Thread(target=stop_generator, args=("kubota", True), daemon=True).start()
        return "Kubota stop initiated", 200
    return "Invalid slave ID", 400

@app.route('/registers')
def registers_page():
    return render_template_string(REGISTERS_HTML)

@app.route('/readreg')
def read_reg_endpoint():
    try:
        slave_id = request.args.get('id', type=int)
        port = request.args.get('port', default=503, type=int)
        addr = request.args.get('addr', type=int)
        reg_type = request.args.get('type', default='u16')
        if slave_id is None or addr is None:
            return jsonify({"success": False, "error": "Missing parameters"})
        result = None
        if reg_type == 'u16':
            result = modbus.read_holding_register_16(MODBUS_HOST, port, slave_id, addr)
        elif reg_type == 's16':
            result = modbus.read_holding_register_16s(MODBUS_HOST, port, slave_id, addr)
        elif reg_type == 'u32':
            result = modbus.read_holding_register_32(MODBUS_HOST, port, slave_id, addr)
        elif reg_type == 's32':
            result = modbus.read_holding_register_32s(MODBUS_HOST, port, slave_id, addr)
        if result is not None:
            hex_str = f"{result & 0xFFFF:04X}" if reg_type in ['u16','s16'] else f"{result & 0xFFFFFFFF:08X}"
            return jsonify({"success": True, "value": result, "hex": hex_str})
        return jsonify({"success": False, "error": "Read failed"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/writereg')
def write_reg_endpoint():
    try:
        slave_id = request.args.get('id', type=int)
        port = request.args.get('port', default=503, type=int)
        addr = request.args.get('addr', type=int)
        value = request.args.get('value', type=int)
        reg_type = request.args.get('type', default='u16')
        if slave_id is None or addr is None or value is None:
            return jsonify({"success": False, "error": "Missing parameters"})
        success = False
        if reg_type == 'u16':
            success = modbus.write_single_register_16(MODBUS_HOST, port, slave_id, addr, value)
        elif reg_type == 's32':
            high = (value >> 16) & 0xFFFF
            low = value & 0xFFFF
            success = modbus.write_single_register_16(MODBUS_HOST, port, slave_id, addr, high)
            if success:
                success = modbus.write_single_register_16(MODBUS_HOST, port, slave_id, addr + 1, low)
        return jsonify({"success": success})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/readtransfer')
def read_transfer_endpoint():
    try:
        devices = [{"id": 10, "dev": "XW_Master"}, {"id": 12, "dev": "XW_Slave"}, {"id": 11, "dev": "XW_5548"}]
        registers = [
            {"addr": 0x00C0, "name": "SwitchState"}, {"addr": 0x01F5, "name": "AC1Delay"},
            {"addr": 0x01F6, "name": "AC2Delay"}, {"addr": 0x016F, "name": "MaxCharge"},
            {"addr": 0x0164, "name": "ChargerEn"}
        ]
        result = {"success": True, "data": []}
        for dev in devices:
            dev_data = {"dev": dev["dev"], "id": dev["id"], "regs": []}
            for reg in registers:
                val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, dev["id"], reg["addr"])
                dev_data["regs"].append({"n": reg["name"], "a": reg["addr"], "ok": val is not None, "v": val or 0})
            result["data"].append(dev_data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/readags')
def read_ags_endpoint():
    try:
        devices = [{"id": 51, "dev": "AGS_MEP803A"}, {"id": 50, "dev": "AGS_Kubota"}]
        registers = [
            {"addr": 0x0054, "name": "AutoStopDCV"}, {"addr": 0x0056, "name": "AutoStopSOC"},
            {"addr": 0x0059, "name": "StopAbsorp"}, {"addr": 0x005A, "name": "StopFloat"},
            {"addr": 0x006B, "name": "CoolDown"}, {"addr": 0x006C, "name": "SpinDown"}
        ]
        result = {"success": True, "data": []}
        for dev in devices:
            dev_data = {"dev": dev["dev"], "id": dev["id"], "regs": []}
            for reg in registers:
                val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, dev["id"], reg["addr"])
                dev_data["regs"].append({"n": reg["name"], "ok": val is not None, "v": val or 0})
            result["data"].append(dev_data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/setgen')
def set_gen_endpoint():
    slave_id = request.args.get('id', type=int)
    state = request.args.get('state', type=int)
    if slave_id is None or state is None:
        return "Missing parameters", 400
    if slave_id not in [AGS_MEP803A_ID, AGS_KUBOTA_ID]:
        return "Invalid slave ID", 400
    if state not in [0, 1, 2]:
        return "Invalid state", 400
    if state == 1:
        gen = "mep803a" if slave_id == AGS_MEP803A_ID else "kubota"
        threading.Thread(target=start_generator, args=(gen,), daemon=True).start()
        return f"{'MEP-803A' if slave_id == AGS_MEP803A_ID else 'Kubota'} start initiated", 200
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_GENERATOR_MODE, state)
    if success:
        gen_name = "MEP-803A" if slave_id == AGS_MEP803A_ID else "Kubota"
        log_event(f"{gen_name} → {('OFF','AUTO')[state==2]}")
        return "OK", 200
    return "Write failed", 500

@app.route('/setmpptmode')
def set_mppt_endpoint():
    slave_id = request.args.get('id', type=int)
    mode = request.args.get('mode', type=int)
    if slave_id is None or mode is None:
        return "Missing parameters", 400
    if slave_id not in [MPPT_80_ID, SOUTH_ARRAY_ID, WEST_ARRAY_ID]:
        return "Invalid slave ID", 400
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_CHARGE_MODE_FORCE, mode)
    return ("OK", 200) if success else ("Failed", 500)

# --- V2.4: AC Diagnostic Endpoints ---
@app.route('/acdiag')
def acdiag_endpoint():
    """Single AC diagnostic snapshot. Readable anytime via browser or curl."""
    reading = ac_diag_snapshot()
    threading.Thread(target=ac_diag_save, args=(reading,), daemon=True).start()
    return jsonify(reading)

@app.route('/acdiag/log')
def acdiag_log_endpoint():
    """Return last N log entries. Usage: /acdiag/log?n=100"""
    try:
        n = min(int(request.args.get('n', 100)), 2000)
    except Exception:
        n = 100
    with ac_diag_lock:
        if os.path.exists(AC_DIAG_LOG_FILE):
            try:
                with open(AC_DIAG_LOG_FILE, 'r') as f:
                    entries = json.load(f)
                return jsonify(entries[-n:])
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify([])

@app.route('/acdiag/log/clear')
def acdiag_log_clear_endpoint():
    """Clear the AC diagnostic log file."""
    with ac_diag_lock:
        with open(AC_DIAG_LOG_FILE, 'w') as f:
            json.dump([], f)
    return jsonify({"status": "cleared"})

@app.route('/acdiag/stream')
def acdiag_stream_endpoint():
    """
    Take N readings at interval_ms apart and return all at once.
    Usage: /acdiag/stream?n=20&interval=500
    Run BEFORE a generator stop to capture the transition.
    All readings saved to log file automatically.
    """
    try:
        n = min(int(request.args.get('n', 20)), 60)
        interval = max(int(request.args.get('interval', 500)), 200) / 1000.0
    except Exception:
        n = 20
        interval = 0.5

    readings = []
    for _ in range(n):
        r = ac_diag_snapshot()
        readings.append(r)
        threading.Thread(target=ac_diag_save, args=(r,), daemon=True).start()
        time.sleep(interval)

    return jsonify(readings)

# --- Initialize ---
logger.info("=" * 50)
logger.info("Solar Dashboard V2.8 Starting...")
load_config()

poll_thread = threading.Thread(target=poll_modbus, daemon=True)
poll_thread.start()
logger.info("Modbus polling started")
with config_lock:
    logger.info(f"Auto-gen: {'ENABLED' if config['autoGenEnabled'] else 'DISABLED'}")
    logger.info(f"Telegram: {'ENABLED' if config['telegram']['enabled'] else 'DISABLED'}")
logger.info("AC Diagnostic endpoints ready: /acdiag /acdiag/stream /acdiag/log")
logger.info("=" * 50)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
