"""
Script to monitor DEYE inverter data and send it to an MQTT broker.
"""

import os
import time
import json
import logging
from pysolarmanv5 import PySolarmanV5, V5FrameError
import umodbus.exceptions
import paho.mqtt.client as mqtt

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Environment variables
STICK_LOGGER_IP = os.getenv("DEYE_LOGGER_IP", '')
STICK_LOGGER_SERIAL = int(os.getenv("DEYE_LOGGER_SERIAL", ''))
MQTT_HOST = os.getenv("MQTT_HOST", '')
MQTT_USER = os.getenv("MQTT_USER", '')
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", '')
SLEEP_TIME = int(os.getenv("SLEEP_TIME", 60))

# Register definitions for the DEYE inverter
REGISTERS = {
    'battery_temperature': {'id': 182, 'scale': 0.1, 'units': 'C'},
    'battery_voltage': {'id': 183, 'scale': 0.01, 'units': 'V'},
    'battery_soc': {'id': 184, 'scale': 1, 'units': '%'},
    'battery_charge_limit': {'id': 314, 'scale': 1, 'units': 'A'},
    'battery_discharge_limit': {'id': 315, 'scale': 1, 'units': 'A'},
    'grid_frequency': {'id': 79, 'scale': 0.01, 'units': 'Hz'},
    'grid_power': {'id': 169, 'scale': -1, 'units': 'W'},
    'grid_voltage': {'id': 150, 'scale': 0.1, 'units': 'V'},
    'load_power': {'id': 178, 'scale': 1, 'units': 'W'},
}

GRID_CONNECTION_STATUS = {0: 'OFF', 1: 'ON'}
INVERTER_STATE = {0: "standby", 1: "selfcheck", 2: "ok", 3: "alarm", 4: "fault", 5: "activating"}

def on_publish(client, userdata, mid):
    """Callback function for MQTT on_publish event."""
    userdata.discard(mid)

def setup_mqtt_client():
    """Sets up and returns an MQTT client."""
    mqtt_client = mqtt.Client()
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    mqtt_client.on_publish = on_publish
    mqtt_client.user_data_set(set())

    try:
        mqtt_client.connect(MQTT_HOST)
        logging.info("Connected to MQTT broker.")
    except Exception as error:
        logging.error("Failed to connect to MQTT broker: %s", error)
        exit(1)

    return mqtt_client

def send_by_mqtt(mqtt_client, topic, message):
    """Publishes a message to an MQTT topic."""
    try:
        msg_info = mqtt_client.publish(topic, message, qos=1)
        msg_info.wait_for_publish()
        logging.info("Data sent to MQTT broker.")
    except Exception as error:
        logging.error("Failed to send data to MQTT broker: %s", error)

def reg_to_value(regs):
    """Decode inverter faults into readable messages."""
    faults = {
        13: "Working mode change",
        18: "AC over current",
        20: "DC over current",
        23: "AC leak current or transient over current",
        24: "DC insulation impedance",
        26: "DC busbar imbalance",
        29: "Parallel comms cable",
        35: "No AC grid",
        42: "AC line low voltage",
        47: "AC freq high/low",
        56: "DC busbar voltage low",
        63: "ARC fault",
        64: "Heat sink temp failure",
    }
    err = []
    off = 0
    for b16 in regs:
        for bit in range(16):
            msk = 1 << bit
            if msk & b16:
                msg = f"F{bit+off+1:02} " + faults.get(off + msk, "")
                err.append(msg.strip())
        off += 16
    return ", ".join(err)

def get_data(modbus):
    """Fetches and processes data from the inverter."""
    output = {}
    for key, val in REGISTERS.items():
        try:
            res = modbus.read_holding_registers(register_addr=val['id'], quantity=1)
            value = res[0] * val['scale'] - 100 if key == 'battery_temperature' else res[0] * val['scale']
            output[key] = round(value, 2) if key in ['grid_voltage', 'grid_current'] else value
            logging.info("%s: %s %s", key, output[key], val['units'])
        except (V5FrameError, umodbus.exceptions.ModbusError) as error:
            logging.error("Error reading %s: %s", key, error)

    try:
        state_res = modbus.read_holding_registers(register_addr=59, quantity=1)
        output['overall_state'] = INVERTER_STATE.get(state_res[0], "unknown")
        logging.info("Overall state: %s", output['overall_state'])

        fault_res = modbus.read_holding_registers(register_addr=103, quantity=4)
        output['fault_state'] = reg_to_value(fault_res)
        logging.info("Fault state: %s", output['fault_state'])

        grid_res = modbus.read_holding_registers(register_addr=194, quantity=1)
        output['grid_connection'] = GRID_CONNECTION_STATUS.get(grid_res[0], "unknown")
        logging.info("Connection to grid: %s", output['grid_connection'])
    except (V5FrameError, umodbus.exceptions.ModbusError) as error:
        logging.error("Error reading additional states: %s", error)

    return json.dumps(output)

def main():
    """Main loop to collect inverter data and publish to MQTT."""
    logging.info("Connecting to inverter at %s with serial %s", STICK_LOGGER_IP, STICK_LOGGER_SERIAL)
    modbus = PySolarmanV5(STICK_LOGGER_IP, STICK_LOGGER_SERIAL, port=8899, mb_slave_id=1, verbose=False)
    mqtt_client = setup_mqtt_client()
    mqtt_client.loop_start()

    while True:
        message = get_data(modbus)
        if message:
            send_by_mqtt(mqtt_client, 'homeassistant/sensor/inverter/state', message)
        time.sleep(SLEEP_TIME)

    mqtt_client.loop_stop()
    mqtt_client.disconnect()

if __name__ == "__main__":
    main()
