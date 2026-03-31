#!/usr/bin/env python3
"""
Solar Dashboard - Pi 5 Flask Application V2.2
Full autonomous control with persistent settings

FIXES in V2.2:
  - Added sequence-in-progress guards (_stopping/_starting flags) to prevent
    concurrent generator start/stop sequences which caused AGS FC 0x83 faults
  - check_auto_generator() now skips if a sequence is already running

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

# Register addresses (from Schneider specs)
REG_AC_POWER = 0x009A          # 32-bit signed
REG_AC_CURRENT = 0x0096        # 32-bit signed
REG_BATTERY_VOLTAGE = 0x0046   # 32-bit unsigned
REG_BATTERY_SOC = 0x004C       # 32-bit unsigned
REG_PV_VOLTAGE = 0x004C        # 32-bit unsigned
REG_PV_CURRENT = 0x004E        # 32-bit unsigned
REG_PV_POWER = 0x0050          # 32-bit unsigned
REG_CHARGER_STATUS = 0x0049    # 16-bit
REG_GENERATOR_MODE = 0x004D    # 16-bit
REG_CHARGE_MODE_FORCE = 0x00AA # 16-bit
REG_CHARGER_ENABLE = 0x0164    # 16-bit (357)
REG_MAX_CHARGE_RATE = 0x016F   # 16-bit (368)
REG_OPERATING_MODE = 0x0166    # 16-bit
REG_FORCE_CHARGER_STATE = 0x0165  # 16-bit
REG_CHARGE_DC_POWER = 0x005E      # 32-bit unsigned - DC charging power in W

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
    "autoRebootHours": 2
}

# --- Global State ---
config = copy.deepcopy(DEFAULT_CONFIG)
config_lock = threading.Lock()

system_data = {
    "acPower1": 0,
    "acCurrent1": 0.0,
    "acPower2": 0,
    "acCurrent2": 0.0,
    "batteryVoltage": 0.0,
    "batterySOC": 0,
    "mppt80PVPower": 0,
    "mppt80PVVoltage": 0.0,
    "mppt80PVCurrent": 0.0,
    "mppt80ChargeStatus": 0,
    "southArrayPVPower": 0,
    "southArrayPVVoltage": 0.0,
    "southArrayPVCurrent": 0.0,
    "southArrayChargeStatus": 0,
    "westArrayPVPower": 0,
    "westArrayPVVoltage": 0.0,
    "westArrayPVCurrent": 0.0,
    "westArrayChargeStatus": 0,
    "mep803aMode": 0,
    "kubotaMode": 0,
    "lastUpdate": "00:00:00",
    "pollErrors": 0,
    # Live charge rates from inverters
    "mepChargeRateLive": 0,
    "kubotaChargeRateLive": 0,
    # DC Charge Power for ESP32 generator screen
    "chargePower1": 0,
    "chargePower2": 0,
    "chargePower3": 0,
}
data_lock = threading.Lock()
start_time = time.time()

# Auto-gen state
# _stopping / _starting flags prevent concurrent sequences from being spawned.
# check_auto_generator() checks these before spawning a new thread.
# start_generator() and stop_generator() set them True on entry, False on exit.
auto_gen_state = {
    "mep803a_running": False,
    "mep803a_start_time": None,
    "mep803a_cooldown_until": 0,
    "mep803a_low_voltage_since": None,
    "mep803a_stopping": False,   # V2.2: sequence-in-progress guard
    "mep803a_starting": False,   # V2.2: sequence-in-progress guard
    "kubota_running": False,
    "kubota_start_time": None,
    "kubota_cooldown_until": 0,
    "kubota_low_voltage_since": None,
    "kubota_stopping": False,    # V2.2: sequence-in-progress guard
    "kubota_starting": False,    # V2.2: sequence-in-progress guard
    "last_event": "",
    "events": []
}
auto_gen_lock = threading.Lock()

# Global Modbus client instance
modbus = SchneiderModbusTCP()

# --- Config File Functions ---
def load_config():
    """Load configuration from disk, or create default if not exists."""
    global config
    
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                loaded = json.load(f)
            
            # Start with defaults and update with loaded values
            with config_lock:
                config = copy.deepcopy(DEFAULT_CONFIG)
                
                # Update top-level keys
                if 'autoGenEnabled' in loaded:
                    config['autoGenEnabled'] = loaded['autoGenEnabled']
                if 'autoRebootHours' in loaded:
                    config['autoRebootHours'] = loaded['autoRebootHours']
                
                # Update nested dicts
                for section in ['mep803a', 'kubota', 'rampDown']:
                    if section in loaded and isinstance(loaded[section], dict):
                        for key, value in loaded[section].items():
                            if key in config[section]:
                                config[section][key] = value
            
            logger.info(f"Config loaded from {CONFIG_FILE}")
            logger.info(f"  autoGenEnabled: {config['autoGenEnabled']}")
            logger.info(f"  MEP charge rate: {config['mep803a']['chargeRate']}%")
            logger.info(f"  Kubota charge rate: {config['kubota']['chargeRate']}%")
        else:
            with config_lock:
                config = copy.deepcopy(DEFAULT_CONFIG)
            save_config()
            logger.info(f"Created default config at {CONFIG_FILE}")
            
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        with config_lock:
            config = copy.deepcopy(DEFAULT_CONFIG)

def save_config():
    """Save configuration to disk."""
    try:
        # Ensure directory exists
        config_dir = os.path.dirname(CONFIG_FILE)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)
        
        with config_lock:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
        
        logger.info(f"Config saved to {CONFIG_FILE}")
        return True
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        return False

def log_event(message):
    """Log an event to the auto-gen state."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    event = f"{timestamp} - {message}"
    with auto_gen_lock:
        auto_gen_state["last_event"] = event
        auto_gen_state["events"].append(event)
        if len(auto_gen_state["events"]) > 50:
            auto_gen_state["events"] = auto_gen_state["events"][-50:]
    logger.info(f"EVENT: {event}")

