import requests
from requests.auth import HTTPDigestAuth
import struct
import time
import paho.mqtt.client as mqtt
import json
import os

mqtt_host = os.environ.get("MQTT_HOST",'')
mqtt_user = os.environ.get("MQTT_USER",'')
mqtt_password = os.environ.get("MQTT_PASSWORD",'')
hikvision_doorbell_host = os.environ.get("HIKVISION_DOORBELL_HOST",'')
hikvision_doorbell_user = os.environ.get("HIKVISION_DOORBELL_USER",'')
hikvision_doorbell_password = os.environ.get("HIKVISION_DOORBELL_PASSWORD",'')

sleep_time = 3

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
	output = {}
	url = f'http://{hikvision_doorbell_host}/ISAPI/VideoIntercom/callStatus?format=json'
	response = requests.get(url, auth=HTTPDigestAuth(hikvision_doorbell_user, hikvision_doorbell_password))
	#print(response)
	data = json.loads(response.text)
	output['call_state'] = data['CallStatus']['status']
	message = json.dumps(output)
	print(message)
	send_by_mqtt('homeassistant/sensor/doorbell/state', message)


def main():
    while True:
        get_data()
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()
