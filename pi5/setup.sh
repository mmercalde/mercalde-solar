#!/bin/bash
# Solar Dashboard Setup Script for Pi 5
# Run as: sudo ./setup.sh

set -e

INSTALL_DIR="/home/pi/solar_dashboard"
SERVICE_NAME="solar-dashboard"

echo "=== Solar Dashboard Setup ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo ./setup.sh"
    exit 1
fi

# Create install directory
echo "Creating installation directory..."
mkdir -p "$INSTALL_DIR"
cp app.py "$INSTALL_DIR/"
cp schneider_modbus.py "$INSTALL_DIR/"
cp requirements.txt "$INSTALL_DIR/"
cp solar-dashboard.service "$INSTALL_DIR/"
chown -R pi:pi "$INSTALL_DIR"

# Create virtual environment
echo "Creating Python virtual environment..."
sudo -u pi python3 -m venv "$INSTALL_DIR/venv"

# Install dependencies
echo "Installing Python dependencies..."
sudo -u pi "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u pi "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# Install systemd service
echo "Installing systemd service..."
cp solar-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Commands:"
echo "  Start:   sudo systemctl start $SERVICE_NAME"
echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
echo "  Status:  sudo systemctl status $SERVICE_NAME"
echo "  Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "Dashboard will be available at: http://<pi-ip>:8080"
echo ""
echo "To start now: sudo systemctl start $SERVICE_NAME"