# --- Charger Control Functions ---
def set_charge_rate_single(slave_id, rate):
    """Set charge rate on a single inverter."""
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_MAX_CHARGE_RATE, rate)
    if success:
        logger.info(f"Inverter {slave_id} charge rate set to {rate}%")
    return success

def set_charger_enabled_single(slave_id, enabled):
    """Enable/disable charger on a single inverter."""
    value = 1 if enabled else 0
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_CHARGER_ENABLE, value)
    if success:
        logger.info(f"Inverter {slave_id} charger {'enabled' if enabled else 'disabled'}")
    return success

def force_charger_state_single(slave_id, state):
    """Force charger state (1=Bulk, 2=Float, 3=No Float)."""
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_FORCE_CHARGER_STATE, state)
    if success:
        logger.info(f"Inverter {slave_id} forced to state {state}")
    return success

def set_operating_mode_single(slave_id, mode):
    """Set operating mode (2=Standby, 3=Operating)."""
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_OPERATING_MODE, mode)
    if success:
        logger.info(f"Inverter {slave_id} operating mode set to {mode}")
    return success

def read_charge_rate(slave_id):
    """Read current charge rate from an inverter."""
    result = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_MAX_CHARGE_RATE)
    return result if result is not None else 0

# --- MEP-803A Charger Control (XW Pro 6848 Master + Slave) ---
def ensure_mep_chargers_ready():
    """Enable MEP-803A chargers at configured rate and force to Bulk."""
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
    """Ramp down MEP-803A chargers before generator stop."""
    with config_lock:
        step_delay = config["rampDown"]["stepDelay"]
        zero_hold = config["rampDown"]["zeroHoldTime"]
    
    logger.info(">>> Ramping down MEP-803A chargers...")
    log_event("MEP ramp-down started")
    
    ramp_steps = [75, 50, 25, 10, 0]
    for rate in ramp_steps:
        logger.info(f"  Setting charge rate to {rate}%")
        set_charge_rate_single(INVERTER_1_ID, rate)
        set_charge_rate_single(INVERTER_2_ID, rate)
        time.sleep(step_delay)
    
    logger.info("  Disabling chargers...")
    set_charger_enabled_single(INVERTER_1_ID, False)
    set_charger_enabled_single(INVERTER_2_ID, False)
    
    logger.info(f"  Holding at 0% for {zero_hold} seconds...")
    time.sleep(zero_hold)
    
    log_event("MEP ramp-down complete")

def restore_mep_chargers():
    """Restore MEP-803A chargers after generator stop."""
    with config_lock:
        rate = config["mep803a"]["chargeRate"]
    
    logger.info(f">>> Restoring MEP-803A chargers to {rate}%...")
    
    set_charger_enabled_single(INVERTER_1_ID, True)
    set_charger_enabled_single(INVERTER_2_ID, True)
    time.sleep(0.3)
    set_charge_rate_single(INVERTER_1_ID, rate)
    set_charge_rate_single(INVERTER_2_ID, rate)
    time.sleep(0.3)
    force_charger_state_single(INVERTER_1_ID, 1)
    force_charger_state_single(INVERTER_2_ID, 1)
    
    log_event(f"MEP chargers restored @ {rate}%")

