#!/usr/bin/env bash
# RabbitMQ + MQTT + mTLS one-shot setup for Shelly devices
# - Default mode: quiet, with spinner; logs detailed output to /tmp/rabbitmq-mqtt-setup.log
# - Debug mode (--debug): verbose (streams command output), spinner disabled
#
# Notes / decisions baked-in:
# - curl uses --tlsv1.2 (don't force old TLS with -1 / TLSv1.0);
# - Plaintext mgmt/1883 are conditional on KEEP_PLAINTEXT=true (migration friendly).
# - Admin password is randomly generated unless provided (printed at the end).
# - Ubuntu "noble" repos are pinned deliberately (bare minimum baseline as requested).
# - Primary IP is derived from the default route (avoids docker/bridge IPs).
# - Least privilege for the CN user: restrict to amq.topic and MQTT_PREFIX via topic permissions.
# - SAN URI client_id binding is gated by RabbitMQ version.
# - UFW rules only if ufw exists and is active (skip otherwise).
# - set -o errtrace so ERR trap fires in subshells too.
# - printf used (not echo -e), with %s formatting, for safe color/spacing.

set -euo pipefail
set -o errtrace   # make ERR trap fire in subshells too
umask 027         # safer defaults for created files

# =============== Defaults (overridable by args/env) =================
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-}"        # may be overridden by --admin-pass

CLIENT_CN="${CLIENT_CN:-Shelly-Group}"
CLIENT_ID="${CLIENT_ID:-}"          # set to CN after arg parsing if still empty

VHOST="${VHOST:-/shelly}"

# Keep plaintext ports (1883 + 15672) during migration? (true/false)
KEEP_PLAINTEXT=${KEEP_PLAINTEXT:-false}

# Where to store TLS materials (all in one folder for easy copying)
TLS_DIR="${TLS_DIR:-/etc/rabbitmq-tls}"

# A safe export folder to expose via web - contains ONLY the 3 client files
EXPORT_DIR="${EXPORT_DIR:-/etc/mqtt-cert}"

# Optional second bundle for non-disruptive monitoring (separate client_id)
MAKE_MONITOR_CERT=${MAKE_MONITOR_CERT:-true}
MONITOR_CLIENT_ID="${MONITOR_CLIENT_ID:-}"   # set post-parse if empty
MONITOR_EXPORT_DIR="${MONITOR_EXPORT_DIR:-/etc/mqtt-cert-monitor}"

# Display-only MQTT prefix for topics (what you set in Shelly)
MQTT_PREFIX="${MQTT_PREFIX:-something}"

# If you know the broker IP, set it or pass --ip; otherwise it will be auto-detected
SERVER_IP="${SERVER_IP:-}"

# What address will clients dial? (DNS name or IP) — used for the server cert CN/SAN
CONNECT_DNS="${CONNECT_DNS:-}"           # e.g. mqtt.example.com
CONNECT_IP="${CONNECT_IP:-}"             # e.g. the VM's PUBLIC IP

# Log level for RabbitMQ logs (broker); debug/info/warn/error
LOG_LEVEL="${LOG_LEVEL:-info}"

# If true, regenerate CA/server/client even if they already exist
FORCE_REGEN="${FORCE_REGEN:-false}"

# Mode: default=quiet with spinner; debug=verbose without spinner
DEBUG=false

# Spinner & logging for long/quiet ops
SPINNER=true                      # default mode uses spinner
LOG_FILE="/tmp/rabbitmq-mqtt-setup.log"   # fixed path; easy to tail

# Which RabbitMQ series to install (controls Erlang pinning too): 3.13, 4.0, or 4.1
RMQ_SERIES="${RMQ_SERIES:-4.1}"
# ====================================================================

# Derived: topic filter for helper commands
TOPIC_FILTER="${MQTT_PREFIX}/#"

# ----- Colors & helpers -----
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  PURPLE=$'\033[35m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; NC=$'\033[0m'; BOLD=$'\033[1m'
else
  PURPLE=""; GREEN=""; RED=""; YELLOW=""; CYAN=""; NC=""; BOLD=""
fi
section() { printf "\n%s%s▶ %s%s\n" "$PURPLE" "$BOLD" "$*" "$NC"; }
ok()      { printf "%s✔ %s%s\n" "$GREEN" "$*" "$NC"; }
warn()    { printf "%s⚠ %s%s\n" "$YELLOW" "$*" "$NC"; }
err()     { printf "%s✖ %s%s\n" "$RED" "$*" "$NC" >&2; }
trap 'err "Failed at line $LINENO: $BASH_COMMAND"; exit 1' ERR

require_cmd() { command -v "$1" >/dev/null 2>&1; }

# Escape regex specials (except slash) for sed/ERE
re_escape() { sed -e 's/[.[\()*^$?+{}|]/\\&/g'; }

# Pick the newest available version matching a regex from apt-cache
# (use grep -E instead of awk -v to avoid '\.' escape warnings)
pick_version() {
  local pkg="$1" rx="$2"
  apt-cache madison "$pkg" \
    | awk '{print $3}' \
    | grep -E -m1 "$rx"
}

# -------- argument parsing ----------
ADMIN_PASS_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin-user)        ADMIN_USER="$2"; shift 2;;
    -p|--admin-pass)     ADMIN_PASS_ARG="$2"; shift 2;;
    -C|--client-cn)      CLIENT_CN="$2"; shift 2;;
    --client-id)         CLIENT_ID="$2"; shift 2;;
    -V|--vhost)          VHOST="$2"; shift 2;;
    -l|--log-level)      LOG_LEVEL="$2"; shift 2;;
    --force-regen)       FORCE_REGEN=true; shift ;;

    -i|--ip|--server-ip) SERVER_IP="$2"; shift 2;;
    --connect-dns)       CONNECT_DNS="$2"; shift 2;;
    --connect-ip)        CONNECT_IP="$2"; shift 2;;

    # plaintext: default false; presence enables
    --keep-plaintext)    KEEP_PLAINTEXT=true; shift ;;

    --tls-dir)           TLS_DIR="$2"; shift 2;;
    --export-dir)        EXPORT_DIR="$2"; shift 2;;
    --monitor-export-dir) MONITOR_EXPORT_DIR="$2"; shift 2;;
    --no-monitor-cert)   MAKE_MONITOR_CERT=false; shift ;;
    --monitor-client-id) MONITOR_CLIENT_ID="$2"; shift 2;;

    --mqtt-prefix)       MQTT_PREFIX="$2"; shift 2;;

    --rmq-series)        RMQ_SERIES="$2"; shift 2;;

    -d|--debug)          DEBUG=true; SPINNER=false; shift ;;

    --) shift; break;;
    *)  warn "Unknown argument: $1"; shift;;
  esac
