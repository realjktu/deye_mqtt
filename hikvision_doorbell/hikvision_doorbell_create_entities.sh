/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/doorbell/call_state/config" -f mqtt_discovery_doorbell.json
