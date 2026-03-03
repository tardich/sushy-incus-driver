#!/usr/bin/env bash

set -euo pipefail

# configfile default path
CONF_PATH="${SUSHY_EMULATOR_CONFIG_PATH:-/etc/sushy/sushy-emulator.conf}"
HOST_BIND="${SUSHY_EMULATOR_HOST:-::}" # IPv6 all (and IPv4 via dual-stack)
PORT="${SUSHY_EMULATOR_PORT:-8000}"

# Start with config file if existing
if [[ -f "${CONF_PATH}" ]]; then
	echo "[entrypoint Using config: ${CONF_PATH}"
	exec sushy-emulator --config "${CONF_PATH}" -i "${HOST_BIND}" -p "${PORT}" "$@"
else
	echo "[entrypoint] No config file found, starting with defaults"
	exec sushy-emulator -i "${HOST_BIND}" -p "${PORT}" "$@"
fi
