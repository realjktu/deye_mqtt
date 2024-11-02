from pysolarmanv5 import PySolarmanV5, V5FrameError
from gpiozero import LED
import paramiko
import umodbus.exceptions
import struct
import time
import json
import os

stick_logger_ip = os.environ.get("DEYE_LOGGER_IP",'')
stick_logger_serial = int(os.environ.get("DEYE_LOGGER_SERIAL",''))
shutdown_host = os.environ.get("SHUTDOWN_HOST",'')
shutdown_host_user = os.environ.get("SHUTDOWN_HOST_USER",'')

# https://github.com/kellerza/sunsynk/blob/main/src/sunsynk/definitions/single_phase.py
registers={
   'battery_soc':{
    'id': 184,
    'scale': 1,
    'units': '%'
    }
}

grid_connection_status = {
    0: 'OFF',
    1: 'ON'
    }

def powersave():
    client = paramiko.client.SSHClient()
    client.load_system_host_keys()
    client.connect(shutdown_host, username=shutdown_host_user)
    stdin, stdout, stderr = client.exec_command('sudo poweroff')
    print(stdout.read())
    client.close()
    print("waiting shutdown")
    time.sleep(30)
    led = LED(26)
    led.on()
    time.sleep(2)
    led.off()


def get_data():
    modbus = PySolarmanV5(
        stick_logger_ip, stick_logger_serial, port=8899, mb_slave_id=1, verbose=False
        )
    output = {}
    for key, val in registers.items():
        res = modbus.read_holding_registers(register_addr=val['id'], quantity=1)
        #print(f'{key}: {res[0]*val['scale']} {val['units']}')
        output[key] = res[0]*val['scale']

    res = modbus.read_holding_registers(register_addr=194, quantity=1)
    print(f'Connection to grid: {grid_connection_status[res[0]]}')
    output['grid_connection'] = grid_connection_status[res[0]]
    print(output)
    if output['grid_connection']=='OFF' and int(output['battery_soc'])<=70:
    #if output['grid_connection']=='ON' and int(output['battery_soc'])<=99:
        print('Move to power safe mode')
        powersave()


def main():
    print(f'Connecting to {stick_logger_ip} {stick_logger_serial}')
    get_data()

if __name__ == "__main__":
    main()
