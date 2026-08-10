#!/usr/bin/env bash
# One command to run the whole system on this machine.
#
#   ./run.sh                 # broker + twins + orchestrator + telemetry + audit
#   ./run.sh --interval 0.5  # slower playback (default 0.05 = ~1500x)
#   ./run.sh --once          # one degradation cycle instead of looping
#   ./run.sh --stop          # stop the broker and exit
#
# A fresh clone works: the credentials the broker needs do not exist in git,
# so they are generated on first run. Nothing here is interactive and nothing
# needs sudo.

set -euo pipefail
cd "$(dirname "$0")"

INTERVAL="0.05"
SIM_MODE="--loop"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    --once)     SIM_MODE="--anomaly"; shift ;;
    --stop)     docker compose down; echo "[run] broker stopped"; exit 0 ;;
    -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
    *)          echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

export MQTT_HOST="${MQTT_HOST:-localhost}"
export MQTT_PORT="${MQTT_PORT:-1883}"
export MQTT_TLS="${MQTT_TLS:-false}"

# ---------------------------------------------------------------- 1. secrets
# Idempotent: generates a CA, a server certificate and one password per
# service identity on first run, then re-stages the broker's runtime config.
./mosquitto/make_credentials.sh

# ----------------------------------------------------------------- 2. broker
echo "[run] starting broker"
docker compose up -d >/dev/null

# Wait for it to actually accept connections. `docker compose up` returns as
# soon as the container starts, which is earlier than mosquitto being ready —
# starting the twins into that gap makes them burn their connect retries.
echo -n "[run] waiting for broker"
for i in $(seq 1 30); do
  if python3 - "$MQTT_HOST" "$MQTT_PORT" <<'PY' 2>/dev/null; then
import socket, sys
s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1)
s.close()
PY
    echo " ready"
    break
  fi
  echo -n "."
  sleep 0.5
  if [[ $i -eq 30 ]]; then echo " TIMED OUT"; docker compose logs --tail 20; exit 1; fi
done

# ------------------------------------------------------------ 3. credentials
# Every service authenticates as itself. main.py runs six twins plus the
# orchestrator in one process, so these have to be per-service variables —
# a single shared credential would collapse the least-privilege ACL into one
# account holding the union of all seven permissions.
eval "$(python3 - <<'PY'
import json
for user, pw in json.load(open("mosquitto/credentials.json")).items():
    key = user.upper().replace("-", "_")
    print(f"export MQTT_USERNAME_{key}='{user}'")
    print(f"export MQTT_PASSWORD_{key}='{pw}'")
PY
)"

# --------------------------------------------------------------- 4. processes
mkdir -p logs
PIDS=()

cleanup() {
  echo
  echo "[run] stopping components"
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "[run] components stopped. The broker is still up — ./run.sh --stop to stop it."
}
# EXIT only: bash runs the EXIT trap after INT/TERM anyway, so trapping all
# three ran cleanup three times on Ctrl-C.
trap cleanup EXIT

echo "[run] starting audit sink"
python3 audit/audit_sink.py > logs/audit_sink.log 2>&1 &
PIDS+=($!)

echo "[run] starting twins + orchestrator"
python3 main.py > logs/main.log 2>&1 &
PIDS+=($!)

# The twins need to be subscribed before telemetry starts, or the first
# messages land with nobody listening.
sleep 3

echo "[run] starting occupancy publisher"
python3 occupancy_publisher.py --interval "$INTERVAL" > logs/occupancy.log 2>&1 &
PIDS+=($!)

echo "[run] starting sensor simulator ($SIM_MODE, interval ${INTERVAL}s)"
python3 sensor_simulator.py --interval "$INTERVAL" $SIM_MODE > logs/simulator.log 2>&1 &
PIDS+=($!)

cat <<EOF

[run] running. Logs in logs/.
      watch predictions : python3 watch.py datacenter/predictions/CRAC-01
      audit trail       : python3 audit/audit_sink.py --show 20
      verify the chain  : python3 audit/audit_sink.py --verify
      serve the demo    : ./serve_demo.sh

      Ctrl-C to stop.
EOF

wait
