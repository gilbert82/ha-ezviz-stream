#!/usr/bin/with-contenv bashio

# Read configuration from /data/options.json
# bashio is available in HA add-on containers and provides helper functions

bashio::log.info "Starting Ezviz Camera Proxy..."

# Export config values as environment variables for the Python app
export EZVIZ_USERNAME="$(bashio::config 'ezviz_username')"
export EZVIZ_PASSWORD="$(bashio::config 'ezviz_password')"
export EZVIZ_REGION="$(bashio::config 'ezviz_region')"
export CAMERA_SERIAL="$(bashio::config 'camera_serial')"
export CAMERA_PASSWORD="$(bashio::config 'camera_password')"
export SNAPSHOT_INTERVAL="$(bashio::config 'snapshot_interval')"
export ENABLE_MQTT_EVENTS="$(bashio::config 'enable_mqtt_events')"

# HA Supervisor / Ingress environment
export INGRESS_ENTRY="$(bashio::addon.ingress_entry 2>/dev/null || echo '/')"
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"
export HA_SUPERVISOR_URL="http://supervisor"

# Data directory for token caching and snapshots
export DATA_PATH="/data"
mkdir -p /data/snapshots

bashio::log.info "Camera serial: ${CAMERA_SERIAL}"
bashio::log.info "Ezviz region: ${EZVIZ_REGION}"
bashio::log.info "Snapshot interval: ${SNAPSHOT_INTERVAL}s"
bashio::log.info "Ingress entry: ${INGRESS_ENTRY}"

# Start Flask application
exec python3 /app/server.py