done

# Disable spinner if not a TTY (prevents stray spinner frames in non-interactive logs)
[[ -t 1 ]] || SPINNER=false

# Prepare log file (truncate). If we cannot create it, fall back silently.
if ! : >"$LOG_FILE" 2>/dev/null; then
  warn "Cannot write log file at $LOG_FILE; falling back to /dev/null"
  LOG_FILE="/dev/null"
fi

# Make apt/dpkg fully non-interactive to avoid prompts (needrestart, tzdata, etc.)
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export APT_LISTCHANGES_FRONTEND=none

# Simple spinner + runners (quiet vs debug)
# - Default: background the command, show spinner, log to $LOG_FILE
# - Debug: stream command output to both screen and $LOG_FILE
spinner_loop() {
  local pid="$1" desc="$2" frames='-\|/' i=0 char
  while kill -0 "$pid" 2>/dev/null; do
    char="${frames:i++%${#frames}:1}"
    printf "\r%s%s %s%s" "$CYAN" "$char" "$desc" "$NC"
    sleep 0.1
  done
  printf "\r\033[K"  # clear line
}
run_step() {
  # usage: run_step "desc" CMD...
  local desc="$1"; shift
  if $DEBUG; then
    printf "%s… %s%s\n" "$CYAN" "$desc" "$NC"
    # In debug we pipe through tee; guard against set -e + pipefail
    set +e
    "$@" 2>&1 | tee -a "$LOG_FILE"
    local rc=${PIPESTATUS[0]}
    set -e
    if (( rc != 0 )); then
      err "$desc failed (see $LOG_FILE)"
      tail -n 80 "$LOG_FILE" | sed 's/^/  /'
      exit "$rc"
    fi
  else
    ( "$@" >>"$LOG_FILE" 2>&1 ) & local pid=$!
    $SPINNER && spinner_loop "$pid" "$desc"
    # Guard wait so a non-zero exit doesn’t trigger the ERR trap first
    if ! wait "$pid"; then
      local rc=$?
      err "$desc failed (see $LOG_FILE)"
      tail -n 80 "$LOG_FILE" | sed 's/^/  /'
      exit "$rc"
    fi
  fi
}

# Admin password precedence: arg > env > random
if [[ -n "$ADMIN_PASS_ARG" ]]; then 
  ADMIN_PASS="$ADMIN_PASS_ARG"
fi
if [[ -z "${ADMIN_PASS:-}" ]]; then
  ADMIN_PASS="$(openssl rand -base64 24)"
  GENERATED_ADMIN_PASS=true
else
  GENERATED_ADMIN_PASS=false
fi

# Post-parse cascaded defaults
: "${CLIENT_ID:=$CLIENT_CN}"
: "${MONITOR_CLIENT_ID:=${CLIENT_ID}-mon}"

# Recompute derived helper
TOPIC_FILTER="${MQTT_PREFIX}/#"

section "Preflight: ensure required tools (auto-install if missing)"
# We assume: sudo + apt-get exist on Ubuntu
for c in sudo apt-get; do
  if ! require_cmd "$c"; then err "Missing $c (this script targets Ubuntu/Debian)."; fi
done

# Map commands -> apt packages; install only missing
declare -A PKG_FOR_CMD=(
  [curl]=curl
  [gpg]=gnupg
  [openssl]=openssl
  [ip]=iproute2
  [awk]=gawk         # awk is usually present; install gawk if not
  [tar]=tar
  [tee]=coreutils    # should already be installed
  [systemctl]=systemd
  [apt-cache]=apt
)
TO_INSTALL=()
for cmd in "${!PKG_FOR_CMD[@]}"; do
  if ! require_cmd "$cmd"; then TO_INSTALL+=("${PKG_FOR_CMD[$cmd]}"); fi
done
if (( ${#TO_INSTALL[@]} )); then
  if $DEBUG; then
    run_step "apt-get update" sudo apt-get update
    run_step "install base tools" sudo -E apt-get -y \
      -o Dpkg::Use-Pty=0 \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      install "${TO_INSTALL[@]}" || true
  else
    run_step "apt-get update" sudo apt-get -qq update
    run_step "install base tools" sudo -E apt-get -y -qq \
      -o Dpkg::Use-Pty=0 \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      install "${TO_INSTALL[@]}" || true
  fi
fi
ok "Base tools present"
ok "Logging details to: $LOG_FILE"

section "Detecting server identity"
SERVER_HOST="$(hostname -f || hostname)"
if [[ -z "${SERVER_IP}" ]]; then
  # Prefer IP of default route (avoids docker/bridge IPs). If you pass --ip, we skip this.
  if ip route get 1.1.1.1 >/dev/null 2>&1; then
    SERVER_IP="$(ip route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  elif hostname -I >/dev/null 2>&1; then
    SERVER_IP="$(hostname -I | awk '{print $1}')"
  else
    SERVER_IP="$(ip -4 addr show | awk '/inet /{print $2}' | cut -d/ -f1 | head -n1)"
  fi
fi
# If not provided, fall back sensibly for the dialed address
: "${CONNECT_IP:=${SERVER_IP}}"
CONNECT_HOST="${CONNECT_DNS:-${CONNECT_IP}}"
ok "Hostname: ${SERVER_HOST}"
ok "Primary IP: ${SERVER_IP}"
ok "Clients will connect to: ${CONNECT_HOST}"

section "Installing prerequisites"
if $DEBUG; then
  run_step "apt-get update" sudo apt-get update
  run_step "install curl/gnupg/openssl" sudo -E apt-get -y \
    -o Dpkg::Use-Pty=0 \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    install curl gnupg apt-transport-https openssl
else
  run_step "apt-get update" sudo apt-get -qq update
  run_step "install curl/gnupg/openssl" sudo -E apt-get -y -qq \
    -o Dpkg::Use-Pty=0 \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    install curl gnupg apt-transport-https openssl
fi
ok "Prerequisites installed"

section "Adding Team RabbitMQ key and repos"
# Safer curl flags: no TLSv1.0; enforce TLS >= 1.2 (no TLS 1.3 requirement here)
curl -fsSL --proto '=https' --tlsv1.2 --retry 3 \
  "https://keys.openpgp.org/vks/v1/by-fingerprint/0A9AF2115F4687BD29803A206B73A36E6026DFCA" \
  | sudo gpg --dearmor | sudo tee /usr/share/keyrings/com.rabbitmq.team.gpg >/dev/null

# NOTE: Repos are intentionally pinned to Ubuntu noble (bare minimum baseline).
sudo tee /etc/apt/sources.list.d/rabbitmq.list >/dev/null <<'EOF'
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
EOF

if $DEBUG; then
  run_step "apt-get update (RabbitMQ repos)" sudo apt-get update
else
  run_step "apt-get update (RabbitMQ repos)" sudo apt-get -qq update
fi
ok "Repositories added"

# ---- Decide series & pin versions (3.13 -> Erlang 26; 4.0/4.1 -> Erlang 27) ----
case "$RMQ_SERIES" in
  3.13) ERLANG_RX='^1:26\.'; RBMQ_RX='^3\.13\.' ;;
  4.0)  ERLANG_RX='^1:27\.'; RBMQ_RX='^4\.0\.'  ;;
  4.1)  ERLANG_RX='^1:27\.'; RBMQ_RX='^4\.1\.'  ;;
  *)    err "Unsupported --rmq-series '$RMQ_SERIES' (use 3.13, 4.0, or 4.1)";;
