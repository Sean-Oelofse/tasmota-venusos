#!/bin/bash
# =============================================================================
# install_tasmota_service.sh
# Installs tasmota.py as a daemontools service on Venus OS.
#
# One-line install from GitHub (recommended):
#   bash <(curl -fsSL https://raw.githubusercontent.com/Sean-Oelofse/tasmota-venusos/main/install_tasmota_service.sh) --mqtt-host 127.0.0.1
#
# Or clone and run locally:
#   git clone https://github.com/Sean-Oelofse/tasmota-venusos.git
#   cd tasmota-venusos
#   chmod +x install_tasmota_service.sh
#   ./install_tasmota_service.sh --mqtt-host 127.0.0.1
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

GITHUB_RAW="https://raw.githubusercontent.com/Sean-Oelofse/tasmota-venusos/main"

SERVICE_NAME="tasmota-discovery"
INSTALL_DIR="/opt/victronenergy/tasmota-discovery"
SERVICE_DIR="/service/${SERVICE_NAME}"
SVCS_PERSISTENT="/data/conf/runit/${SERVICE_NAME}"
LOG_DIR="/var/log/${SERVICE_NAME}"
LOCK_FILE="/run/${SERVICE_NAME}.lock"

# If tasmota.py lives next to this script (local clone), use it directly.
# Otherwise the installer will download it from GitHub.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_SCRIPT="${SCRIPT_DIR}/tasmota.py"

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
# Service teardown
#
# daemontools/runit never kill an orphaned 'supervise' on their own. Every time
# the /service symlink or the supervise/ lock is recreated, svscan starts a
# fresh supervise while the old one lingers - each with its own python child.
# Re-running this installer used to pile up orphaned supervisors, which is the
# "multiple instances" bug. Always tear down cleanly before (re)creating so a
# reinstall can never leave two supervisors (or two python workers) behind.
# -----------------------------------------------------------------------------
teardown_service() {
    # Ask the tracked supervisor(s) to bring the service down and exit.
    if [[ -e "${SERVICE_DIR}" ]]; then
        svc -dx "${SERVICE_DIR}/log" 2>/dev/null || true
        svc -dx "${SERVICE_DIR}"     2>/dev/null || true
        sv  stop "${SERVICE_DIR}"    2>/dev/null || true
    fi

    # Remove the symlink so svscan won't respawn supervise while we clean up.
    [[ -L "${SERVICE_DIR}" ]] && rm -f "${SERVICE_DIR}"

    sleep 2

    # Kill any orphaned supervisors and python workers left by earlier installs.
    # Match on the service name / install path only, so unrelated services are
    # never touched.
    local pids
    pids="$(ps w 2>/dev/null \
        | grep -E "supervise ${SERVICE_NAME}|${INSTALL_DIR}/tasmota\.py" \
        | grep -v grep \
        | awk '{print $1}')"
    if [[ -n "${pids}" ]]; then
        warn "Killing leftover ${SERVICE_NAME} processes: $(echo ${pids})"
        kill ${pids} 2>/dev/null || true
        sleep 2
        kill -9 ${pids} 2>/dev/null || true
    fi

    rm -f "${LOCK_FILE}"
}

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

    # Stop and remove any (possibly orphaned) supervisors + workers first.
    teardown_service
    info "Stopped and removed supervisors for ${SERVICE_NAME}"

    if [[ -d "${SVCS_PERSISTENT}" ]]; then
        rm -rf "${SVCS_PERSISTENT}"
        info "Removed ${SVCS_PERSISTENT}"
    fi

    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
        info "Removed ${INSTALL_DIR}"
    fi

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
python3 --version &>/dev/null \
    || die "python3 not found."

python3 -c "import paho.mqtt.client" 2>/dev/null \
    || die "paho-mqtt not installed. Run: pip3 install paho-mqtt"

# -----------------------------------------------------------------------------
# Tear down any existing (or orphaned) supervisors before touching files
# -----------------------------------------------------------------------------
# This makes reinstalls idempotent: we always start from a clean slate with no
# lingering supervise/python, instead of stacking a new supervisor on top of
# the old ones. SVCS_PERSISTENT (run scripts, any hand-edited config) is left
# intact - only the running processes and the /service symlink are cleared.
info "Clearing any existing ${SERVICE_NAME} supervisors"
teardown_service

# -----------------------------------------------------------------------------
# Fetch tasmota.py — local copy takes priority, else download from GitHub
# -----------------------------------------------------------------------------
mkdir -p "${INSTALL_DIR}"

if [[ -f "${LOCAL_SCRIPT}" ]]; then
    info "Using local tasmota.py from ${LOCAL_SCRIPT}"
    cp "${LOCAL_SCRIPT}" "${INSTALL_DIR}/tasmota.py"
else
    info "tasmota.py not found locally — downloading from GitHub"
    curl --fail --silent --show-error --location \
        "${GITHUB_RAW}/tasmota.py" \
        -o "${INSTALL_DIR}/tasmota.py" \
        || die "Failed to download tasmota.py from GitHub. Check your internet connection."
    info "Downloaded tasmota.py"
fi

chmod 755 "${INSTALL_DIR}/tasmota.py"

# -----------------------------------------------------------------------------
# Write environment config
# -----------------------------------------------------------------------------
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
# Create runit service structure
# /data/conf/runit/ is persistent across firmware updates.
# /service/ is tmpfs — symlinks are restored on boot via rc.local.
# -----------------------------------------------------------------------------
info "Creating runit service structure"
mkdir -p "${SVCS_PERSISTENT}/log"

cat > "${SVCS_PERSISTENT}/run" <<'RUNEOF'
#!/bin/sh
if [ -f /opt/victronenergy/tasmota-discovery/environment ]; then
    set -a
    . /opt/victronenergy/tasmota-discovery/environment
    set +a
fi
exec 2>&1
exec python3 /opt/victronenergy/tasmota-discovery/tasmota.py
RUNEOF
chmod 755 "${SVCS_PERSISTENT}/run"

mkdir -p "${LOG_DIR}"
cat > "${SVCS_PERSISTENT}/log/run" <<LOGEOF
#!/bin/sh
exec svlogd -tt ${LOG_DIR}
LOGEOF
chmod 755 "${SVCS_PERSISTENT}/log/run"

# -----------------------------------------------------------------------------
# Symlink into /service/ for immediate pickup by runit
# -----------------------------------------------------------------------------
if [[ -L "${SERVICE_DIR}" ]]; then
    info "Removing existing symlink ${SERVICE_DIR}"
    rm "${SERVICE_DIR}"
fi

info "Symlinking ${SVCS_PERSISTENT} -> ${SERVICE_DIR}"
ln -s "${SVCS_PERSISTENT}" "${SERVICE_DIR}"

# -----------------------------------------------------------------------------
# Patch /data/rc.local to restore symlinks on every reboot
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
sleep 1

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
echo "  To update to the latest version from GitHub:"
echo "    bash <(curl -fsSL ${GITHUB_RAW}/install_tasmota_service.sh) --mqtt-host ${MQTT_HOST}"
echo ""
echo "  To uninstall:"
echo "    bash <(curl -fsSL ${GITHUB_RAW}/install_tasmota_service.sh) --uninstall"
echo ""