# --- Kubota Charger Control (XW+ 5548) ---
def ensure_kubota_chargers_ready():
    """Enable Kubota charger at configured rate (70% default)."""
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
    """Ramp down Kubota charger before generator stop."""
    with config_lock:
        step_delay = config["rampDown"]["stepDelay"]
        zero_hold = config["rampDown"]["zeroHoldTime"]
    
    logger.info(">>> Ramping down Kubota charger...")
    log_event("Kubota ramp-down started")
    
    ramp_steps = [50, 25, 10, 0]
    for rate in ramp_steps:
        logger.info(f"  Setting charge rate to {rate}%")
        set_charge_rate_single(INVERTER_3_ID, rate)
        time.sleep(step_delay)
    
    logger.info("  Disabling charger...")
    set_charger_enabled_single(INVERTER_3_ID, False)
    
    logger.info(f"  Holding at 0% for {zero_hold} seconds...")
    time.sleep(zero_hold)
    
    log_event("Kubota ramp-down complete")

def restore_kubota_chargers():
    """Restore Kubota charger after generator stop."""
    with config_lock:
        rate = config["kubota"]["chargeRate"]
    
    logger.info(f">>> Restoring Kubota charger to {rate}%...")
    
    set_charger_enabled_single(INVERTER_3_ID, True)
    time.sleep(0.3)
    set_charge_rate_single(INVERTER_3_ID, rate)
    time.sleep(0.3)
    force_charger_state_single(INVERTER_3_ID, 1)
    
    log_event(f"Kubota charger restored @ {rate}%")

# --- Generator Control Functions ---
def start_generator(gen_type):
    """Start a generator and activate its chargers.
    V2.2: Sets _starting flag on entry, clears on exit to prevent concurrent sequences.
    """
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
            return success
        finally:
            with auto_gen_lock:
                auto_gen_state["kubota_starting"] = False

    return False

def stop_generator(gen_type, graceful=True):
    """Stop a generator with optional ramp-down, with retry on AUTO.
    V2.2: Sets _stopping flag on entry, clears on exit to prevent concurrent sequences.
    """
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

# --- Auto Generator Control Logic ---
def check_auto_generator():
    """Check if generators should auto-start or stop based on voltage.
    V2.2: Skips spawning a new thread if a sequence is already in progress.
    """
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
    
    with auto_gen_lock:
        # --- MEP-803A Logic ---
        mep_is_running = (mep_mode == 1)

        if not mep_is_running and voltage <= mep_cfg["startVoltage"]:
            if current_time > auto_gen_state["mep803a_cooldown_until"]:
                if auto_gen_state["mep803a_low_voltage_since"] is None:
                    auto_gen_state["mep803a_low_voltage_since"] = current_time
                elif current_time - auto_gen_state["mep803a_low_voltage_since"] >= 60:
                    # V2.2: Only spawn if no sequence is already running
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
                # V2.2: Only spawn if no sequence is already running
                if not auto_gen_state["mep803a_stopping"] and not auto_gen_state["mep803a_starting"]:
                    logger.info(f"AUTO: Stopping MEP-803A ({reason})")
                    threading.Thread(target=stop_generator, args=("mep803a", True), daemon=True).start()
                else:
                    logger.info("AUTO: MEP-803A sequence in progress, skipping stop trigger")

        # --- Kubota Logic ---
        kubota_is_running = (kubota_mode == 1)

        if not kubota_is_running and not mep_is_running and voltage <= kub_cfg["startVoltage"]:
            if current_time > auto_gen_state["kubota_cooldown_until"]:
                if auto_gen_state["kubota_low_voltage_since"] is None:
                    auto_gen_state["kubota_low_voltage_since"] = current_time
                elif current_time - auto_gen_state["kubota_low_voltage_since"] >= 60:
                    # V2.2: Only spawn if no sequence is already running
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
                # V2.2: Only spawn if no sequence is already running
                if not auto_gen_state["kubota_stopping"] and not auto_gen_state["kubota_starting"]:
                    logger.info(f"AUTO: Stopping Kubota ({reason})")
                    threading.Thread(target=stop_generator, args=("kubota", True), daemon=True).start()
                else:
                    logger.info("AUTO: Kubota sequence in progress, skipping stop trigger")