esac

# Resolve versions (best effort: pin if found, else fall back to unpinned)
ERLANG_VERSION="$(pick_version erlang-base "$ERLANG_RX" || true)"
RBMQ_VERSION_PIN="$(pick_version rabbitmq-server "$RBMQ_RX" || true)"
if [[ -z "$ERLANG_VERSION" && "$RMQ_SERIES" == "3.13" ]]; then
  ERLANG_VERSION="${ERLANG_26_VERSION:-1:26.2.5.13-1}"
fi

section "Install plan"
printf "  RabbitMQ series: %s\n" "$RMQ_SERIES"
printf "  Erlang desired:  %s\n" "${ERLANG_VERSION:-<repo default>}"
printf "  RabbitMQ pin:    %s\n" "${RBMQ_VERSION_PIN:-<repo default>}"

section "Installing Erlang + RabbitMQ"
ERL_PKGS=(erlang-base erlang-asn1 erlang-crypto erlang-eldap erlang-ftp erlang-inets
          erlang-mnesia erlang-os-mon erlang-parsetools erlang-public-key
          erlang-runtime-tools erlang-snmp erlang-ssl erlang-syntax-tools
          erlang-tftp erlang-tools erlang-xmerl)

if [[ -n "$ERLANG_VERSION" ]]; then
  for i in "${!ERL_PKGS[@]}"; do ERL_PKGS[$i]="${ERL_PKGS[$i]}=${ERLANG_VERSION}"; done
else
  warn "Could not resolve Erlang version matching ${ERLANG_RX}; installing unpinned Erlang (repo default)."
fi

if $DEBUG; then
  run_step "install Erlang" sudo -E apt-get -y \
    -o Dpkg::Use-Pty=0 \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    install "${ERL_PKGS[@]}"
else
  run_step "install Erlang" sudo -E apt-get -y -qq \
    -o Dpkg::Use-Pty=0 \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    install "${ERL_PKGS[@]}"
fi

if [[ -n "$RBMQ_VERSION_PIN" ]]; then
  if $DEBUG; then
    run_step "install RabbitMQ ($RBMQ_VERSION_PIN)" sudo -E apt-get -y \
      -o Dpkg::Use-Pty=0 \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      install "rabbitmq-server=${RBMQ_VERSION_PIN}" --fix-missing
  else
    run_step "install RabbitMQ ($RBMQ_VERSION_PIN)" sudo -E apt-get -y -qq \
      -o Dpkg::Use-Pty=0 \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      install "rabbitmq-server=${RBMQ_VERSION_PIN}" --fix-missing
  fi
else
  warn "Could not resolve rabbitmq-server version for series ${RMQ_SERIES}; installing repo default."
  if $DEBUG; then
    run_step "install RabbitMQ" sudo -E apt-get -y \
      -o Dpkg::Use-Pty=0 \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      install rabbitmq-server --fix-missing
  else
    run_step "install RabbitMQ" sudo -E apt-get -y -qq \
      -o Dpkg::Use-Pty=0 \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      install rabbitmq-server --fix-missing
  fi
fi
ok "Erlang + RabbitMQ Installed"

section "Enable & start RabbitMQ (handle masked units)"
# shellcheck disable=SC2016
run_step "unmask+enable+start rabbitmq" sudo bash -c '
  set -euo pipefail

  # Unmask if needed (robust w/ pipefail)
  st="$(systemctl is-enabled rabbitmq-server 2>/dev/null || true)"
  if [[ "$st" == masked* ]]; then
    echo "Unmasking rabbitmq-server.service"
    systemctl unmask rabbitmq-server
  fi

  st_epmd="$(systemctl is-enabled epmd.socket 2>/dev/null || true)"
  if [[ "$st_epmd" == masked* ]]; then
    echo "Unmasking epmd.socket"
    systemctl unmask epmd.socket
  fi'

  # Reload, enable, start
run_step "enable+start rabbitmq" sudo bash -c '
  set -euo pipefail
  systemctl daemon-reload
  systemctl enable rabbitmq-server
  systemctl start rabbitmq-server

  # Verify and show logs if it failed
  if ! systemctl is-active --quiet rabbitmq-server; then
    echo "RabbitMQ failed to start; recent logs:" >&2
    journalctl -u rabbitmq-server -n 120 --no-pager >&2 || true
    exit 1
  fi
'
ok "RabbitMQ is running"

