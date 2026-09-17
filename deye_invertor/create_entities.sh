#!/bin/sh
# Publishes the Home Assistant MQTT discovery configs for the inverter.
#
# The configs are published retained so Home Assistant can rebuild the entities
# after a restart. Without them, HA has nothing to create the entities from.
#
# Run with the project environment loaded:
#   cd deye_invertor && . ../.env && ./create_entities.sh
#
# Override the client binary if it is not on PATH, e.g.
#   MOSQUITTO_PUB=/opt/homebrew/opt/mosquitto/bin/mosquitto_pub ./create_entities.sh
set -eu

cd "$(dirname "$0")"

MOSQUITTO_PUB="${MOSQUITTO_PUB:-mosquitto_pub}"
MQTT_PORT="${MQTT_PORT:-1883}"

for var in MQTT_HOST MQTT_USER MQTT_PASSWORD; do
  eval "value=\${$var:-}"
  if [ -z "$value" ]; then
    echo "$var is not set. Load the project environment first: . ../.env" >&2
    exit 1
  fi
done

# publish <platform> <entity> <config file>
publish() {
  platform=$1
  entity=$2
  config=$3
  topic="homeassistant/${platform}/invertor/${entity}/config"
  echo "publishing ${topic}"
  "$MOSQUITTO_PUB" -r -h "$MQTT_HOST" -p "$MQTT_PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
    -t "$topic" -f "$config"
}

publish sensor battery_temperature      battery_temperature_discovery.json
publish sensor battery_voltage          mqtt_discovery_battery_voltage.json
publish sensor battery_soc              mqtt_discovery_battery_soc.json
publish sensor battery_charge_limit     mqtt_discovery_battery_charge_limit.json
publish sensor battery_dischage_limit   mqtt_discovery_battery_dischage_limit.json
publish sensor grid_frequency           mqtt_discovery_grid_frequency.json
publish sensor grid_voltage             mqtt_discovery_grid_voltage.json
publish sensor grid_power               mqtt_discovery.json
publish sensor load_power               mqtt_discovery_load_power.json
publish sensor overall_state            mqtt_discovery_overall_state.json
publish sensor fault_state              mqtt_discovery_fault_state.json

# Grid connection is an ON/OFF value, so it belongs to the binary_sensor platform.
publish binary_sensor grid_connection   mqtt_discovery_grid_connection.json

echo "done"