# --- Polling Thread ---
def poll_modbus():
    """Background thread to poll all Modbus devices."""
    global system_data, start_time
    
    while True:
        errors = 0
        new_data = {}
        
        try:
            # Inverter 1
            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_AC_POWER)
            new_data["acPower1"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_AC_CURRENT)
            new_data["acCurrent1"] = round(val / 1000.0, 3) if val is not None else 0.0
            errors += 0 if val is not None else 1
            
            # Inverter 2
            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_2_ID, REG_AC_POWER)
            new_data["acPower2"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            val = modbus.read_holding_register_32s(MODBUS_HOST, MODBUS_PORT, INVERTER_2_ID, REG_AC_CURRENT)
            new_data["acCurrent2"] = round(val / 1000.0, 3) if val is not None else 0.0
            errors += 0 if val is not None else 1
            
            # Battery Monitor
            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, BATTERY_MONITOR_ID, REG_BATTERY_VOLTAGE)
            new_data["batteryVoltage"] = round(val / 1000.0, 2) if val is not None else 0.0
            errors += 0 if val is not None else 1
            
            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, BATTERY_MONITOR_ID, REG_BATTERY_SOC)
            new_data["batterySOC"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            # MPPT 80
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
            
            # South Array
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
            
            # West Array
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
            
            # AGS Units
            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, AGS_MEP803A_ID, REG_GENERATOR_MODE)
            new_data["mep803aMode"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            val = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, AGS_KUBOTA_ID, REG_GENERATOR_MODE)
            new_data["kubotaMode"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            # Read LIVE charge rates from inverters
            mep_rate = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_MAX_CHARGE_RATE)
            new_data["mepChargeRateLive"] = mep_rate if mep_rate is not None else 0
            
            kubota_rate = modbus.read_holding_register_16(MODBUS_HOST, MODBUS_PORT, INVERTER_3_ID, REG_MAX_CHARGE_RATE)
            new_data["kubotaChargeRateLive"] = kubota_rate if kubota_rate is not None else 0
            
            # DC Charge Power (for ESP32 generator screen)
            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, INVERTER_1_ID, REG_CHARGE_DC_POWER)
            new_data["chargePower1"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, INVERTER_2_ID, REG_CHARGE_DC_POWER)
            new_data["chargePower2"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            val = modbus.read_holding_register_32(MODBUS_HOST, MODBUS_PORT, INVERTER_3_ID, REG_CHARGE_DC_POWER)
            new_data["chargePower3"] = val if val is not None else 0
            errors += 0 if val is not None else 1
            
            # Timestamp
            elapsed = int(time.time() - start_time)
            hours = (elapsed // 3600) % 24
            minutes = (elapsed // 60) % 60
            seconds = elapsed % 60
            new_data["lastUpdate"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            new_data["pollErrors"] = errors
            
            with data_lock:
                system_data.update(new_data)
            
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
body { font-family: 'Inter', sans-serif; background: linear-gradient(135deg, #2c3e50, #34495e); color: #ecf0f1; margin: 0; padding: 20px; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
.container { background: #3b5167; border-radius: 15px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6); padding: 30px; max-width: 900px; width: 100%; box-sizing: border-box; text-align: center; }
h2 { color: #82e0aa; margin-bottom: 25px; font-size: 1.8em; }
.data-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 20px; margin-bottom: 20px; }
.solar-data-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 20px; }
.generator-data-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; margin-bottom: 20px; }
@media (max-width: 600px) { .data-grid, .solar-data-grid, .generator-data-grid { grid-template-columns: 1fr; } }
.card { background: #4a6582; border-radius: 10px; padding: 20px; box-shadow: 0 5px 15px rgba(0, 0, 0, 0.5); }
.card h3 { font-size: 1em; color: #bbdefb; margin: 0 0 10px 0; }
.card p { font-size: 2em; font-weight: bold; margin: 0; color: #fff; }
.card .small-text { font-size: 1.1em; font-weight: normal; margin-top: 5px; color: #b0c4de; }
.bar-container { background-color: #5d7a96; border-radius: 5px; height: 15px; margin-top: 10px; overflow: hidden; }
.bar { height: 100%; background: linear-gradient(90deg, #00e0a8, #00c0ff); border-radius: 5px; transition: width 0.5s; }
.section-title { color: #82e0aa; margin: 20px 0 15px 0; font-size: 1.5em; border-bottom: 2px solid #5d7a96; padding-bottom: 10px; }
.gen-controls { margin-top: 15px; display: flex; flex-direction: column; align-items: center; gap: 8px; }
.gen-controls select { padding: 8px; border-radius: 5px; border: 1px solid #5d7a96; background: #3b5167; color: #ecf0f1; width: 100%; max-width: 150px; }
.gen-controls button { padding: 8px 12px; border-radius: 5px; border: none; background: #82e0aa; color: #2c3e50; font-weight: bold; cursor: pointer; }
.gen-controls button:hover { background: #5cb85c; }
.settings-panel { background: #3d566e; border-radius: 10px; padding: 20px; margin-top: 20px; text-align: left; }
.settings-panel h3 { color: #f39c12; margin: 0 0 15px 0; }
.settings-row { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 15px; }
@media (max-width: 600px) { .settings-row { grid-template-columns: 1fr; } }
.settings-group { background: #4a6582; border-radius: 8px; padding: 15px; }
.settings-group h4 { color: #82e0aa; margin: 0 0 10px 0; font-size: 1em; }
.setting-item { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.setting-item label { color: #b0c4de; font-size: 0.9em; }
.setting-item input { width: 80px; padding: 5px 8px; border-radius: 4px; border: 1px solid #5d7a96; background: #3b5167; color: #ecf0f1; text-align: right; }
.toggle-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; padding: 10px; background: #4a6582; border-radius: 8px; }
.toggle-btn { padding: 10px 20px; border-radius: 5px; border: none; font-weight: bold; cursor: pointer; transition: all 0.3s; min-width: 100px; }
.toggle-btn.enabled { background: #27ae60; color: white; }
.toggle-btn.disabled { background: #c0392b; color: white; }
.save-btn { background: #3498db; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; margin-top: 10px; }
.save-btn:hover { background: #2980b9; }
.event-log { background: #2c3e50; border-radius: 5px; padding: 10px; max-height: 100px; overflow-y: auto; font-family: monospace; font-size: 0.8em; }
.footer { margin-top: 20px; font-size: 0.8em; color: #b0c4de; }
.status-indicator { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; }
.status-ok { background: #82e0aa; }
.status-error { background: #e74c3c; }
.live-rate { font-size: 0.8em; color: #f39c12; margin-top: 5px; }
</style>
</head>
<body>
<div class='container'>
<h2>☀️ Solar Inverter Dashboard</h2>

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
        <div class='gen-controls'>
            <select id='mep803a_select'><option value='0'>OFF</option><option value='1'>ON</option><option value='2'>AUTO</option></select>
            <button onclick='setGeneratorMode(51, document.getElementById("mep803a_select").value)'>Set</button>
        </div>
    </div>
    <div class='card'>
        <h3>🔧 Kubota</h3>
        <p><span id='kubotaMode_value'>--</span></p>
        <p class='live-rate'>Live Rate: <span id='kubotaChargeRateLive_value'>--</span>%</p>
        <div class='gen-controls'>
            <select id='kubota_select'><option value='0'>OFF</option><option value='1'>ON</option><option value='2'>AUTO</option></select>
            <button onclick='setGeneratorMode(50, document.getElementById("kubota_select").value)'>Set</button>
        </div>
    </div>
</div>

<div class='settings-panel'>
    <h3>⚡ Automatic Generator Control</h3>
    
    <div class='toggle-row'>
        <span>Auto Control: <strong id='autoGenStatus' style='color: #c0392b;'>DISABLED</strong></span>
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
    
    <h4 style='color: #f39c12; margin-top: 15px;'>🔧 System Settings</h4>
    <div class='settings-row'>
        <div class='settings-group'>
            <h4>Ramp-Down Settings</h4>
            <div class='setting-item'><label>Step Delay (sec)</label><input type='number' id='rampStepDelay' min='5' max='60'></div>
            <div class='setting-item'><label>Zero Hold (sec)</label><input type='number' id='rampZeroHold' min='30' max='300'></div>
            <button class='save-btn' onclick='saveRampSettings()'>SAVE RAMP</button>
        </div>
        <div class='settings-group'>
            <h4>Recent Events</h4>
            <div class='event-log' id='eventLog'>Loading events...</div>
        </div>
    </div>
</div>

<div class='footer'>
    <span id='status_indicator' class='status-indicator status-ok'></span>
    Last Update: <span id='lastUpdate_value'>--:--:--</span> | 
    Errors: <span id='pollErrors_value'>0</span> | 
    <a href='/registers' style='color: #82e0aa;'>Register Tool</a> | 
    Running on Pi 5 V2.2
</div>
</div>

<script>
const chargeStatusMap = {0:'Not Charging', 768:'Not Charging', 769:'Bulk', 770:'Absorption', 773:'Float', 774:'No Float', 776:'Disabled', 1025:'AC Pass-Thru'};
const genModeMap = {0:'OFF', 1:'ON', 2:'AUTO', 3:'Force On'};
let currentConfig = null;

function updateUI(data) {
    document.getElementById('acPower1_value').textContent = data.acPower1 || 0;
    document.getElementById('acCurrent1_value').textContent = data.acCurrent1 || 0;
    document.getElementById('acPower2_value').textContent = data.acPower2 || 0;
    document.getElementById('acCurrent2_value').textContent = data.acCurrent2 || 0;
    document.getElementById('batteryVoltage_value').textContent = data.batteryVoltage || 0;
    document.getElementById('batterySOC_value').textContent = data.batterySOC || 0;
    document.getElementById('batterySOC_bar').style.width = (data.batterySOC || 0) + '%';
    document.getElementById('mppt80PVPower_value').textContent = data.mppt80PVPower || 0;
    document.getElementById('mppt80ChargeStatus_value').textContent = chargeStatusMap[data.mppt80ChargeStatus] || 'Unknown';
    document.getElementById('southArrayPVPower_value').textContent = data.southArrayPVPower || 0;
    document.getElementById('southArrayChargeStatus_value').textContent = chargeStatusMap[data.southArrayChargeStatus] || 'Unknown';
    document.getElementById('westArrayPVPower_value').textContent = data.westArrayPVPower || 0;
    document.getElementById('westArrayChargeStatus_value').textContent = chargeStatusMap[data.westArrayChargeStatus] || 'Unknown';
    document.getElementById('mep803aMode_value').textContent = genModeMap[data.mep803aMode] || 'Unknown';
    document.getElementById('kubotaMode_value').textContent = genModeMap[data.kubotaMode] || 'Unknown';
    document.getElementById('mepChargeRateLive_value').textContent = data.mepChargeRateLive || 0;
    document.getElementById('kubotaChargeRateLive_value').textContent = data.kubotaChargeRateLive || 0;
    document.getElementById('lastUpdate_value').textContent = data.lastUpdate || '--:--:--';
    document.getElementById('pollErrors_value').textContent = data.pollErrors || 0;
    document.getElementById('status_indicator').className = (data.pollErrors > 0) ? 'status-indicator status-error' : 'status-indicator status-ok';
}

function updateConfigUI(cfg) {
    if (!cfg) { console.error('No config received'); return; }
    currentConfig = cfg;
    const enabled = cfg.autoGenEnabled === true;
    document.getElementById('autoGenStatus').textContent = enabled ? 'ENABLED' : 'DISABLED';
    document.getElementById('autoGenStatus').style.color = enabled ? '#27ae60' : '#c0392b';
    const btn = document.getElementById('autoGenToggle');
    btn.textContent = enabled ? 'DISABLE' : 'ENABLE';
    btn.className = enabled ? 'toggle-btn enabled' : 'toggle-btn disabled';
    if (cfg.mep803a) {
        document.getElementById('mepStartV').value = cfg.mep803a.startVoltage || 51.5;
        document.getElementById('mepStopV').value = cfg.mep803a.stopVoltage || 55.0;
        document.getElementById('mepChargeRate').value = cfg.mep803a.chargeRate || 100;
        document.getElementById('mepMaxRuntime').value = cfg.mep803a.maxRuntime || 120;
        document.getElementById('mepCooldown').value = cfg.mep803a.cooldown || 5;
    }
    if (cfg.kubota) {
        document.getElementById('kubStartV').value = cfg.kubota.startVoltage || 52.3;
        document.getElementById('kubStopV').value = cfg.kubota.stopVoltage || 55.0;
        document.getElementById('kubChargeRate').value = cfg.kubota.chargeRate || 70;
        document.getElementById('kubMaxRuntime').value = cfg.kubota.maxRuntime || 120;
        document.getElementById('kubCooldown').value = cfg.kubota.cooldown || 5;
    }
    if (cfg.rampDown) {
        document.getElementById('rampStepDelay').value = cfg.rampDown.stepDelay || 15;
        document.getElementById('rampZeroHold').value = cfg.rampDown.zeroHoldTime || 120;
    }
}

async function fetchData() {
    try {
        const resp = await fetch('/data');
        if (resp.ok) { updateUI(await resp.json()); }
    } catch (e) { console.error('fetchData error:', e); }
}

async function fetchConfig() {
    try {
        const resp = await fetch('/config');
        if (resp.ok) {
            const data = await resp.json();
            if (data.config) updateConfigUI(data.config);
            if (data.events && data.events.length > 0) {
                document.getElementById('eventLog').innerHTML = data.events.slice(-10).reverse().join('<br>');
            } else {
                document.getElementById('eventLog').innerHTML = 'No events yet...';
            }
        }
    } catch (e) { console.error('fetchConfig error:', e); }
}

async function toggleAutoGen() {
    if (!currentConfig) { alert('Config not loaded yet'); return; }
    const newState = !currentConfig.autoGenEnabled;
    try {
        const resp = await fetch('/config?autoGenEnabled=' + (newState ? '1' : '0'));
        if (resp.ok) { await fetchConfig(); }
        else { alert('Failed to toggle auto-gen'); }
    } catch (e) { alert('Error: ' + e); }
}

async function saveMepSettings() {
    const params = new URLSearchParams({
        'mep.startVoltage': document.getElementById('mepStartV').value,
        'mep.stopVoltage': document.getElementById('mepStopV').value,
        'mep.chargeRate': document.getElementById('mepChargeRate').value,
        'mep.maxRuntime': document.getElementById('mepMaxRuntime').value,
        'mep.cooldown': document.getElementById('mepCooldown').value
    });
    try {
        const resp = await fetch('/config?' + params.toString());
        if (resp.ok) { alert('MEP settings saved!'); fetchConfig(); }
        else { alert('Failed to save'); }
    } catch (e) { alert('Error: ' + e); }
}

async function saveKubSettings() {
    const params = new URLSearchParams({
        'kub.startVoltage': document.getElementById('kubStartV').value,
        'kub.stopVoltage': document.getElementById('kubStopV').value,
        'kub.chargeRate': document.getElementById('kubChargeRate').value,
        'kub.maxRuntime': document.getElementById('kubMaxRuntime').value,
        'kub.cooldown': document.getElementById('kubCooldown').value
    });
    try {
        const resp = await fetch('/config?' + params.toString());
        if (resp.ok) { alert('Kubota settings saved!'); fetchConfig(); }
        else { alert('Failed to save'); }
    } catch (e) { alert('Error: ' + e); }
}

async function saveRampSettings() {
    const params = new URLSearchParams({
        'ramp.stepDelay': document.getElementById('rampStepDelay').value,
        'ramp.zeroHoldTime': document.getElementById('rampZeroHold').value
    });
    try {
        const resp = await fetch('/config?' + params.toString());
        if (resp.ok) { alert('Ramp settings saved!'); fetchConfig(); }
        else { alert('Failed to save'); }
    } catch (e) { alert('Error: ' + e); }
}

function setGeneratorMode(slaveId, mode) {
    const modeText = {0:'OFF', 1:'ON', 2:'AUTO'}[mode] || 'Unknown';
    if (!confirm('Set generator to ' + modeText + '?')) return;
    const endpoint = (mode == 0) ? '/stopgen?id=' + slaveId : '/setgen?id=' + slaveId + '&state=' + mode;
    fetch(endpoint).then(resp => {
        if (!resp.ok) alert('Command failed');
        setTimeout(fetchData, 1000);
    }).catch(e => alert('Error: ' + e));
}

document.addEventListener('DOMContentLoaded', () => {
    fetchData();
    fetchConfig();
    setInterval(fetchData, 5000);
    setInterval(fetchConfig, 30000);
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
body { font-family: sans-serif; background: linear-gradient(135deg, #1a252f, #2c3e50); color: #ecf0f1; margin: 0; padding: 15px; }
.container { background: #3b5167; border-radius: 8px; padding: 15px; max-width: 900px; margin: 0 auto; }
h2 { color: #82e0aa; margin: 0 0 15px 0; text-align: center; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
@media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
.panel { background: #4a6582; border-radius: 6px; padding: 12px; }
.panel h3 { color: #f39c12; margin: 0 0 10px 0; }
.form-row { display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.form-row label { color: #b0c4de; width: 60px; display: flex; align-items: center; }
.form-row input, .form-row select { flex: 1; padding: 6px; border-radius: 4px; border: 1px solid #5d7a96; background: #3b5167; color: #ecf0f1; }
.btn { padding: 8px 12px; border-radius: 4px; border: none; cursor: pointer; font-weight: bold; margin: 2px; }
.btn-read { background: #3498db; color: white; }
.btn-write { background: #e67e22; color: white; }
.btn-batch { background: #9b59b6; color: white; }
.log { background: #2c3e50; border-radius: 4px; padding: 10px; height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.8em; margin-top: 15px; }
.back-link { display: block; text-align: center; margin-top: 15px; color: #82e0aa; }
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
function log(msg, type='info') {
  const d = document.getElementById('log');
  const e = document.createElement('div');
  e.style.color = type==='success'?'#27ae60':type==='error'?'#e74c3c':'#3498db';
  e.textContent = new Date().toLocaleTimeString() + ' ' + msg;
  d.appendChild(e);
  d.scrollTop = d.scrollHeight;
}
function parseAddr(v) { return v.startsWith('0x') ? parseInt(v,16) : parseInt(v,10); }
async function readReg() {
  const id=document.getElementById('readId').value, port=document.getElementById('readPort').value;
  const addr=parseAddr(document.getElementById('readAddr').value), type=document.getElementById('readType').value;
  log('Reading ID='+id+' Addr=0x'+addr.toString(16).toUpperCase());
  try {
    const r = await fetch('/readreg?id='+id+'&port='+port+'&addr='+addr+'&type='+type);
    const d = await r.json();
    if(d.success) log('Value: '+d.value+' (0x'+d.hex+')','success');
    else log('Failed: '+(d.error||'Unknown'),'error');
  } catch(e) { log('Error: '+e,'error'); }
}
async function writeReg() {
  const id=document.getElementById('writeId').value, port=document.getElementById('writePort').value;
  const addr=parseAddr(document.getElementById('writeAddr').value), val=document.getElementById('writeValue').value;
  const type=document.getElementById('writeType').value;
  log('Writing ID='+id+' Addr=0x'+addr.toString(16).toUpperCase()+' Value='+val);
  try {
    const r = await fetch('/writereg?id='+id+'&port='+port+'&addr='+addr+'&value='+val+'&type='+type);
    const d = await r.json();
    if(d.success) log('Write OK','success');
    else log('Failed: '+(d.error||'Unknown'),'error');
  } catch(e) { log('Error: '+e,'error'); }
}
async function readTransferRegs() {
  log('=== Transfer/Ramp Registers ===');
  try {
    const r = await fetch('/readtransfer');
    const d = await r.json();
    if(d.success) {
      for(const dev of d.data) {
        log('--- '+dev.dev+' (ID:'+dev.id+') ---');
        for(const reg of dev.regs) {
          if(reg.ok) log('  '+reg.n+': '+reg.v,'success');
          else log('  '+reg.n+': FAILED','error');
        }
      }
    }
  } catch(e) { log('Error: '+e,'error'); }
}
async function readAGSRegs() {
  log('=== AGS Registers ===');
  try {
    const r = await fetch('/readags');
    const d = await r.json();
    if(d.success) {
      for(const dev of d.data) {
        log('--- '+dev.dev+' (ID:'+dev.id+') ---');
        for(const reg of dev.regs) {
          if(reg.ok) log('  '+reg.n+': '+reg.v,'success');
          else log('  '+reg.n+': FAILED','error');
        }
      }
    }
  } catch(e) { log('Error: '+e,'error'); }
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
    """Get or update configuration."""
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
                    val = max(45.0, min(60.0, float(request.args.get(param))))
                    config['mep803a'][key] = val
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
                    val = max(45.0, min(60.0, float(request.args.get(param))))
                    config['kubota'][key] = val
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
    
    if changed:
        save_config()
        log_event("Config updated")
    
    with config_lock:
        cfg_copy = copy.deepcopy(config)
    with auto_gen_lock:
        events = auto_gen_state["events"][-20:]
    
    return jsonify({"config": cfg_copy, "events": events})

@app.route('/stopgen')
def stop_gen_endpoint():
    """Graceful generator stop with ramp-down."""
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
            {"addr": 0x00C0, "name": "SwitchState"},
            {"addr": 0x01F5, "name": "AC1Delay"},
            {"addr": 0x01F6, "name": "AC2Delay"},
            {"addr": 0x016F, "name": "MaxCharge"},
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
            {"addr": 0x0054, "name": "AutoStopDCV"},
            {"addr": 0x0056, "name": "AutoStopSOC"},
            {"addr": 0x0059, "name": "StopAbsorp"},
            {"addr": 0x005A, "name": "StopFloat"},
            {"addr": 0x006B, "name": "CoolDown"},
            {"addr": 0x006C, "name": "SpinDown"}
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
        if slave_id == AGS_MEP803A_ID:
            threading.Thread(target=start_generator, args=("mep803a",), daemon=True).start()
            return "MEP-803A start initiated", 200
        else:
            threading.Thread(target=start_generator, args=("kubota",), daemon=True).start()
            return "Kubota start initiated", 200
    
    success = modbus.write_single_register_16(MODBUS_HOST, MODBUS_PORT, slave_id, REG_GENERATOR_MODE, state)
    if success:
        gen_name = "MEP-803A" if slave_id == AGS_MEP803A_ID else "Kubota"
        mode_name = {0: "OFF", 2: "AUTO"}[state]
        log_event(f"{gen_name} → {mode_name}")
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
logger.info("Solar Dashboard V2.2 Starting...")
load_config()

poll_thread = threading.Thread(target=poll_modbus, daemon=True)
poll_thread.start()
logger.info("Modbus polling started")
with config_lock:
    logger.info(f"Auto-gen: {'ENABLED' if config['autoGenEnabled'] else 'DISABLED'}")
logger.info("=" * 50)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
