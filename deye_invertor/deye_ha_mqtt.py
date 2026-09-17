"""
Script to monitor DEYE inverter data and send it to an MQTT broker.
"""

import os
import queue
import signal
import struct
import time
import json
import logging
import threading
from pysolarmanv5 import PySolarmanV5, V5FrameError, NoSocketAvailableError
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

# Reliability tuning.
# The stick logger regularly drops requests outright. Healthy replies come back in
# roughly half a second, so a short timeout detects a lost reply quickly instead of
# blocking the poll cycle on the library default of 60s.
SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", 3))
READ_RETRIES = int(os.getenv("READ_RETRIES", 3))
# Small pause between requests: the logger chokes on back-to-back requests.
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", 0.2))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", 0.5))
# Upper bound for one poll cycle. Without it, a logger that accepts connections
# but never answers can stretch a cycle to timeout * retries * register count.
POLL_DEADLINE = float(os.getenv("POLL_DEADLINE", max(30.0, SLEEP_TIME * 0.75)))
MQTT_PUBLISH_TIMEOUT = float(os.getenv("MQTT_PUBLISH_TIMEOUT", 10))
# Must match "state_topic" in the discovery configs under deye_invertor/*.json,
# which all use the "invertor" spelling. Renaming this breaks every HA entity.
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "homeassistant/sensor/invertor/state")

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
# Home Assistant reads the literal payload "None" as an unknown state. Publishing
# anything else for an unmapped code breaks the enum options list on the HA side.
UNKNOWN_STATE = "None"

# Errors that mean "this exchange failed", not "the inverter rejected the request".
# queue.Empty is what pysolarmanv5 raises when the logger never answers,
# TimeoutError/OSError cover socket level failures, V5FrameError covers a corrupt
# or out-of-sequence reply, struct.error covers a truncated payload.
TRANSPORT_ERRORS = (
    queue.Empty,
    TimeoutError,
    OSError,
    V5FrameError,
    NoSocketAvailableError,
    struct.error,
)

# Set when the process should stop, so sleeps can be interrupted.
SHUTDOWN = threading.Event()


class InverterReadError(Exception):
    """Raised when a register could not be read after all retries."""


def _release_queue(data_queue):
    """Releases the semaphores and feeder thread behind a multiprocessing queue."""
    data_queue.cancel_join_thread()
    data_queue.close()


def _release_selector(poll):
    """Releases the selector's underlying poll object."""
    poll.close()


class InverterClient:
    """Owns the PySolarmanV5 connection and rebuilds it when it goes bad.

    A timed out request leaves the pysolarmanv5 receive queue in a state where a
    late reply can be handed to the *next* request, which then either raises a
    sequence number error or returns another register's value. Dropping the
    connection after a transport failure is the only reliable way to resync.
    """

    def __init__(self, address, serial):
        self.address = address
        self.serial = serial
        self._modbus = None

    def _connect(self):
        self.close()
        logging.info("Connecting to inverter at %s with serial %s", self.address, self.serial)
        # auto_reconnect is deliberately off: this class handles reconnects, and
        # letting the library also reconnect in its reader thread can resurrect a
        # connection we just closed, leaking a thread and a socket each time.
        self._modbus = PySolarmanV5(
            self.address,
            self.serial,
            port=8899,
            mb_slave_id=1,
            socket_timeout=SOCKET_TIMEOUT,
            v5_error_correction=True,
            auto_reconnect=False,
            verbose=False,
        )

    def close(self):
        """Closes the connection, ignoring any teardown errors."""
        modbus, self._modbus = self._modbus, None
        if modbus is None:
            return
        try:
            modbus.disconnect()
        except Exception:  # pylint: disable=broad-exception-caught
            logging.debug("Ignoring error while disconnecting from inverter", exc_info=True)
        # disconnect() closes the socket but leaves the multiprocessing queue and
        # the poll selector behind. Since this process reconnects on every failed
        # read, those would pile up as leaked semaphores and file descriptors.
        for attr, release in (("_data_queue", _release_queue), ("_poll", _release_selector)):
            obj = getattr(modbus, attr, None)
            if obj is None:
                continue
            try:
                release(obj)
            except Exception:  # pylint: disable=broad-exception-caught
                logging.debug("Ignoring error while releasing %s", attr, exc_info=True)

    def read(self, register_addr, quantity=1):
        """Reads holding registers, retrying on transport failures.

        :raises InverterReadError: If every attempt failed.
        :raises umodbus.exceptions.ModbusError: If the inverter rejected the request.
        """
        last_error = None
        for attempt in range(1, READ_RETRIES + 1):
            try:
                if self._modbus is None:
                    self._connect()
                return self._modbus.read_holding_registers(
                    register_addr=register_addr, quantity=quantity
                )
            except umodbus.exceptions.ModbusError:
                # The exchange worked, the inverter refused it. Retrying is pointless
                # and the connection is still healthy, so keep it.
                raise
            except TRANSPORT_ERRORS as error:
                last_error = error
                logging.warning(
                    "Read of register %s failed (attempt %s/%s): %s: %s",
                    register_addr, attempt, READ_RETRIES, type(error).__name__, error,
                )
                self.close()
                if attempt < READ_RETRIES and not SHUTDOWN.is_set():
                    SHUTDOWN.wait(RETRY_DELAY)

        raise InverterReadError(
            f"register {register_addr} unreadable after {READ_RETRIES} attempts: {last_error}"
        ) from last_error


