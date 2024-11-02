from pysolarmanv5 import PySolarmanV5, V5FrameError
import umodbus.exceptions
import struct
import os

stick_logger_ip = os.environ.get("DEYE_LOGGER_IP",'')
stick_logger_serial = int(os.environ.get("DEYE_LOGGER_SERIAL",''))

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
    0: 'Disconnected',
    1: 'Connected'
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


def main():
    modbus = PySolarmanV5(
        stick_logger_ip, stick_logger_serial, port=8899, mb_slave_id=1, verbose=False
        )

    for key, val in registers.items():
        res = modbus.read_holding_registers(register_addr=val['id'], quantity=1)
        if key == 'battery_temperature':
            print(f'{key}: {res[0]*val['scale']-100} {val['units']}')
        else:
            print(f'{key}: {res[0]*val['scale']} {val['units']}')

    res = modbus.read_holding_registers(register_addr=59, quantity=1)
    print(f'Overall state: {inverter_state[res[0]]}') 
    res = modbus.read_holding_registers(register_addr=103, quantity=4)
    print(f'Fault state: {res} data: {reg_to_value(res)}') 
    res = modbus.read_holding_registers(register_addr=194, quantity=1)
    print(f'Connection to grid: {grid_connection_status[res[0]]}')


if __name__ == "__main__":
    main()
