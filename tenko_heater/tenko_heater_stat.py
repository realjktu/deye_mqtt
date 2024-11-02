import sys
import requests
import struct
import time
import paho.mqtt.client as mqtt
import json
import os


mqtt_host = os.environ.get("MQTT_HOST",'')
mqtt_user = os.environ.get("MQTT_USER",'')
mqtt_password = os.environ.get("MQTT_PASSWORD",'')
tenko_heater_host = os.environ.get("TENKO_HEATER_HOST",'')
tenko_heater_user = os.environ.get("TENKO_HEATER_USER",'')
tenko_heater_password = os.environ.get("TENKO_HEATER_PASSWORD",'')

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

def get_data():
    payload = {'login': tenko_heater_user, 'password': tenko_heater_password}
    r = requests.post(f'http://{tenko_heater_host}/api/v1/auth', data=payload)
    print(r.text)
    res = r.json()
    print(res['token'])
    headers = {'Authorization': 'Bearer '+res['token']}
    r = requests.get(f'http://{tenko_heater_host}/api/v1/total_state', headers=headers)
    print(r.text)
    res = r.json()
    '''
        WFT - input
        RWFT - output
        AT - sensor
        PRS - presure
    '''
    output = {'input_temperature': res['WFT'],
              'output_temperature': res['RWFT'],
              'sensor_temperature': res['AT'],
              'pressure': res['PRS'],
              'heater1_state': res['HE1'],
              'heater2_state': res['HE2']
              }
    if res['ERR']['type_total']!=0 or res['ERR']['type_time']!=0:
        output['error'] = 'OFF'
    else:
        output['error'] = 'ON'
    message = json.dumps(output)
    print(message)
    send_by_mqtt('homeassistant/sensor/heater/state', message)
    print('done')

def main():
    while True:
        get_data()
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