def on_connect(client, userdata, flags, reason_code, properties):
    """Callback function for MQTT on_connect event."""
    if reason_code == 0:
        logging.info("Connected to MQTT broker.")
    else:
        logging.error("MQTT connection failed: %s", reason_code)


def on_disconnect(client, userdata, flags, reason_code, properties):
    """Callback function for MQTT on_disconnect event."""
    if reason_code != 0:
        logging.warning("Unexpected MQTT disconnect (%s). Client will reconnect.", reason_code)


def on_publish(client, userdata, mid, reason_code, properties):
    """Callback function for MQTT on_publish event."""
    userdata.discard(mid)


def setup_mqtt_client():
    """Sets up and returns an MQTT client."""
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_publish = on_publish
    mqtt_client.user_data_set(set())
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

    try:
        mqtt_client.connect(MQTT_HOST)
    except Exception as error:
        logging.error("Failed to connect to MQTT broker: %s", error)
        exit(1)

    return mqtt_client


def send_by_mqtt(mqtt_client, topic, message):
    """Publishes a message to an MQTT topic."""
    try:
        msg_info = mqtt_client.publish(topic, message, qos=1)
        # Never wait without a timeout here: if the broker link is down,
        # wait_for_publish() blocks forever and the poll loop stops.
        msg_info.wait_for_publish(timeout=MQTT_PUBLISH_TIMEOUT)
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


def read_register(client, name, register_addr, quantity=1, deadline=None):
    """Reads a register, logging and swallowing failures.

    :return: Register values, or None if the register could not be read.
    """
    if deadline is not None and time.monotonic() > deadline:
        logging.warning("Poll budget exhausted, skipping %s.", name)
        return None
    try:
        return client.read(register_addr, quantity)
    except (InverterReadError, umodbus.exceptions.ModbusError) as error:
        logging.error("Error reading %s: %s", name, error)
        return None
    finally:
        if not SHUTDOWN.is_set():
            SHUTDOWN.wait(REQUEST_DELAY)


def get_data(client):
    """Fetches and processes data from the inverter.

    :return: Dict of the values that were read. Missing values are omitted
        instead of being published as bogus readings.
    """
    deadline = time.monotonic() + POLL_DEADLINE
    output = {}
    for key, val in REGISTERS.items():
        res = read_register(client, key, val['id'], deadline=deadline)
        if res is None:
            continue
        value = res[0] * val['scale'] - 100 if key == 'battery_temperature' else res[0] * val['scale']
        output[key] = round(value, 2)
        logging.info("%s: %s %s", key, output[key], val['units'])

    state_res = read_register(client, 'overall_state', 59, deadline=deadline)
    if state_res is not None:
        output['overall_state'] = INVERTER_STATE.get(state_res[0], UNKNOWN_STATE)
        logging.info("Overall state: %s", output['overall_state'])

    fault_res = read_register(client, 'fault_state', 103, quantity=4, deadline=deadline)
    if fault_res is not None:
        output['fault_state'] = reg_to_value(fault_res)
        logging.info("Fault state: %s", output['fault_state'])

    grid_res = read_register(client, 'grid_connection', 194, deadline=deadline)
    if grid_res is not None:
        output['grid_connection'] = GRID_CONNECTION_STATUS.get(grid_res[0], UNKNOWN_STATE)
        logging.info("Connection to grid: %s", output['grid_connection'])

    return output


def handle_signal(signum, _frame):
    """Requests a clean shutdown."""
    logging.info("Received signal %s, shutting down.", signum)
    SHUTDOWN.set()


def main():
    """Main loop to collect inverter data and publish to MQTT."""
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    client = InverterClient(STICK_LOGGER_IP, STICK_LOGGER_SERIAL)
    mqtt_client = setup_mqtt_client()
    mqtt_client.loop_start()

    try:
        while not SHUTDOWN.is_set():
            try:
                output = get_data(client)
            except Exception:  # pylint: disable=broad-exception-caught
                # An unexpected failure must not kill a long running collector.
                logging.exception("Unexpected error while polling the inverter.")
                client.close()
                output = {}

            if output:
                send_by_mqtt(mqtt_client, MQTT_TOPIC, json.dumps(output))
            else:
                logging.warning("No data read from inverter, skipping publish.")

            SHUTDOWN.wait(SLEEP_TIME)
    finally:
        client.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
