from pysolarmanv5 import PySolarmanV5, V5FrameError
import umodbus.exceptions
import struct
import time
import paho.mqtt.client as mqtt
import json
import os

stick_logger_ip = os.environ.get("DEYE_LOGGER_IP",'')
stick_logger_serial = int(os.environ.get("DEYE_LOGGER_SERIAL",''))
mqtt_host = os.environ.get("MQTT_HOST",'')
mqtt_user = os.environ.get("MQTT_USER",'')
mqtt_password = os.environ.get("MQTT_PASSWORD",'')
sleep_time = 60

def on_publish(client, userdata, mid, reason_code, properties):
    # reason_code and properties will only be present in MQTTv5. It's always unset in MQTTv3
    try:
        userdata.remove(mid)
    except KeyError:
        print("on_publish() is called with a mid not present in unacked_publish")
        print("This is due to an unavoidable race-condition:")
        print("* publish() return the mid of the message sent.")
        print("* mid from publish() is added to unacked_publish by the main thread")
        print("* on_publish() is called by the loop_start thread")
        print("While unlikely (because on_publish() will be called after a network round-trip),")
        print(" this is a race-condition that COULD happen")
        print("")
        print("The best solution to avoid race-condition is using the msg_info from publish()")
        print("We could also try using a list of acknowledged mid rather than removing from pending list,")
        print("but remember that mid could be re-used !")


def send_by_mqtt(topic, message):
    unacked_publish = set()
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.password = mqtt_password
    mqttc.username = mqtt_user
    mqttc.host = mqtt_host
    mqttc.on_publish = on_publish

    mqttc.user_data_set(unacked_publish)
    mqttc.connect(mqtt_host)
    mqttc.loop_start()

    # Our application produce some messages
    msg_info = mqttc.publish(topic, message, qos=1)
    unacked_publish.add(msg_info.mid)
    # Wait for all message to be published
    while len(unacked_publish):
        time.sleep(0.1)
    # Due to race-condition described above, the following way to wait for all publish is safer
    msg_info.wait_for_publish()

    mqttc.disconnect()
    mqttc.loop_stop()


# https://github.com/kellerza/sunsynk/blob/main/src/sunsynk/definitions/single_phase.py
registers={
    'battery_temperature':{
    'id': 182,
    'scale': 0.1,
    'units': 'C'
    },
    'battery_voltage':{
    'id': 183,
    'scale': 0.01,
    'units': 'V'
    },
   'battery_soc':{
    'id': 184,
    'scale': 1,
    'units': '%'
    },
#   'battery_power':{
#    'id': 190,
#    'scale': 1
#    },
#   'battery_current':{
#    'id': 191,
#    'scale': 0.01
#    },
   'battery_charge_limit':{
    'id': 314,
    'scale': 1,
    'units': 'A'
    },
   'battery_dischage_limit':{
    'id': 315,
    'scale': 1,
    'units': 'A'
    },

   'grid_frequency':{
    'id': 79,
    'scale': 0.01,
    'units': 'Hz' 
    },
   'grid_power':{
    'id': 169,
    'scale': -1,
    'units': 'W' 
    },
   'grid_ld_power':{
    'id': 167,
    'scale': -1,
    'units': 'W' 
    },
   'grid_l2_power':{
    'id': 168,
    'scale': -1,
    'units': 'W' 
    },
   'grid_voltage':{
    'id': 150,
    'scale': 0.1,
    'units': 'V' 
    },
   'grid_current':{
    'id': 150,
    'scale': 0.1,
    'units': 'V' 
    },
   'grid_ct_power':{
    'id': 172,
    'scale': -1,
    'units': 'W' 
    },

   'load_power':{
    'id': 178,
    'scale': 1,
    'units': 'W' 
    },  
    'load_l1_power':{
    'id': 176,
    'scale': 1,
    'units': 'W' 
    },
   'load_l2_power':{
    'id': 177,
    'scale': 1,
    'units': 'W' 
    },
   'load_frequency':{
    'id': 192,
    'scale': 0.01,
    'units': 'Hz' 
    }
}

grid_connection_status = {
    0: 'OFF',
    1: 'ON'
    }

inverter_state = {
            0: "standby",
            1: "selfcheck",
            2: "ok",
            3: "alarm",
            4: "fault",
            5: "activating"
            }

def reg_to_value(regs):
    """Decode Inverter faults."""
    faults = {
        13: "Working mode change",
        18: "AC over current",
        20: "DC over current",
        23: "F23 AC leak current or transient over current",
        24: "F24 DC insulation impedance",
        26: "F26 DC busbar imbalanced",
        29: "Parallel comms cable",
        35: "No AC grid",
        42: "AC line low voltage",
        47: "AC freq high/low",
        56: "DC busbar voltage low",
        63: "ARC fault",
        64: "Heat sink tempfailure",
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


def get_data():
    modbus = PySolarmanV5(
        stick_logger_ip, stick_logger_serial, port=8899, mb_slave_id=1, verbose=False
        )
    output = {}
    for key, val in registers.items():
        res = modbus.read_holding_registers(register_addr=val['id'], quantity=1)
        if key == 'battery_temperature':
            print(f'{key}: {res[0]*val['scale']-100} {val['units']}')
            output[key] = res[0]*val['scale']-100
        else:
            if key == 'grid_voltage' or key == 'grid_current':
                print(f'{key}: {round(res[0]*val['scale'])} {val['units']}')
                output[key] = round(res[0]*val['scale'])
            else:
                print(f'{key}: {res[0]*val['scale']} {val['units']}')
                output[key] = res[0]*val['scale']

    res = modbus.read_holding_registers(register_addr=59, quantity=1)
    print(f'Overall state: {inverter_state[res[0]]}')
    output['overall_state'] = inverter_state[res[0]]
    res = modbus.read_holding_registers(register_addr=103, quantity=4)
    print(f'Fault state: {res} data: {reg_to_value(res)}')
    res = modbus.read_holding_registers(register_addr=194, quantity=1)
    print(f'Connection to grid: {grid_connection_status[res[0]]}')
    output['grid_connection'] = grid_connection_status[res[0]]

    message = json.dumps(output)
    print(message)
    send_by_mqtt('homeassistant/sensor/invertor/state', message)

def main():
    print(f'Connecting to {stick_logger_ip} {stick_logger_serial}')
    while True:
        get_data()
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