section "Enable plugins (Management UI + MQTT)"
run_step "enable rabbitmq_management"        sudo rabbitmq-plugins enable rabbitmq_management
run_step "enable rabbitmq_mqtt"              sudo rabbitmq-plugins enable rabbitmq_mqtt
# For AMQP EXTERNAL auth; harmless if unused by MQTT but fine to enable:
run_step "enable rabbitmq_auth_mechanism_ssl" sudo rabbitmq-plugins enable rabbitmq_auth_mechanism_ssl || true
ok "Plugins enabled"

section "Enable feature flag (UI only)"
# Only enable the detailed queues endpoint (adds more detail in the Queues page).
# We DO NOT enable anything else (e.g., khepri_db).
run_step "enable detailed_queues_endpoint" sudo rabbitmqctl enable_feature_flag detailed_queues_endpoint || true

# Capture status for the summary table
DETAILED_QUEUES_FLAG_STATUS="$(
  sudo rabbitmqctl list_feature_flags 2>>"$LOG_FILE" | awk '$1=="detailed_queues_endpoint"{print $2}'
)"
ok "detailed_queues_endpoint enabled"

# Capture only the version string (avoid "Asking node..." noise)
RABBIT_VERSION="$(
  sudo rabbitmq-diagnostics server_version 2>>"$LOG_FILE" | awk 'NF{line=$0} END{print line}'
)"
ok "RabbitMQ version detected: ${RABBIT_VERSION}"

section "Generate CA, server, and client certificates (mutual TLS)"
sudo mkdir -p "$TLS_DIR"
sudo chown -R root:rabbitmq "$TLS_DIR"
sudo chmod 750 "$TLS_DIR"

if [[ "$FORCE_REGEN" != "true" ]] && [[ -s "$TLS_DIR/ca.key" && -s "$TLS_DIR/tls.key" && -s "$TLS_DIR/client.key" ]]; then
  warn "Existing TLS materials found in ${TLS_DIR}; skipping regeneration (set FORCE_REGEN=true to rotate)."
else
  # --- CA (explicit CA:true) ---
  run_step "generate CA private key" \
    sudo openssl genrsa -out "$TLS_DIR/ca.key" 4096

  sudo tee "$TLS_DIR/openssl-ca.cnf" >/dev/null <<'EOF'
[ req ]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[ dn ]
CN = RabbitMQ Demo CA
[ v3_ca ]
basicConstraints = critical,CA:true
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
EOF

  run_step "self-sign CA certificate" \
    sudo openssl req -x509 -new -key "$TLS_DIR/ca.key" -sha256 -days 3650 \
      -config "$TLS_DIR/openssl-ca.cnf" -out "$TLS_DIR/ca.crt"
  ok "CA created"

  # --- Server cert (CN/SAN match what Shelly dials) ---
  run_step "generate server private key" \
    sudo openssl genrsa -out "$TLS_DIR/tls.key" 2048

  sudo tee "$TLS_DIR/openssl.cnf" >/dev/null <<EOF
[ req ]
distinguished_name = dn
req_extensions     = v3_req
prompt             = no
[ dn ]
CN = ${CONNECT_HOST}
[ v3_req ]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
[ alt_names ]
DNS.1 = ${CONNECT_DNS:-${SERVER_HOST}}
DNS.2 = ${SERVER_HOST}
IP.1  = ${CONNECT_IP}
IP.2  = ${SERVER_IP}
EOF

  run_step "create server CSR" \
    sudo openssl req -new -key "$TLS_DIR/tls.key" -out "$TLS_DIR/server.csr" -config "$TLS_DIR/openssl.cnf"
  run_step "sign server certificate" \
    sudo openssl x509 -req -in "$TLS_DIR/server.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
      -out "$TLS_DIR/tls.crt" -days 825 -sha256 -extensions v3_req -extfile "$TLS_DIR/openssl.cnf"
  ok "Server certificate created"

  # --- Client cert (Shelly) ---
  run_step "generate client private key" \
    sudo openssl genrsa -out "$TLS_DIR/client.key" 2048
  sudo tee "$TLS_DIR/openssl-client.cnf" >/dev/null <<EOF
