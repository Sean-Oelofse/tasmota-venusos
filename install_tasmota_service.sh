#!/bin/bash
# =============================================================================
# install_tasmota_service.sh
# Installs tasmota_discovery.py as a daemontools service on Venus OS.
#
# Usage:
#   chmod +x install_tasmota_service.sh
#   ./install_tasmota_service.sh [options]
#
# Options:
#   --mqtt-host   MQTT broker host (default: 127.0.0.1)
#   --mqtt-port   MQTT broker port (default: 1883)
#   --mqtt-user   MQTT username    (default: empty)
#   --mqtt-pass   MQTT password    (default: empty)
#   --poll        Poll interval in seconds (default: 30)
#   --probe       Probe timeout in seconds (default: 15)
#   --uninstall   Remove the service instead of installing
# =============================================================================

set -e

SERVICE_NAME="tasmota-discovery"
INSTALL_DIR="/opt/victronenergy/tasmota-discovery"
SERVICE_DIR="/service/${SERVICE_NAME}"
SVCS_PERSISTENT="/data/conf/runit/${SERVICE_NAME}"
LOG_DIR="/var/log/${SERVICE_NAME}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISCOVERY_SCRIPT="${SCRIPT_DIR}/tasmota.py"

# Defaults
MQTT_HOST="127.0.0.1"
MQTT_PORT="1883"
MQTT_USER=""
MQTT_PASS=""
POLL_INTERVAL="30"
PROBE_TIMEOUT="15"
UNINSTALL=0

# -----------------------------------------------------------------------------
# Colour helpers
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mqtt-host)  MQTT_HOST="$2";     shift 2 ;;
        --mqtt-port)  MQTT_PORT="$2";     shift 2 ;;
        --mqtt-user)  MQTT_USER="$2";     shift 2 ;;
        --mqtt-pass)  MQTT_PASS="$2";     shift 2 ;;
        --poll)       POLL_INTERVAL="$2"; shift 2 ;;
        --probe)      PROBE_TIMEOUT="$2"; shift 2 ;;
        --uninstall)  UNINSTALL=1;        shift   ;;
        *)            die "Unknown option: $1" ;;
    esac
done

# -----------------------------------------------------------------------------
# Sanity checks
# -----------------------------------------------------------------------------
[[ "$(id -u)" -eq 0 ]] || die "This script must be run as root."

[[ -f /opt/victronenergy/version ]] \
    || warn "Venus OS not detected — proceeding anyway (dry-run environment?)"

# -----------------------------------------------------------------------------
# Uninstall path
# -----------------------------------------------------------------------------
if [[ $UNINSTALL -eq 1 ]]; then

    info "Uninstalling ${SERVICE_NAME}..."

    # Stop service if running
    if [[ -d "${SERVICE_DIR}" ]]; then
        sv stop "${SERVICE_DIR}" 2>/dev/null || true
        rm -rf "${SERVICE_DIR}"
        info "Removed ${SERVICE_DIR}"
    fi

    # Remove persistent runit entry
    if [[ -d "${SVCS_PERSISTENT}" ]]; then
        rm -rf "${SVCS_PERSISTENT}"
        info "Removed ${SVCS_PERSISTENT}"
    fi

    # Remove install directory
    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
        info "Removed ${INSTALL_DIR}"
    fi

    # Remove log directory
    if [[ -d "${LOG_DIR}" ]]; then
        rm -rf "${LOG_DIR}"
        info "Removed ${LOG_DIR}"
    fi

    info "Uninstall complete."
    exit 0
fi

# -----------------------------------------------------------------------------
# Pre-install checks
# -----------------------------------------------------------------------------
[[ -f "${DISCOVERY_SCRIPT}" ]] \
    || die "tasmota.py not found at ${DISCOVERY_SCRIPT}\nRun this script from the same directory as tasmota.py."

python3 --version &>/dev/null \
    || die "python3 not found."

python3 -c "import paho.mqtt.client" 2>/dev/null \
    || die "paho-mqtt not installed. Run: pip3 install paho-mqtt"

# -----------------------------------------------------------------------------
# Install files
# -----------------------------------------------------------------------------
info "Creating install directory: ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

info "Copying tasmota.py"
cp "${DISCOVERY_SCRIPT}" "${INSTALL_DIR}/tasmota.py"
chmod 755 "${INSTALL_DIR}/tasmota.py"

