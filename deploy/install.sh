#!/bin/sh
# Install files only. Never stop processes, configure networking or start units.
set -eu
umask 077

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script with sudo." >&2
    exit 1
fi
if [ "$#" -gt 1 ]; then
    echo "Usage: sudo sh deploy/install.sh [path/to/config.json]" >&2
    exit 1
fi

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_DIR=/opt/onvif-rtsp-bridge
CONFIG_DIR=/etc/onvif-rtsp-bridge
if [ "$#" -eq 1 ]; then
    CONFIG_FILE=$1
elif [ -f "$CONFIG_DIR/config.json" ]; then
    CONFIG_FILE="$CONFIG_DIR/config.json"
else
    CONFIG_FILE="$PROJECT_DIR/config/config.json"
fi
UNIT_DIR=/etc/systemd/system
SERVICE_USER=onvif-bridge
PYTHON=/usr/bin/python3
SOURCE_FILES="bridge_config.py bridge_network.py bridge_xml.py onvif_proxy.py rtsp_proxy.py"
UNIT_FILES="bridge-network.service onvif-bridge.service rtsp-bridge.service onvif-rtsp-bridge.target"

for tool in install cp mktemp date getent adduser systemctl chown chmod; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Required command is missing: $tool" >&2
        exit 1
    fi
done
[ -x "$PYTHON" ] || { echo "Missing $PYTHON" >&2; exit 1; }
[ -x /sbin/ip ] || { echo "Missing /sbin/ip" >&2; exit 1; }
[ -r "$CONFIG_FILE" ] || { echo "Configuration file is not readable." >&2; exit 1; }
for name in $SOURCE_FILES; do
    [ -r "$PROJECT_DIR/src/$name" ] || { echo "Missing source file: $name" >&2; exit 1; }
done
for name in $UNIT_FILES; do
    [ -r "$PROJECT_DIR/deploy/$name" ] || { echo "Missing unit file: $name" >&2; exit 1; }
done

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 5) else 1)'
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$PROJECT_DIR/src/onvif_proxy.py" \
    --config "$CONFIG_FILE" --check-config

# Stop with the installed configuration before replacing an active installation.
# ExecStop needs the old configuration to remove its existing interfaces.
for name in $UNIT_FILES; do
    if systemctl is-active --quiet "$name" 2>/dev/null; then
        echo "Installation stopped: $name is active." >&2
        echo "Stop onvif-rtsp-bridge.target with its old config before reinstalling." >&2
        exit 1
    fi
done

install -d -m 700 -o root -g root /var/backups/onvif-rtsp-bridge
BACKUP_DIR=$(mktemp -d "/var/backups/onvif-rtsp-bridge/install-$(date +%Y%m%d-%H%M%S)-XXXXXX")
install -d -m 700 "$BACKUP_DIR/src" "$BACKUP_DIR/units"
for name in $SOURCE_FILES; do
    if [ -f "$APP_DIR/src/$name" ]; then
        cp -p "$APP_DIR/src/$name" "$BACKUP_DIR/src/$name"
    fi
done
for name in $UNIT_FILES; do
    if [ -f "$UNIT_DIR/$name" ]; then
        cp -p "$UNIT_DIR/$name" "$BACKUP_DIR/units/$name"
    fi
done
if [ -f "$CONFIG_DIR/config.json" ]; then
    cp -p "$CONFIG_DIR/config.json" "$BACKUP_DIR/config.json"
fi

if ! getent passwd "$SERVICE_USER" >/dev/null; then
    adduser --system --group --no-create-home "$SERVICE_USER"
fi
if ! getent group "$SERVICE_USER" >/dev/null; then
    echo "The service group is missing: $SERVICE_USER" >&2
    exit 1
fi

install -d -m 755 -o root -g root "$APP_DIR" "$APP_DIR/src"
for name in $SOURCE_FILES; do
    install -m 644 -o root -g root "$PROJECT_DIR/src/$name" "$APP_DIR/src/$name"
done
install -d -m 750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
if "$PYTHON" -c 'import os, sys; sys.exit(0 if os.path.exists(sys.argv[2]) and os.path.samefile(sys.argv[1], sys.argv[2]) else 1)' \
    "$CONFIG_FILE" "$CONFIG_DIR/config.json"; then
    # GNU install rejects copying a file onto itself. Keep the configuration bytes.
    echo "Keeping the installed configuration."
    chown root:"$SERVICE_USER" "$CONFIG_DIR/config.json"
    chmod 640 "$CONFIG_DIR/config.json"
else
    install -m 640 -o root -g "$SERVICE_USER" "$CONFIG_FILE" "$CONFIG_DIR/config.json"
fi
for name in $UNIT_FILES; do
    install -m 644 -o root -g root "$PROJECT_DIR/deploy/$name" "$UNIT_DIR/$name"
done
systemctl daemon-reload

echo "Installation complete. Backups: $BACKUP_DIR"
echo "No processes were stopped and no services were started or enabled."
echo "Network settings and the uploaded project files were not changed."
echo "Next: stop the manual ONVIF/RTSP processes with Ctrl+C, then run:"
echo "  sudo systemctl start onvif-rtsp-bridge.target"
echo "  sudo systemctl status bridge-network onvif-bridge rtsp-bridge --no-pager"
echo "Enable boot startup only after checking the video:"
echo "  sudo systemctl enable onvif-rtsp-bridge.target"