[ req ]
distinguished_name = dn
req_extensions     = v3_req
prompt             = no
[ dn ]
CN = ${CLIENT_CN}
[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectAltName = URI:${CLIENT_ID}
EOF
  run_step "create client CSR" \
    sudo openssl req -new -key "$TLS_DIR/client.key" -out "$TLS_DIR/client.csr" -config "$TLS_DIR/openssl-client.cnf"
  run_step "sign client certificate" \
    sudo openssl x509 -req -in "$TLS_DIR/client.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
      -out "$TLS_DIR/client.crt" -days 825 -sha256 -extensions v3_req -extfile "$TLS_DIR/openssl-client.cnf"
  ok "Client certificate created"

  # Optional: monitoring client cert
  if [[ "${MAKE_MONITOR_CERT}" == "true" ]]; then
    section "Generate MONITOR client certificate (client_id: ${MONITOR_CLIENT_ID})"
    run_step "generate monitor private key" \
      sudo openssl genrsa -out "$TLS_DIR/client-monitor.key" 2048
    sudo tee "$TLS_DIR/openssl-client-monitor.cnf" >/dev/null <<EOF
[ req ]
distinguished_name = dn
req_extensions     = v3_req
prompt             = no
[ dn ]
CN = ${CLIENT_CN}
[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectAltName = URI:${MONITOR_CLIENT_ID}
EOF
    run_step "create monitor CSR" \
      sudo openssl req -new -key "$TLS_DIR/client-monitor.key" -out "$TLS_DIR/client-monitor.csr" -config "$TLS_DIR/openssl-client-monitor.cnf"
    run_step "sign monitor certificate" \
      sudo openssl x509 -req -in "$TLS_DIR/client-monitor.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
        -out "$TLS_DIR/client-monitor.crt" -days 825 -sha256 -extensions v3_req -extfile "$TLS_DIR/openssl-client-monitor.cnf"
    ok "Monitor certificate created"
  fi

  # File permissions (least privilege)
  sudo chown root:rabbitmq "$TLS_DIR"/{tls.key,client.key}
  sudo chmod 640 "$TLS_DIR"/{tls.key,client.key}
  sudo chmod 644 "$TLS_DIR"/{tls.crt,client.crt,ca.crt}
  if [[ "${MAKE_MONITOR_CERT}" == "true" ]]; then
    sudo chown root:rabbitmq "$TLS_DIR/client-monitor.key"
    sudo chmod 640 "$TLS_DIR/client-monitor.key"
    sudo chmod 644 "$TLS_DIR/client-monitor.crt"
  fi
fi

section "Backup and write /etc/rabbitmq/rabbitmq.conf (mTLS enforced)"
# Backup existing config (timestamped) before overwriting
if [[ -f /etc/rabbitmq/rabbitmq.conf ]]; then
  sudo cp -a /etc/rabbitmq/rabbitmq.conf "/etc/rabbitmq/rabbitmq.conf.bak.$(date +%F_%H%M%S)"
  warn "Existing config backed up"
fi

# version compare ($1 >= $2 ?)
ver_ge() { [ "$(printf '%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

# Gate SAN client_id binding by RabbitMQ series
if ver_ge "$RABBIT_VERSION" "4.0.0"; then
  # 4.x: supports binding MQTT client_id to certificate SAN (URI)
  SAN_BLOCK=$(cat <<'EOS'
## 4.x: Enforce MQTT client_id from certificate SAN (URI)
mqtt.ssl_cert_client_id_from = subject_alternative_name
# Username still comes from CN unless you switch to SAN:
# ssl_cert_login_from = subject_alternative_name
# ssl_cert_login_san_type = uri
EOS
)
  SAN_STATUS="4.x: client_id bound to SAN (URI)"
else
  # 3.x (incl. 3.13): NO client_id-from-SAN support. Keep CN for username.
  SAN_BLOCK=$(cat <<'EOS'
## 3.x: client_id-from-SAN NOT supported.
## Cert login uses CN by default. If you prefer username from SAN URI, uncomment:
# ssl_cert_login_from = subject_alternative_name
# ssl_cert_login_san_type = uri
EOS
)
  SAN_STATUS="3.x: CN username; no client_id SAN binding"
fi

# TLS versions: lock to TLS 1.2 ONLY (explicitly omit 1.3 everywhere)
TLS_VERSIONS=$'ssl_options.versions.1           = tlsv1.2'

# Management TLS version line is a 4.x feature; 3.13 uses global ssl_options
if ver_ge "$RABBIT_VERSION" "4.0.0"; then
  MGMT_TLS_VERSIONS=$'management.ssl.versions.1   = tlsv1.2'
else
  MGMT_TLS_VERSIONS='# management.ssl.versions.* unsupported on 3.13 (global TLS forced to TLSv1.2)'
fi

# For disabling plaintext listeners:
# - RabbitMQ 4.x: DO NOT set 'management.tcp.port = none' (expects an integer). Omit/comment the line to disable.
# - RabbitMQ 3.13: omit/comment the tcp lines as before.
if ver_ge "$RABBIT_VERSION" "4.0.0"; then
  # Management (HTTP): HTTPS-only by omitting/commenting the TCP listener
  if $KEEP_PLAINTEXT; then
    MGMT_TCP_LINE="management.tcp.port = 15672"
  else
    MGMT_TCP_LINE="# management.tcp.port disabled"
  fi

  # MQTT: TCP listener can be explicitly disabled with '= none'
  MQTT_TCP_LINE=$( $KEEP_PLAINTEXT && echo "mqtt.listeners.tcp.default = 1883" || echo "mqtt.listeners.tcp = none" )
else
  # 3.13 branch
  MGMT_TCP_LINE=$( $KEEP_PLAINTEXT && echo "management.tcp.port = 15672" || echo "# management.tcp.port disabled" )
  MQTT_TCP_LINE=$( $KEEP_PLAINTEXT && echo "mqtt.listeners.tcp.default = 1883" || echo "# mqtt.listeners.tcp.default disabled" )
fi

# NOTE: the $( $KEEP_PLAINTEXT && echo ... ) trick uses /bin/true|false as commands.
sudo tee /etc/rabbitmq/rabbitmq.conf >/dev/null <<EOF
## Management
management.ssl.port = 15671
management.ssl.certfile   = ${TLS_DIR}/tls.crt
management.ssl.keyfile    = ${TLS_DIR}/tls.key
management.ssl.cacertfile = ${TLS_DIR}/ca.crt
${MGMT_TLS_VERSIONS}
${MGMT_TCP_LINE}

## MQTT listeners
mqtt.listeners.ssl.default = 8883
${MQTT_TCP_LINE}
mqtt.vhost = ${VHOST}
mqtt.allow_anonymous = false

## Use client certificate CN as the MQTT username
mqtt.ssl_cert_login = true
ssl_cert_login_from = common_name

${SAN_BLOCK}

## Global TLS options (server-auth + client-auth)
ssl_options.certfile             = ${TLS_DIR}/tls.crt
ssl_options.keyfile              = ${TLS_DIR}/tls.key
ssl_options.cacertfile           = ${TLS_DIR}/ca.crt
ssl_options.verify               = verify_peer
ssl_options.fail_if_no_peer_cert = true
ssl_options.depth                = 3
${TLS_VERSIONS}

# Optional AMQP over TLS (uncomment if you need 5671)
# listeners.ssl.default = 5671

# Broker log level (debug/info/warning/error)
log.file.level = ${LOG_LEVEL}
EOF
ok "rabbitmq.conf written"

section "Create vhost and users (CN must exist as a user)"
# Use quiet runner so rabbitmqctl chatter goes to $LOG_FILE in default mode
run_step "add vhost ${VHOST}" sudo rabbitmqctl add_vhost "$VHOST" || true

# Admin with generated/arg password
if ! sudo rabbitmqctl list_users 2>>"$LOG_FILE" | awk '{print $1}' | grep -qx "$ADMIN_USER"; then
  run_step "add admin user ${ADMIN_USER}" sudo rabbitmqctl add_user "$ADMIN_USER" "$ADMIN_PASS"
else
  run_step "update admin password" sudo rabbitmqctl change_password "$ADMIN_USER" "$ADMIN_PASS"
fi
run_step "tag admin as administrator" sudo rabbitmqctl set_user_tags "$ADMIN_USER" administrator
run_step "grant admin perms on /"     sudo rabbitmqctl set_permissions -p / "$ADMIN_USER" ".*" ".*" ".*"
run_step "grant admin perms on ${VHOST}" sudo rabbitmqctl set_permissions -p "$VHOST" "$ADMIN_USER" ".*" ".*" ".*"

# Delete guest (least privilege, smaller attack surface)
run_step "delete guest user" sudo rabbitmqctl delete_user guest || true

# CN user for cert-login (password unused when cert auth is on)
if ! sudo rabbitmqctl list_users 2>>"$LOG_FILE" | awk '{print $1}' | grep -qx "$CLIENT_CN"; then
  run_step "add CN user ${CLIENT_CN}" sudo rabbitmqctl add_user "$CLIENT_CN" "unused-password"
else
  run_step "refresh CN user password" sudo rabbitmqctl change_password "$CLIENT_CN" "unused-password"
fi

# Least-privilege:
# - configure: allow declaring MQTT subscription queues (and temp amq.gen-*)
# - write:     allow binding the sub queues + publish to amq.topic
# - read:      allow consuming from sub queues + pass exchange read check
CONFIG_RGX='^mqtt-subscription-.*$|^amq\.gen-.*$'
WRITE_RGX='^amq\.topic$|^mqtt-subscription-.*$|^amq\.gen-.*$'
READ_RGX='^mqtt-subscription-.*$|^amq\.gen-.*$|^amq\.topic$|^mqtt$'

run_step "set base perms for CN on ${VHOST}" \
  sudo rabbitmqctl set_permissions -p "$VHOST" "$CLIENT_CN" \
    "$CONFIG_RGX" "$WRITE_RGX" "$READ_RGX"

# Constrain publish/subscribe under your prefix in BOTH forms (slash + dot)
# and allow Shelly legacy/broadcast READ on 'shellies/command' (read-only)
MQTT_PREFIX_ESC="$(printf "%s" "$MQTT_PREFIX" | re_escape)"
AMQP_PREFIX_ESC="$(printf "%s" "$MQTT_PREFIX" | sed 's/[.[\()*^$?+{}|]/\\&/g; s,/,\\.,g')"
MQTT_RGX="^${MQTT_PREFIX_ESC}(/.*)?$"
AMQP_RGX="^${AMQP_PREFIX_ESC}(\\..*)?$"
TOPIC_RGX="(${MQTT_RGX})|(${AMQP_RGX})"

TOPIC_RGX_W="${TOPIC_RGX}"
TOPIC_RGX_R="${TOPIC_RGX}|(^shellies/command$)|(^shellies\\.command$)"

if sudo rabbitmqctl help 2>>"$LOG_FILE" | grep -q set_topic_permissions; then
  run_step "set topic perms for CN (prefix + shellies/command READ)" \
    sudo rabbitmqctl set_topic_permissions -p "$VHOST" "$CLIENT_CN" amq.topic \
      "$TOPIC_RGX_W" "$TOPIC_RGX_R"
else
  warn "RabbitMQ version lacks 'set_topic_permissions'; leaving broad topic access."
fi

ok "Users and permissions set"

section "Open firewall (UFW)"
# Only add rules if ufw exists and is active (some distros use other firewalls)
if command -v ufw >/dev/null 2>&1 && sudo ufw status | awk 'NR==1 && tolower($2)=="active"{ok=1} END{exit !ok}'; then
  run_step "ufw allow 8883/tcp"  sudo ufw allow 8883/tcp  >/dev/null 2>&1 || true  # MQTTS
  run_step "ufw allow 15671/tcp" sudo ufw allow 15671/tcp >/dev/null 2>&1 || true  # HTTPS (Mgmt)
  if $KEEP_PLAINTEXT; then
    run_step "ufw allow 1883/tcp"  sudo ufw allow 1883/tcp  >/dev/null 2>&1 || true
    run_step "ufw allow 15672/tcp" sudo ufw allow 15672/tcp >/dev/null 2>&1 || true
  fi
  ok "Firewall updated via UFW"
else
  warn "UFW not present or not active; skipping firewall rules."
fi

section "Restart RabbitMQ and show listeners"
# Make REALLY sure it isn't masked before restart and recover gracefully on failure
CONF_BAK_LATEST="$(ls -1t /etc/rabbitmq/rabbitmq.conf.bak.* 2>/dev/null | head -n1 || true)"
run_step "ensure unit unmasked" sudo bash -c '
  set -euo pipefail
  [[ "$(systemctl is-enabled rabbitmq-server 2>/dev/null || true)" == masked* ]] && systemctl unmask rabbitmq-server || true
  [[ "$(systemctl is-enabled epmd.socket 2>/dev/null || true)" == masked* ]] && systemctl unmask epmd.socket || true
  systemctl daemon-reload
'
if ! (sudo systemctl restart rabbitmq-server >>"$LOG_FILE" 2>&1); then
  err "Restart failed (see $LOG_FILE). Attempting recovery…"
  if [[ -n "$CONF_BAK_LATEST" && -f "$CONF_BAK_LATEST" ]]; then
    warn "Restoring previous config: $CONF_BAK_LATEST"
    sudo cp -af "$CONF_BAK_LATEST" /etc/rabbitmq/rabbitmq.conf
    sudo systemctl start rabbitmq-server || true
  fi
  echo "Recent journal:" >&2
  journalctl -xeu rabbitmq-server --no-pager -n 120 2>&1 | sed 's/^/  /' >&2 || true
  exit 1
fi
# Listeners output is useful to show explicitly to confirm ports/protocols
sudo rabbitmq-diagnostics listeners || true

section "Pack client bundle for Shelly upload"
BUNDLE="/tmp/${CLIENT_CN//[^A-Za-z0-9_.-]/_}-mqtt-mtls.tar.gz"
sudo tar czf "$BUNDLE" -C "$TLS_DIR" client.crt client.key ca.crt
sudo chmod 600 "$BUNDLE"
ok "Bundle created: $BUNDLE"

# Optional pack monitor client bundle
if [[ "${MAKE_MONITOR_CERT}" == "true" ]]; then
  section "Pack MONITOR client bundle (non-disruptive subscribe)"
  BUNDLE_MON="/tmp/${CLIENT_CN//[^A-Za-z0-9_.-]/_}-monitor-mtls.tar.gz"
  sudo tar czf "$BUNDLE_MON" -C "$TLS_DIR" client-monitor.crt client-monitor.key ca.crt
  sudo chmod 600 "$BUNDLE_MON"
  ok "Monitor bundle created: $BUNDLE_MON"
fi

section "Prepare export folder for web upload (ONLY the 3 client files)"
sudo mkdir -p "$EXPORT_DIR"
# world-readable CRTs; key readable by root (serve with sudo or adjust as needed)
sudo install -m 644 "$TLS_DIR/ca.crt"     "$EXPORT_DIR/ca.crt"
sudo install -m 644 "$TLS_DIR/client.crt" "$EXPORT_DIR/client.crt"
sudo install -m 600 "$TLS_DIR/client.key" "$EXPORT_DIR/client.key"
ok "Export folder ready: $EXPORT_DIR"

# Optional export monitor certs separately
if [[ "${MAKE_MONITOR_CERT}" == "true" ]]; then
  sudo mkdir -p "$MONITOR_EXPORT_DIR"
  sudo install -m 644 "$TLS_DIR/ca.crt"             "$MONITOR_EXPORT_DIR/ca.crt"
  sudo install -m 644 "$TLS_DIR/client-monitor.crt" "$MONITOR_EXPORT_DIR/client.crt"
  sudo install -m 600 "$TLS_DIR/client-monitor.key" "$MONITOR_EXPORT_DIR/client.key"
  ok "Monitor export folder ready: $MONITOR_EXPORT_DIR"
fi

# ---------- Final output formatting helpers ----------
COL1_W=36
COL2_W=50
mkdash()  { local n="$1" __s; printf -v __s '%*s' "$n" ""; printf "%s" "${__s// /-}"; }
tbar()    { printf "%s%s+%s+%s+%s\n" "$GREEN" "$BOLD" "$(mkdash $((COL1_W+2)))" "$(mkdash $((COL2_W+2)))" "$NC"; }
trow()    { printf "%s| %-*s | %-*s |%s\n" "$CYAN" "$COL1_W" "$1" "$COL2_W" "$2" "$NC"; }
titlebox(){ local w=$((COL1_W+COL2_W+7)); printf "%s%s+%s+%s\n" "$GREEN" "$BOLD" "$(mkdash $((w-2)))" "$NC"; \
            printf "%s%s| %-*s |%s\n" "$GREEN" "$BOLD" $((w-4)) "$1" "$NC"; \
            printf "%s%s+%s+%s\n" "$GREEN" "$BOLD" "$(mkdash $((w-2)))" "$NC"; }
hr()      { local w=$((COL1_W+COL2_W+7)); printf "%s%s%s\n" "$GREEN" "$(mkdash $w)" "$NC"; }

# ---- status used in tables ----
PLAINTEXT_STATUS=$( $KEEP_PLAINTEXT && echo "Kept open (1883/15672)" || echo "Disabled" )

# ---- Display label for admin password (table-friendly) ----
if $GENERATED_ADMIN_PASS; then
  ADMIN_PASS_LABEL="Admin password (auto-generated)"
elif [[ -n "$ADMIN_PASS_ARG" ]]; then
  ADMIN_PASS_LABEL="Admin password (from --admin-pass)"
else
  ADMIN_PASS_LABEL="Admin password (from env)"
fi

section "Success! Final steps"
titlebox "Shelly MQTT (TLS with client certificate)"
echo

# ===== Shelly device config (copy these into the Shelly UI) =====
printf "%s%sShelly device config%s\n" "$PURPLE" "$BOLD" "$NC"
tbar
trow "Enable"                               "[x]"
trow "User TLS"                             "Selected"
trow "Use client certificate"               "[x] Enabled"
trow "Enable 'MQTT Control'"                "[x] Enabled"
trow "Enable RPC over MQTT"                 "[x] Enabled"
trow "RPC status notifications over MQTT"   "[x] Enabled"
trow "Generic status update over MQTT"      "[x] Enabled"
trow "Server"                               "${CONNECT_HOST}:8883"
trow "Client ID"                            "${CLIENT_ID}"
trow "MQTT prefix"                          "${MQTT_PREFIX}"
trow "Username / Password"                  "Leave blank"
tbar
echo
printf "%sTip:%s Upload the CA, client cert, and client key to the Shelly first, then apply these settings.\n\n" "$YELLOW" "$NC"

# ===== Certificates to upload on the Shelly =====
printf "%s%sShelly certificate upload%s\n" "$PURPLE" "$BOLD" "$NC"
tbar
trow "CA certificate"              "${EXPORT_DIR}/ca.crt"
trow "Client certificate"          "${EXPORT_DIR}/client.crt"
trow "Client private key"          "${EXPORT_DIR}/client.key"
tbar
echo

# ===== Paths & Files =====
printf "%s%sPaths & Files%s\n" "$PURPLE" "$BOLD" "$NC"
tbar
trow "Export folder (web)"         "${EXPORT_DIR}"
tbar
echo

# ===== Bundles =====
printf "%s%sBundles%s\n" "$PURPLE" "$BOLD" "$NC"
tbar
trow "Tarball (all 3)"             "$BUNDLE"
if [[ "${MAKE_MONITOR_CERT}" == "true" ]]; then
  trow "Monitor tarball"           "$BUNDLE_MON"
fi
tbar
echo

# ===== Monitor (optional) =====
if [[ "${MAKE_MONITOR_CERT}" == "true" ]]; then
  printf "%s%sMonitor client (non-disruptive)%s\n" "$PURPLE" "$BOLD" "$NC"
  tbar
  trow "Monitor client_id"         "${MONITOR_CLIENT_ID}"
  trow "Monitor export folder"     "${MONITOR_EXPORT_DIR}"
  tbar
  echo
fi

# ===== Broker details & diagnostics (for operators) =====
printf "%s%sBroker details & diagnostics%s\n" "$PURPLE" "$BOLD" "$NC"
tbar
trow "Vhost used by MQTT"          "${VHOST} (via mqtt.vhost)"
trow "Client ID bound to cert"     "${SAN_STATUS:-Unknown}"
trow "Topic ACL (prefix)"          "${MQTT_PREFIX}/* and ${MQTT_PREFIX}.*"
trow "Compat READ topic"           "shellies/command (read-only)"
trow "TLS / mTLS"                  "TLSv1.2 (client cert required)"
trow "Feature flags"               "detailed_queues_endpoint=${DETAILED_QUEUES_FLAG_STATUS:-unknown}"
trow "Plain ports"                 "${PLAINTEXT_STATUS}"
trow "Management UI"               "https://${SERVER_IP}:15671"
trow "Admin user"                  "${ADMIN_USER}"
trow "$ADMIN_PASS_LABEL"           "${ADMIN_PASS}"
trow "Broker log file"             "/var/log/rabbitmq/rabbit@$(hostname -s).log"
tbar
echo
printf "%sNote:%s Shelly does not send a vhost; RabbitMQ routes it to '%s' via mqtt.vhost.\n\n" "$YELLOW" "$NC" "$VHOST"
echo

# ===== Optional local test (mutual TLS via mosquitto-clients) =====
printf "%s%sOptional local test (mutual TLS via mosquitto-clients):%s\n" "$PURPLE" "$BOLD" "$NC"
echo "  sudo apt-get -y install mosquitto-clients"
echo "  sudo mosquitto_sub -h ${CONNECT_HOST} -p 8883 \\"
echo "    --cafile ${EXPORT_DIR}/ca.crt --cert ${EXPORT_DIR}/client.crt --key ${EXPORT_DIR}/client.key \\"
echo "    --id ${CLIENT_ID} --tls-version tlsv1.2 -t '${TOPIC_FILTER}' -v"
if [[ "${MAKE_MONITOR_CERT}" == "true" ]]; then
  echo
  printf "%s%sOptional monitoring test(mutual TLS via mosquitto-clients):%s\n" "$PURPLE" "$BOLD" "$NC"
  echo "  sudo mosquitto_sub -h ${CONNECT_HOST} -p 8883 \\"
  echo "    --cafile ${MONITOR_EXPORT_DIR}/ca.crt --cert ${MONITOR_EXPORT_DIR}/client.crt --key ${MONITOR_EXPORT_DIR}/client.key \\"
  echo "    --id ${MONITOR_CLIENT_ID} --tls-version tlsv1.2 -t '${TOPIC_FILTER}' -v"
fi
echo

# Friendly tip to avoid confusion hitting MQTT port in a browser
warn "Port 8883 is MQTT over TLS (not HTTP). Use a MQTT client; If you expose files via HTTP, (never ${TLS_DIR})."

# ---------- TLS quick diagnostics (non-fatal) ----------
section "TLS quick diagnostics (non-fatal)"

# Make sure failures here don't abort the script
set +e

TLS_DIR="${TLS_DIR:-/etc/rabbitmq-tls}"
HOST_SHOW="${CONNECT_HOST:-${SERVER_IP}}"

printf "%sLocal server certificate (CN/SAN/Issuer/Fingerprint)%s\n" "$PURPLE$BOLD" "$NC"
sudo openssl x509 -in "$TLS_DIR/tls.crt" -noout \
  -subject -issuer -dates -fingerprint -sha256 \
  -ext subjectAltName 2>/dev/null | sed 's/^/  /'
echo

printf "%sCA certificate (basic constraints / key usage)%s\n" "$PURPLE$BOLD" "$NC"
sudo openssl x509 -in "$TLS_DIR/ca.crt" -noout -text 2>/dev/null \
  | grep -Ei 'Basic Constraints|Key Usage|CA:TRUE|keyCertSign|cRLSign' | sed 's/^/  /'
echo

printf "%sLocal chain verification%s\n" "$PURPLE$BOLD" "$NC"
sudo openssl verify -CAfile "$TLS_DIR/ca.crt" "$TLS_DIR/tls.crt" 2>&1 | sed 's/^/  /'
echo

# Fingerprint compare: local vs what the broker presents on the wire (mTLS-aware)
printf "%sRemote certificate at %s:8883 (best-effort)%s\n" "$PURPLE$BOLD" "$HOST_SHOW" "$NC"

# Temporarily relax error handling for best-effort network probe
trap - ERR
set +o pipefail

REMOTE_OUT="$(
  timeout 6 openssl s_client -connect "${HOST_SHOW}:8883" -servername "${CONNECT_DNS:-$HOST_SHOW}" \
    -CAfile "$TLS_DIR/ca.crt" \
    -cert   "${EXPORT_DIR}/client.crt" \
    -key    "${EXPORT_DIR}/client.key" \
    -tls1_2 </dev/null 2>/dev/null
)"
REMOTE_FPR="$(printf '%s' "$REMOTE_OUT" | openssl x509 -noout -fingerprint -sha256 2>/dev/null | awk -F= '{print $2}')"

if [[ -n "$REMOTE_FPR" ]]; then
  LOCAL_FPR="$(sudo openssl x509 -in "$TLS_DIR/tls.crt" -noout -fingerprint -sha256 2>/dev/null | awk -F= '{print $2}')"
  printf "  Local  SHA256 Fingerprint: %s\n" "${LOCAL_FPR:-<unknown>}"
  printf "  Remote SHA256 Fingerprint: %s\n" "${REMOTE_FPR}"
  if [[ -n "$LOCAL_FPR" && "$LOCAL_FPR" == "$REMOTE_FPR" ]]; then
    printf "%s  ✔ Fingerprints MATCH (no TLS terminator in the middle)%s\n" "$GREEN" "$NC"
  else
    printf "%s  ✖ Fingerprints DIFFER (LB/proxy or wrong cert being presented)%s\n" "$RED" "$NC"
  fi
  printf '%s' "$REMOTE_OUT" | openssl x509 -noout -subject -issuer -ext subjectAltName 2>/dev/null | sed 's/^/  /'
else
  printf "  (Could not retrieve remote certificate — port closed, firewall, or handshake blocked)\n"
fi
echo

# Restore strict mode
set -o pipefail
trap 'err "Failed at line $LINENO: $BASH_COMMAND"; exit 1' ERR

set -e
# ---------- end TLS quick diagnostics ----------
ok "Done."
