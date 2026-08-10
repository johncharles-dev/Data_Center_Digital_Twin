#!/usr/bin/env bash
# Generates every local secret the broker needs: a TLS server certificate and
# one password per service identity.
#
# Nothing this script writes is ever committed — see .gitignore. A fresh
# clone has no credentials and therefore cannot connect to anybody's broker,
# which is the point.
#
# Idempotent: re-running regenerates only what is missing. Pass --force to
# rotate everything.
#
#   ./mosquitto/make_credentials.sh
#   ./mosquitto/make_credentials.sh --force
#
# mosquitto_passwd is not installed on the host (no mosquitto clients here),
# so it is run inside the eclipse-mosquitto image.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS="$HERE/certs"
PASSWD="$HERE/passwd"
CREDS="$HERE/credentials.json"
RUNTIME="$HERE/.runtime"
IMAGE="eclipse-mosquitto:2"

# The identity list IS the ACL's user list. Keep them in step: a username in
# one and not the other is either a service that cannot connect or a password
# that grants nothing.
IDENTITIES=(
  sensor-simulator
  occupancy-publisher
  twin-cooling
  twin-occupancy
  twin-energy
  twin-rack-SR-RACK-01
  twin-rack-SR-RACK-02
  twin-rack-SR-RACK-03
  orchestrator
  operator
  auditor
  viewer
)

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ $FORCE -eq 1 ]]; then
  echo "[creds] --force: removing existing secrets"
  rm -rf "$CERTS" "$PASSWD" "$CREDS" "$RUNTIME"
fi

# ---------------------------------------------------------------- TLS certs
if [[ -f "$CERTS/server.crt" && -f "$CERTS/server.key" && -f "$CERTS/ca.crt" ]]; then
  echo "[creds] TLS certificates already present, keeping them"
else
  echo "[creds] generating a local CA and server certificate"
  mkdir -p "$CERTS"

  # A private CA, not a self-signed leaf: clients can then verify the server
  # against ca.crt instead of disabling verification, which is what
  # tests/test_broker_security.py does on the 8883 listener.
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 \
    -keyout "$CERTS/ca.key" -out "$CERTS/ca.crt" \
    -subj "/CN=dcdt-local-ca" 2>/dev/null

  openssl req -newkey rsa:2048 -nodes -sha256 \
    -keyout "$CERTS/server.key" -out "$CERTS/server.csr" \
    -subj "/CN=localhost" 2>/dev/null

  # SANs matter: a cert with only a CN fails hostname verification on modern
  # TLS stacks. Cover the names the broker is actually reached by.
  cat > "$CERTS/san.cnf" <<'EOF'
subjectAltName = DNS:localhost, DNS:dcdt-broker, IP:127.0.0.1
extendedKeyUsage = serverAuth
EOF

  openssl x509 -req -in "$CERTS/server.csr" -CA "$CERTS/ca.crt" \
    -CAkey "$CERTS/ca.key" -CAcreateserial -out "$CERTS/server.crt" \
    -days 825 -sha256 -extfile "$CERTS/san.cnf" 2>/dev/null

  rm -f "$CERTS/server.csr" "$CERTS/san.cnf"
  chmod 0644 "$CERTS"/*.crt
  chmod 0640 "$CERTS"/*.key
  echo "[creds] certificates written to mosquitto/certs/"
fi

# ------------------------------------------------------------ passwords
if [[ -f "$PASSWD" && -f "$CREDS" ]]; then
  echo "[creds] password file already present, keeping it"
else
  echo "[creds] generating one password per service identity"

  # Build the JSON and the mosquitto_passwd argument list together so the two
  # can never disagree about what a service's password is.
  tmp_json="$(mktemp)"
  echo "{" > "$tmp_json"
  pw_args=""
  n=${#IDENTITIES[@]}
  for i in "${!IDENTITIES[@]}"; do
    id="${IDENTITIES[$i]}"
    # 24 chars of base64 with the shell-awkward characters removed.
    pw="$(openssl rand -base64 32 | tr -d '/+=\n' | cut -c1-24)"
    comma=","
    [[ $i -eq $((n - 1)) ]] && comma=""
    printf '  "%s": "%s"%s\n' "$id" "$pw" "$comma" >> "$tmp_json"
    pw_args="$pw_args $id $pw"
  done
  echo "}" >> "$tmp_json"

  # mosquitto_passwd -b appends; -c creates. Create with the first identity,
  # append the rest. Runs as root inside the container, so chown the result
  # to uid 1883 (the mosquitto user) or the broker cannot read its own
  # password file at startup.
  docker run --rm -v "$HERE:/m" --entrypoint sh "$IMAGE" -c "
    set -e
    rm -f /m/passwd
    first=1
    set -- $pw_args
    while [ \$# -gt 0 ]; do
      user=\$1; pass=\$2; shift 2
      if [ \$first -eq 1 ]; then
        mosquitto_passwd -c -b /m/passwd \"\$user\" \"\$pass\"
        first=0
      else
        mosquitto_passwd -b /m/passwd \"\$user\" \"\$pass\"
      fi
    done
    chown 1883:1883 /m/passwd
    chmod 0600 /m/passwd
  " >/dev/null

  mv "$tmp_json" "$CREDS"
  chmod 0600 "$CREDS"
  echo "[creds] ${#IDENTITIES[@]} identities written to mosquitto/passwd"
fi

# --------------------------------------------------- runtime config staging
# mosquitto refuses to run as root and reads its config as uid 1883. Two
# consequences the obvious setup trips over:
#
#   * the TLS private key must be readable by 1883, and
#   * mosquitto 2.x warns "File ... owner is not mosquitto. Future versions
#     will refuse to load this file" for any config it does not own.
#
# Rather than chown the git-tracked mosquitto.conf and acl — which would make
# them require sudo to edit — stage a copy into .runtime/ and own THAT. The
# source files stay ordinary user-editable files; re-running this script (or
# run.sh, which always does) re-syncs the copy.
echo "[creds] staging runtime config"

# The copy is done INSIDE the container, as root, rather than on the host.
# The staged files must end up owned by uid 1883, which means the host user
# cannot overwrite them on the next run — doing the copy as root sidesteps
# that entirely and keeps this script re-runnable without sudo.
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

docker run --rm -v "$HERE:/m" --entrypoint sh "$IMAGE" -c "
  set -e
  mkdir -p /m/.runtime
  cp -f /m/mosquitto.conf /m/.runtime/mosquitto.conf
  cp -f /m/acl           /m/.runtime/acl

  # Directories owned by the host user so the next run can still write here;
  # files owned by mosquitto at 0600, because mosquitto 2.x warns about any
  # config it does not own or that is group/world readable, and 3.0 will
  # refuse to load it.
  chown ${HOST_UID}:${HOST_GID} /m/.runtime /m/certs 2>/dev/null || true
  chown 1883:1883 /m/.runtime/mosquitto.conf /m/.runtime/acl
  chmod 0600 /m/.runtime/mosquitto.conf /m/.runtime/acl

  for f in /m/certs/*.crt /m/certs/*.key; do
    [ -e \"\$f\" ] || continue
    chown 1883:1883 \"\$f\"
  done
  chmod 0640 /m/certs/*.key 2>/dev/null || true
  chmod 0644 /m/certs/*.crt 2>/dev/null || true
" >/dev/null

echo "[creds] done. Secrets live in mosquitto/ and are gitignored."
