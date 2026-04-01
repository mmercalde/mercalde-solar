#!/usr/bin/env python3
"""
Solar Dashboard - Pi 5 Flask Application V2.3
Full autonomous control with persistent settings

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
"""

import os
import json
import time
import threading
import logging
import copy
import requests as http_requests
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string

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
REG_CHARGE_MODE_FORCE = 0x00AA
REG_CHARGER_ENABLE = 0x0164
REG_MAX_CHARGE_RATE = 0x016F
REG_OPERATING_MODE = 0x0166
REG_FORCE_CHARGER_STATE = 0x0165
REG_CHARGE_DC_POWER = 0x005E

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
    "lastUpdate": "00:00:00", "pollErrors": 0,
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

    # Battery low alert
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

            # AGS Units — tracked separately for offline detection
            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, AGS_MEP803A_ID, REG_GENERATOR_MODE)
            new_data["mep803aMode"] = val if val is not None else 0
            new_data["mepAgsOnline"] = val is not None
            if val is None:
                errors += 1
                mep_ok = False

            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, AGS_KUBOTA_ID, REG_GENERATOR_MODE)
            new_data["kubotaMode"] = val if val is not None else 0
            new_data["kubotaAgsOnline"] = val is not None
            if val is None:
                errors += 1
                kubota_ok = False

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

            elapsed = int(time.time() - start_time)
            new_data["lastUpdate"] = f"{(elapsed//3600)%24:02d}:{(elapsed//60)%60:02d}:{elapsed%60:02d}"
            new_data["pollErrors"] = errors

            with data_lock:
                system_data.update(new_data)

            # V2.3: AGS offline detection
            check_ags_status(mep_ok, kubota_ok)

            # V2.3: Non-AGS poll error alert
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
<html>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>Solar Dashboard - Pi 5</title>
<style>
body{font-family:'Inter',sans-serif;background:linear-gradient(135deg,#2c3e50,#34495e);color:#ecf0f1;margin:0;padding:20px;display:flex;flex-direction:column;align-items:center;min-height:100vh;}
.container{background:#3b5167;border-radius:15px;box-shadow:0 10px 30px rgba(0,0,0,0.6);padding:30px;max-width:900px;width:100%;box-sizing:border-box;text-align:center;}
h2{color:#82e0aa;margin-bottom:25px;font-size:1.8em;}
.data-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:20px;margin-bottom:20px;}
.solar-data-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-bottom:20px;}
.generator-data-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;margin-bottom:20px;}
@media(max-width:600px){.data-grid,.solar-data-grid,.generator-data-grid{grid-template-columns:1fr;}}
.card{background:#4a6582;border-radius:10px;padding:20px;box-shadow:0 5px 15px rgba(0,0,0,0.5);}
.card h3{font-size:1em;color:#bbdefb;margin:0 0 10px 0;}
.card p{font-size:2em;font-weight:bold;margin:0;color:#fff;}
.card .small-text{font-size:1.1em;font-weight:normal;margin-top:5px;color:#b0c4de;}
.bar-container{background-color:#5d7a96;border-radius:5px;height:15px;margin-top:10px;overflow:hidden;}
.bar{height:100%;background:linear-gradient(90deg,#00e0a8,#00c0ff);border-radius:5px;transition:width 0.5s;}
.section-title{color:#82e0aa;margin:20px 0 15px 0;font-size:1.5em;border-bottom:2px solid #5d7a96;padding-bottom:10px;}
.gen-controls{margin-top:15px;display:flex;flex-direction:column;align-items:center;gap:8px;}
.gen-controls select{padding:8px;border-radius:5px;border:1px solid #5d7a96;background:#3b5167;color:#ecf0f1;width:100%;max-width:150px;}
.gen-controls button{padding:8px 12px;border-radius:5px;border:none;background:#82e0aa;color:#2c3e50;font-weight:bold;cursor:pointer;}
.gen-controls button:hover{background:#5cb85c;}
.settings-panel{background:#3d566e;border-radius:10px;padding:20px;margin-top:20px;text-align:left;}
.settings-panel h3{color:#f39c12;margin:0 0 15px 0;}
.settings-row{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:15px;}
@media(max-width:600px){.settings-row{grid-template-columns:1fr;}}
.settings-group{background:#4a6582;border-radius:8px;padding:15px;}
.settings-group h4{color:#82e0aa;margin:0 0 10px 0;font-size:1em;}
.setting-item{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
.setting-item label{color:#b0c4de;font-size:0.9em;}
.setting-item input{width:80px;padding:5px 8px;border-radius:4px;border:1px solid #5d7a96;background:#3b5167;color:#ecf0f1;text-align:right;}
.setting-item input[type=text]{width:160px;text-align:left;}
.toggle-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px;padding:10px;background:#4a6582;border-radius:8px;}
.toggle-btn{padding:10px 20px;border-radius:5px;border:none;font-weight:bold;cursor:pointer;transition:all 0.3s;min-width:100px;}
.toggle-btn.enabled{background:#27ae60;color:white;}
.toggle-btn.disabled{background:#c0392b;color:white;}
.save-btn{background:#3498db;color:white;padding:10px 20px;border:none;border-radius:5px;cursor:pointer;font-weight:bold;margin-top:10px;margin-right:5px;}
.save-btn:hover{background:#2980b9;}
.test-btn{background:#9b59b6;color:white;padding:10px 20px;border:none;border-radius:5px;cursor:pointer;font-weight:bold;margin-top:10px;}
.test-btn:hover{background:#8e44ad;}
.event-log{background:#1a252f;border-radius:5px;padding:10px;height:220px;overflow-y:auto;font-family:monospace;font-size:0.78em;text-align:left;line-height:1.5;}
.ev-error{color:#e74c3c;}
.ev-warn{color:#f39c12;}
.ev-ok{color:#2ecc71;}
.ev-info{color:#b0c4de;}
.footer{margin-top:20px;font-size:0.8em;color:#b0c4de;}
.status-indicator{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:8px;}
.status-ok{background:#82e0aa;}
.status-error{background:#e74c3c;animation:blink 1s infinite;}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}
.live-rate{font-size:0.8em;color:#f39c12;margin-top:5px;}
.ags-status{font-size:0.75em;margin-top:4px;}
.ags-online{color:#2ecc71;}
.ags-offline{color:#e74c3c;font-weight:bold;}
.error-banner{background:#c0392b;color:white;padding:10px 15px;border-radius:8px;margin-bottom:15px;font-weight:bold;display:none;}
</style>
</head>
<body>
<div class='container'>
<h2>☀️ Solar Inverter Dashboard</h2>
<div id='errorBanner' class='error-banner'></div>

<h3 class='section-title'>Inverters & Battery</h3>
<div class='data-grid'>
  <div class='card'><h3>🔌 Inverter M</h3><p><span id='acPower1_value'>--</span> W</p><p class='small-text'><span id='acCurrent1_value'>--</span> A</p></div>
  <div class='card'><h3>🔌 Inverter S</h3><p><span id='acPower2_value'>--</span> W</p><p class='small-text'><span id='acCurrent2_value'>--</span> A</p></div>
  <div class='card'><h3>🔋 Voltage</h3><p><span id='batteryVoltage_value'>--</span> V</p></div>
  <div class='card'><h3>🔋 SOC</h3><p><span id='batterySOC_value'>--</span> %</p><div class='bar-container'><div id='batterySOC_bar' class='bar' style='width:0%'></div></div></div>
</div>

<h3 class='section-title'>Solar Arrays</h3>
<div class='solar-data-grid'>
  <div class='card'><h3>☀️ MPPT 80 (Roof)</h3><p><span id='mppt80PVPower_value'>--</span> W</p><p class='small-text'><span id='mppt80ChargeStatus_value'>--</span></p></div>
  <div class='card'><h3>☀️ South Array</h3><p><span id='southArrayPVPower_value'>--</span> W</p><p class='small-text'><span id='southArrayChargeStatus_value'>--</span></p></div>
  <div class='card'><h3>☀️ West Array</h3><p><span id='westArrayPVPower_value'>--</span> W</p><p class='small-text'><span id='westArrayChargeStatus_value'>--</span></p></div>
</div>

<h3 class='section-title'>Generators</h3>
<div class='generator-data-grid'>
  <div class='card'>
    <h3>🔧 MEP-803A</h3>
    <p><span id='mep803aMode_value'>--</span></p>
    <p class='live-rate'>Live Rate: <span id='mepChargeRateLive_value'>--</span>%</p>
    <p class='ags-status' id='mep_ags_status'>AGS: --</p>
    <div class='gen-controls'>
      <select id='mep803a_select'><option value='0'>OFF</option><option value='1'>ON</option><option value='2'>AUTO</option></select>
      <button onclick='setGeneratorMode(51,document.getElementById("mep803a_select").value)'>Set</button>
    </div>
  </div>
  <div class='card'>
    <h3>🔧 Kubota</h3>
    <p><span id='kubotaMode_value'>--</span></p>
    <p class='live-rate'>Live Rate: <span id='kubotaChargeRateLive_value'>--</span>%</p>
    <p class='ags-status' id='kubota_ags_status'>AGS: --</p>
    <div class='gen-controls'>
      <select id='kubota_select'><option value='0'>OFF</option><option value='1'>ON</option><option value='2'>AUTO</option></select>
      <button onclick='setGeneratorMode(50,document.getElementById("kubota_select").value)'>Set</button>
    </div>
  </div>
</div>

<div class='settings-panel'>
  <h3>⚡ Automatic Generator Control</h3>
  <div class='toggle-row'>
    <span>Auto Control: <strong id='autoGenStatus' style='color:#c0392b;'>DISABLED</strong></span>
    <button id='autoGenToggle' class='toggle-btn disabled' onclick='toggleAutoGen()'>ENABLE</button>
  </div>
  <div class='settings-row'>
    <div class='settings-group'>
      <h4>MEP-803A Thresholds</h4>
      <div class='setting-item'><label>Start Voltage (V)</label><input type='number' id='mepStartV' step='0.1'></div>
      <div class='setting-item'><label>Stop Voltage (V)</label><input type='number' id='mepStopV' step='0.1'></div>
      <div class='setting-item'><label>Charge Rate (%)</label><input type='number' id='mepChargeRate' min='10' max='100'></div>
      <div class='setting-item'><label>Max Runtime (min)</label><input type='number' id='mepMaxRuntime' min='10' max='480'></div>
      <div class='setting-item'><label>Cooldown (min)</label><input type='number' id='mepCooldown' min='1' max='60'></div>
      <button class='save-btn' onclick='saveMepSettings()'>SAVE MEP</button>
    </div>
    <div class='settings-group'>
      <h4>Kubota Thresholds</h4>
      <div class='setting-item'><label>Start Voltage (V)</label><input type='number' id='kubStartV' step='0.1'></div>
      <div class='setting-item'><label>Stop Voltage (V)</label><input type='number' id='kubStopV' step='0.1'></div>
      <div class='setting-item'><label>Charge Rate (%)</label><input type='number' id='kubChargeRate' min='10' max='100'></div>
      <div class='setting-item'><label>Max Runtime (min)</label><input type='number' id='kubMaxRuntime' min='10' max='480'></div>
      <div class='setting-item'><label>Cooldown (min)</label><input type='number' id='kubCooldown' min='1' max='60'></div>
      <button class='save-btn' onclick='saveKubSettings()'>SAVE KUBOTA</button>
    </div>
  </div>

  <h4 style='color:#f39c12;margin-top:15px;'>🔧 System Settings</h4>
  <div class='settings-row'>
    <div class='settings-group'>
      <h4>Ramp-Down Settings</h4>
      <div class='setting-item'><label>Step Delay (sec)</label><input type='number' id='rampStepDelay' min='5' max='60'></div>
      <div class='setting-item'><label>Zero Hold (sec)</label><input type='number' id='rampZeroHold' min='30' max='300'></div>
      <button class='save-btn' onclick='saveRampSettings()'>SAVE RAMP</button>
    </div>
    <div class='settings-group'>
      <h4>📱 Telegram Alerts</h4>
      <div class='toggle-row' style='margin-bottom:8px;padding:8px;'>
        <span style='font-size:0.9em;'>Alerts: <strong id='telegramStatus' style='color:#c0392b;'>OFF</strong></span>
        <button id='telegramToggle' class='toggle-btn disabled' style='min-width:70px;padding:6px 12px;' onclick='toggleTelegram()'>ENABLE</button>
      </div>
      <div class='setting-item'><label>Bot Token</label><input type='text' id='tgToken' placeholder='123456:ABC...'></div>
      <div class='setting-item'><label>Chat ID</label><input type='text' id='tgChatId' placeholder='123456789'></div>
      <button class='save-btn' onclick='saveTelegramSettings()'>SAVE</button>
      <button class='test-btn' onclick='testTelegram()'>TEST</button>
    </div>
  </div>

  <h4 style='color:#f39c12;margin-top:15px;'>📋 Event & Error Log</h4>
  <div class='event-log' id='eventLog'><span class='ev-info'>Loading...</span></div>
</div>

<div class='footer'>
  <span id='status_indicator' class='status-indicator status-ok'></span>
  Last Update: <span id='lastUpdate_value'>--:--:--</span> |
  Errors: <span id='pollErrors_value'>0</span> |
  <a href='/registers' style='color:#82e0aa;'>Register Tool</a> |
  Pi 5 V2.3
</div>
</div>

<script>
const chargeStatusMap={0:'Not Charging',768:'Not Charging',769:'Bulk',770:'Absorption',773:'Float',774:'No Float',776:'Disabled',1025:'AC Pass-Thru'};
const genModeMap={0:'OFF',1:'ON',2:'AUTO',3:'Force On'};
let currentConfig=null;

function updateUI(data){
  document.getElementById('acPower1_value').textContent=data.acPower1||0;
  document.getElementById('acCurrent1_value').textContent=data.acCurrent1||0;
  document.getElementById('acPower2_value').textContent=data.acPower2||0;
  document.getElementById('acCurrent2_value').textContent=data.acCurrent2||0;
  document.getElementById('batteryVoltage_value').textContent=data.batteryVoltage||0;
  document.getElementById('batterySOC_value').textContent=data.batterySOC||0;
  document.getElementById('batterySOC_bar').style.width=(data.batterySOC||0)+'%';
  document.getElementById('mppt80PVPower_value').textContent=data.mppt80PVPower||0;
  document.getElementById('mppt80ChargeStatus_value').textContent=chargeStatusMap[data.mppt80ChargeStatus]||'Unknown';
  document.getElementById('southArrayPVPower_value').textContent=data.southArrayPVPower||0;
  document.getElementById('southArrayChargeStatus_value').textContent=chargeStatusMap[data.southArrayChargeStatus]||'Unknown';
  document.getElementById('westArrayPVPower_value').textContent=data.westArrayPVPower||0;
  document.getElementById('westArrayChargeStatus_value').textContent=chargeStatusMap[data.westArrayChargeStatus]||'Unknown';
  document.getElementById('mep803aMode_value').textContent=genModeMap[data.mep803aMode]||'Unknown';
  document.getElementById('kubotaMode_value').textContent=genModeMap[data.kubotaMode]||'Unknown';
  document.getElementById('mepChargeRateLive_value').textContent=data.mepChargeRateLive||0;
  document.getElementById('kubotaChargeRateLive_value').textContent=data.kubotaChargeRateLive||0;
  document.getElementById('lastUpdate_value').textContent=data.lastUpdate||'--:--:--';
  const errors=data.pollErrors||0;
  document.getElementById('pollErrors_value').textContent=errors;
  document.getElementById('status_indicator').className=errors>0?'status-indicator status-error':'status-indicator status-ok';
  // AGS status
  const mepAgs=document.getElementById('mep_ags_status');
  const kubAgs=document.getElementById('kubota_ags_status');
  if(data.mepAgsOnline){mepAgs.textContent='AGS: Online';mepAgs.className='ags-status ags-online';}
  else{mepAgs.textContent='AGS: OFFLINE ⚠️';mepAgs.className='ags-status ags-offline';}
  if(data.kubotaAgsOnline){kubAgs.textContent='AGS: Online';kubAgs.className='ags-status ags-online';}
  else{kubAgs.textContent='AGS: OFFLINE ⚠️';kubAgs.className='ags-status ags-offline';}
  // Error banner
  const banner=document.getElementById('errorBanner');
  if(errors>0){banner.style.display='block';banner.textContent='⚠️ '+errors+' Modbus poll error(s) detected';}
  else{banner.style.display='none';}
}

function updateConfigUI(cfg){
  if(!cfg)return;
  currentConfig=cfg;
  const enabled=cfg.autoGenEnabled===true;
  document.getElementById('autoGenStatus').textContent=enabled?'ENABLED':'DISABLED';
  document.getElementById('autoGenStatus').style.color=enabled?'#27ae60':'#c0392b';
  const btn=document.getElementById('autoGenToggle');
  btn.textContent=enabled?'DISABLE':'ENABLE';
  btn.className=enabled?'toggle-btn enabled':'toggle-btn disabled';
  if(cfg.mep803a){
    document.getElementById('mepStartV').value=cfg.mep803a.startVoltage||51.5;
    document.getElementById('mepStopV').value=cfg.mep803a.stopVoltage||55.0;
    document.getElementById('mepChargeRate').value=cfg.mep803a.chargeRate||100;
    document.getElementById('mepMaxRuntime').value=cfg.mep803a.maxRuntime||120;
    document.getElementById('mepCooldown').value=cfg.mep803a.cooldown||5;
  }
  if(cfg.kubota){
    document.getElementById('kubStartV').value=cfg.kubota.startVoltage||52.3;
    document.getElementById('kubStopV').value=cfg.kubota.stopVoltage||55.0;
    document.getElementById('kubChargeRate').value=cfg.kubota.chargeRate||70;
    document.getElementById('kubMaxRuntime').value=cfg.kubota.maxRuntime||120;
    document.getElementById('kubCooldown').value=cfg.kubota.cooldown||5;
  }
  if(cfg.rampDown){
    document.getElementById('rampStepDelay').value=cfg.rampDown.stepDelay||15;
    document.getElementById('rampZeroHold').value=cfg.rampDown.zeroHoldTime||120;
  }
  if(cfg.telegram){
    const tgOn=cfg.telegram.enabled===true;
    document.getElementById('telegramStatus').textContent=tgOn?'ON':'OFF';
    document.getElementById('telegramStatus').style.color=tgOn?'#27ae60':'#c0392b';
    const tgBtn=document.getElementById('telegramToggle');
    tgBtn.textContent=tgOn?'DISABLE':'ENABLE';
    tgBtn.className=tgOn?'toggle-btn enabled':'toggle-btn disabled';
    document.getElementById('tgToken').value=cfg.telegram.token||'';
    document.getElementById('tgChatId').value=cfg.telegram.chatId||'';
  }
}

function updateEventLog(events){
  const log=document.getElementById('eventLog');
  if(!events||events.length===0){log.innerHTML='<span class="ev-info">No events yet...</span>';return;}
  log.innerHTML=events.slice().reverse().map(e=>{
    let cls='ev-info';
    if(e.includes('⚠️')||e.includes('FAILED')||e.includes('OFFLINE')||e.toLowerCase().includes('error'))cls='ev-error';
    else if(e.includes('✅')||e.includes('started')||e.includes('ONLINE')||e.includes('restored'))cls='ev-ok';
    return '<div class="'+cls+'">'+e+'</div>';
  }).join('');
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
  if(!currentConfig){alert('Config not loaded');return;}
  const r=await fetch('/config?autoGenEnabled='+(currentConfig.autoGenEnabled?'0':'1'));
  if(r.ok)fetchConfig();
}
async function toggleTelegram(){
  if(!currentConfig){alert('Config not loaded');return;}
  const r=await fetch('/config?tg.enabled='+(currentConfig.telegram.enabled?'0':'1'));
  if(r.ok)fetchConfig();
}
async function saveMepSettings(){
  const p=new URLSearchParams({'mep.startVoltage':document.getElementById('mepStartV').value,'mep.stopVoltage':document.getElementById('mepStopV').value,'mep.chargeRate':document.getElementById('mepChargeRate').value,'mep.maxRuntime':document.getElementById('mepMaxRuntime').value,'mep.cooldown':document.getElementById('mepCooldown').value});
  const r=await fetch('/config?'+p);if(r.ok){alert('MEP settings saved!');fetchConfig();}else alert('Failed');
}
async function saveKubSettings(){
  const p=new URLSearchParams({'kub.startVoltage':document.getElementById('kubStartV').value,'kub.stopVoltage':document.getElementById('kubStopV').value,'kub.chargeRate':document.getElementById('kubChargeRate').value,'kub.maxRuntime':document.getElementById('kubMaxRuntime').value,'kub.cooldown':document.getElementById('kubCooldown').value});
  const r=await fetch('/config?'+p);if(r.ok){alert('Kubota settings saved!');fetchConfig();}else alert('Failed');
}
async function saveRampSettings(){
  const p=new URLSearchParams({'ramp.stepDelay':document.getElementById('rampStepDelay').value,'ramp.zeroHoldTime':document.getElementById('rampZeroHold').value});
  const r=await fetch('/config?'+p);if(r.ok){alert('Ramp settings saved!');fetchConfig();}else alert('Failed');
}
async function saveTelegramSettings(){
  const p=new URLSearchParams({'tg.token':document.getElementById('tgToken').value,'tg.chatId':document.getElementById('tgChatId').value});
  const r=await fetch('/config?'+p);if(r.ok){alert('Telegram settings saved!');fetchConfig();}else alert('Failed');
}
async function testTelegram(){
  const r=await fetch('/testtelegram');const d=await r.json();
  alert(d.success?'✅ '+d.message:'❌ '+d.message);
}
function setGeneratorMode(slaveId,mode){
  const modeText={0:'OFF',1:'ON',2:'AUTO'}[mode]||'Unknown';
  if(!confirm('Set generator to '+modeText+'?'))return;
  const endpoint=(mode==0)?'/stopgen?id='+slaveId:'/setgen?id='+slaveId+'&state='+mode;
  fetch(endpoint).then(r=>{if(!r.ok)alert('Command failed');setTimeout(fetchData,1000);}).catch(e=>alert('Error: '+e));
}
document.addEventListener('DOMContentLoaded',()=>{
  fetchData();fetchConfig();
  setInterval(fetchData,5000);
  setInterval(fetchConfig,15000);
});
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
    return render_template_string(DASHBOARD_HTML)

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

# --- Initialize ---
logger.info("=" * 50)
logger.info("Solar Dashboard V2.3 Starting...")
load_config()

poll_thread = threading.Thread(target=poll_modbus, daemon=True)
poll_thread.start()
logger.info("Modbus polling started")
with config_lock:
    logger.info(f"Auto-gen: {'ENABLED' if config['autoGenEnabled'] else 'DISABLED'}")
    logger.info(f"Telegram: {'ENABLED' if config['telegram']['enabled'] else 'DISABLED'}")
logger.info("=" * 50)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
