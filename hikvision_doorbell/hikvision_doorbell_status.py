"""Script to monitor Hikvision Doorbell and send call status to MQTT broker."""

import os
import time
import json
import logging
import requests
from requests.auth import HTTPDigestAuth
from requests.exceptions import RequestException
import paho.mqtt.client as mqtt

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Environment variables
MQTT_HOST = os.getenv("MQTT_HOST", '')
MQTT_USER = os.getenv("MQTT_USER", '')
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", '')
HIKVISION_DOORBELL_HOST = os.getenv("HIKVISION_DOORBELL_HOST", '')
HIKVISION_DOORBELL_USER = os.getenv("HIKVISION_DOORBELL_USER", '')
HIKVISION_DOORBELL_PASSWORD = os.getenv("HIKVISION_DOORBELL_PASSWORD", '')
SLEEP_TIME = int(os.getenv("SLEEP_TIME", 3))

# Check for missing configurations
REQUIRED_CONFIGS = [MQTT_HOST, MQTT_USER, MQTT_PASSWORD, HIKVISION_DOORBELL_HOST, HIKVISION_DOORBELL_USER, HIKVISION_DOORBELL_PASSWORD]
if not all(REQUIRED_CONFIGS):
    logging.error("One or more required environment variables are missing.")
    exit(1)

def on_publish(client, userdata, mid):
    """Callback function for MQTT on_publish event."""
    userdata.discard(mid)

def setup_mqtt_client():
    """Setup and return an MQTT client."""
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

def send_data(mqtt_client, topic, message):
    """Send data to MQTT broker."""
    try:
        msg_info = mqtt_client.publish(topic, message, qos=1)
        msg_info.wait_for_publish()
        logging.info("Data sent to MQTT broker.")
    except Exception as error:
        logging.error("Failed to send data to MQTT broker: %s", error)

def get_doorbell_status():
    """Fetch the current call status from Hikvision Doorbell."""
    url = f'http://{HIKVISION_DOORBELL_HOST}/ISAPI/VideoIntercom/callStatus?format=json'
    try:
        response = requests.get(url, auth=HTTPDigestAuth(HIKVISION_DOORBELL_USER, HIKVISION_DOORBELL_PASSWORD), timeout=10)
        response.raise_for_status()
        data = response.json()
        call_status = data['CallStatus']['status']
        logging.info("Call status fetched: %s", call_status)
        return {'call_state': call_status}
    except RequestException as error:
        logging.error("Failed to fetch call status from doorbell: %s", error)
        return {}

def main():
    """Main loop to fetch doorbell status and send it to MQTT."""
    mqtt_client = setup_mqtt_client()
    mqtt_client.loop_start()

    while True:
        status_data = get_doorbell_status()
        if status_data:
            message = json.dumps(status_data)
            send_data(mqtt_client, 'homeassistant/sensor/doorbell/state', message)
        else:
            logging.error("No data to send.")

        time.sleep(SLEEP_TIME)

    mqtt_client.loop_stop()
    mqtt_client.disconnect()

if __name__ == "__main__":
    main()