# Write environment file (sourced by the run script)
info "Writing environment config"
cat > "${INSTALL_DIR}/environment" <<EOF
TASMOTA_MQTT_HOST=${MQTT_HOST}
TASMOTA_MQTT_PORT=${MQTT_PORT}
TASMOTA_MQTT_USER=${MQTT_USER}
TASMOTA_MQTT_PASS=${MQTT_PASS}
TASMOTA_POLL_INTERVAL=${POLL_INTERVAL}
TASMOTA_PROBE_TIMEOUT=${PROBE_TIMEOUT}
LOG_LEVEL=INFO
EOF
chmod 600 "${INSTALL_DIR}/environment"

# -----------------------------------------------------------------------------
# Create runit service directory structure
#
# Venus OS uses daemontools/runit. Services live under /service/ but those
# are wiped on reboot. The persistent location is /data/conf/runit/ which
# rc.local symlinks back into /service/ on boot.
# -----------------------------------------------------------------------------
info "Creating runit service structure"
mkdir -p "${SVCS_PERSISTENT}/log"

# ---- run script ----
cat > "${SVCS_PERSISTENT}/run" <<'RUNEOF'
#!/bin/sh
# Source environment variables
if [ -f /opt/victronenergy/tasmota-discovery/environment ]; then
    set -a
    . /opt/victronenergy/tasmota-discovery/environment
    set +a
fi

exec 2>&1
exec python3 /opt/victronenergy/tasmota-discovery/tasmota.py
RUNEOF
chmod 755 "${SVCS_PERSISTENT}/run"

# ---- log/run script (svlogd rotating logger) ----
mkdir -p "${LOG_DIR}"
cat > "${SVCS_PERSISTENT}/log/run" <<LOGEOF
#!/bin/sh
exec svlogd -tt ${LOG_DIR}
LOGEOF
chmod 755 "${SVCS_PERSISTENT}/log/run"

# -----------------------------------------------------------------------------
# Symlink into /service/ so runit picks it up immediately
# -----------------------------------------------------------------------------
if [[ -L "${SERVICE_DIR}" ]]; then
    info "Removing existing symlink ${SERVICE_DIR}"
    rm "${SERVICE_DIR}"
fi

info "Symlinking ${SVCS_PERSISTENT} -> ${SERVICE_DIR}"
ln -s "${SVCS_PERSISTENT}" "${SERVICE_DIR}"

# -----------------------------------------------------------------------------
# Ensure /data/conf/runit is picked up on reboot via rc.local
# Venus OS already sources /data/rc.local on boot. We add a block that
# creates any missing /service symlinks from /data/conf/runit/.
# -----------------------------------------------------------------------------
RC_LOCAL="/data/rc.local"

RUNIT_BLOCK='# tasmota-discovery: restore runit service symlinks after reboot
for svc_dir in /data/conf/runit/*/; do
    svc_name="$(basename "$svc_dir")"
    if [ ! -e "/service/${svc_name}" ]; then
        ln -s "$svc_dir" "/service/${svc_name}"
    fi
done'

if [[ -f "${RC_LOCAL}" ]]; then
    if grep -q "tasmota-discovery" "${RC_LOCAL}"; then
        info "rc.local already contains service block — skipping"
    else
        info "Appending service restore block to ${RC_LOCAL}"
        echo "" >> "${RC_LOCAL}"
        echo "${RUNIT_BLOCK}" >> "${RC_LOCAL}"
    fi
else
    info "Creating ${RC_LOCAL}"
    cat > "${RC_LOCAL}" <<EOF
#!/bin/bash
${RUNIT_BLOCK}
EOF
    chmod +x "${RC_LOCAL}"
fi

# -----------------------------------------------------------------------------
# Start the service
# -----------------------------------------------------------------------------
info "Starting service via sv..."
sleep 1  # give runit a moment to notice the new symlink

if sv status "${SERVICE_DIR}" &>/dev/null; then
    sv restart "${SERVICE_DIR}"
else
    sv start "${SERVICE_DIR}" || warn "sv start returned non-zero — check 'sv status ${SERVICE_DIR}'"
fi

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Installation complete${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  Service dir : ${SVCS_PERSISTENT}"
echo "  Install dir : ${INSTALL_DIR}"
echo "  Log dir     : ${LOG_DIR}"
echo "  MQTT host   : ${MQTT_HOST}:${MQTT_PORT}"
echo ""
echo "  Useful commands:"
echo "    sv status ${SERVICE_DIR}    # check status"
echo "    sv restart ${SERVICE_DIR}   # restart"
echo "    sv stop ${SERVICE_DIR}      # stop"
echo "    tail -f ${LOG_DIR}/current  # live log"
echo ""
echo "  To update the script after changes:"
echo "    cp tasmota.py /opt/victronenergy/tasmota-discovery/"
echo "    ./install_tasmota_service.sh --uninstall"
echo ""
