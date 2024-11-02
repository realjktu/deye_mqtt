"""Script to fetch data from Tenko Heater and send it to an MQTT broker."""

import os
import time
import json
import logging
import requests
from requests.exceptions import RequestException
import paho.mqtt.client as mqtt

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Environment variables
MQTT_HOST = os.getenv("MQTT_HOST", '')
MQTT_USER = os.getenv("MQTT_USER", '')
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", '')
TENKO_HEATER_HOST = os.getenv("TENKO_HEATER_HOST", '')
TENKO_HEATER_USER = os.getenv("TENKO_HEATER_USER", '')
TENKO_HEATER_PASSWORD = os.getenv("TENKO_HEATER_PASSWORD", '')
SLEEP_TIME = int(os.getenv("SLEEP_TIME", 60))

# Check for missing configurations
REQUIRED_CONFIGS = [MQTT_HOST, MQTT_USER, MQTT_PASSWORD, TENKO_HEATER_HOST, TENKO_HEATER_USER, TENKO_HEATER_PASSWORD]
if not all(REQUIRED_CONFIGS):
    logging.error("One or more required environment variables are missing.")
    exit(1)

def authenticate() -> str:
    """Authenticate to Tenko Heater and return the token."""
    payload = {'login': TENKO_HEATER_USER, 'password': TENKO_HEATER_PASSWORD}
    try:
        response = requests.post(f'http://{TENKO_HEATER_HOST}/api/v1/auth', data=payload, timeout=10)
        response.raise_for_status()
        token = response.json().get('token')
        if not token:
            logging.error("Authentication failed: No token received.")
            return ""
        logging.info("Authenticated successfully.")
        return token
    except RequestException as error:
        logging.error(f"Failed to authenticate with Tenko Heater: {error}")
        return ""

def fetch_data(token: str) -> dict:
    """Fetch data from Tenko Heater."""
    headers = {'Authorization': f'Bearer {token}'}
    try:
        response = requests.get(f'http://{TENKO_HEATER_HOST}/api/v1/total_state', headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        logging.info("Data fetched from Tenko Heater.")
        return data
    except RequestException as error:
        logging.error(f"Failed to fetch data: {error}")
        return {}

def parse_data(data: dict) -> dict:
    """Parse the received data and format it for MQTT."""
    if not data:
        return {}
    
    output = {
        'input_temperature': data.get('WFT'),
        'output_temperature': data.get('RWFT'),
        'sensor_temperature': data.get('AT'),
        'pressure': data.get('PRS'),
        'heater1_state': data.get('HE1'),
        'heater2_state': data.get('HE2'),
        'error': 'OFF' if data['ERR']['type_total'] != 0 or data['ERR']['type_time'] != 0 else 'ON'
    }
    return output

def setup_mqtt_client() -> mqtt.Client:
    """Setup and return an MQTT client."""
    mqtt_client = mqtt.Client()
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    mqtt_client.on_publish = on_publish
    mqtt_client.user_data_set(set())
    
    try:
        mqtt_client.connect(MQTT_HOST)
        logging.info("Connected to MQTT broker.")
    except Exception as e:
        logging.error(f"Failed to connect to MQTT broker: {e}")
        exit(1)
    
    return mqtt_client

def on_publish(client, userdata, mid):
    """Callback for MQTT on_publish event."""
    userdata.discard(mid)

def send_data(mqtt_client: mqtt.Client, topic: str, message: str):
    """Send data to MQTT broker."""
    try:
        msg_info = mqtt_client.publish(topic, message, qos=1)
        msg_info.wait_for_publish()
        logging.info("Data sent to MQTT broker.")
    except Exception as e:
        logging.error(f"Failed to send data to MQTT broker: {e}")

def main():
    """Main loop to authenticate, fetch, and send data."""
    mqtt_client = setup_mqtt_client()
    mqtt_client.loop_start()
    
    while True:
        token = authenticate()
        if not token:
            logging.error("Skipping data fetch due to authentication failure.")
            time.sleep(SLEEP_TIME)
            continue

        data = fetch_data(token)
        logging.info(data)
        parsed_data = parse_data(data)
        
        if parsed_data:
            message = json.dumps(parsed_data)
            send_data(mqtt_client, 'homeassistant/sensor/heater/state', message)
        else:
            logging.error("No data to send.")

        time.sleep(SLEEP_TIME)

    mqtt_client.loop_stop()
    mqtt_client.disconnect()

if __name__ == "__main__":
    main()
