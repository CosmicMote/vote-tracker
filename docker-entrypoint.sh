#!/bin/sh
# Container entry point: on first run, start from the example config, then run the command.
set -e

config="${TRACKER_CONFIG:-/data/config.toml}"
if [ ! -f "$config" ]; then
    cp /app/config.example.toml "$config"
    echo "Created $config from config.example.toml. Adjust it on the web app's Admin tab."
fi

exec "$@"
