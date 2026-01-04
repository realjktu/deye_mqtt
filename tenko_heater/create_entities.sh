/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/heater1_state/config" -f mqtt_discovery_heater1_state.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/heater2_state/config" -f mqtt_discovery_heater2_state.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/input/config" -f mqtt_discovery_heater_input.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/output/config" -f mqtt_discovery_heater_output.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/pressure/config" -f mqtt_discovery_heater_pressure.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/sensor/config" -f mqtt_discovery_heater_sensor.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/binary_sensor/heater/error/config" -f mqtt_discovery_heater_error.json


/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/modulation_state/config" -f mqtt_discovery_heater_modulation_state.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/heater/modulation_value/config" -f mqtt_discovery_heater_modulation_value.json
