/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_charge_limit/config" -f mqtt_discovery_battery_charge_limit.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_dischage_limit/config" -f mqtt_discovery_battery_dischage_limit.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_soc/config" -f mqtt_discovery_battery_soc.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_voltage/config" -f mqtt_discovery_battery_voltage.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/grid_frequency/config" -f mqtt_discovery_grid_frequency.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/grid_voltage/config" -f mqtt_discovery_grid_voltage.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/binary_sensor/invertor/grid_connection/config" -f mqtt_discovery_grid_connection.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_voltage/config" -f mqtt_discovery_battery_voltage.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/grid_voltage/config" -f mqtt_discovery_grid_voltage.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/grid_frequency/config" -f mqtt_discovery_grid_frequency.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_dischage_limit/config" -f mqtt_discovery_battery_dischage_limit.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_charge_limit/config" -f mqtt_discovery_battery_charge_limit.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/binary_sensor/invertor/grid_connection/config" -f mqtt_discovery_grid_connection.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/binary_sensor/invertor/overall_state/config" -f mqtt_discovery_overall_state.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/overall_state/config" -f mqtt_discovery_overall_state.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/battery_temperature/config" -f battery_temperature_discovery.json
/opt/homebrew/opt/mosquitto/bin/mosquitto_pub -r -h ${MQTT_HOST} -p 1883 -u ${MQTT_USER} -P ${MQTT_PASSWORD} -t "homeassistant/sensor/invertor/grid_power/config" -f mqtt_discovery.json