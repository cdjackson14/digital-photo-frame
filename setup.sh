#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"

echo "==> Installing system dependencies for Raspberry Pi Digital Photo Frame..."
sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  python3-pygame \
  python3-pil \
  python3-flask \
  libsdl2-2.0-0 \
  libsdl2-image-2.0-0 \
  libsdl2-ttf-2.0-0 \
  fonts-dejavu-core

echo "==> Adding user '${CURRENT_USER}' to video/render/input groups for KMSDRM display access..."
sudo usermod -aG video,render,input "${CURRENT_USER}" || true

echo "==> Creating Python virtual environment (reusing system pygame/Pillow packages for fast install on Pi Zero 2 W)..."
python3 -m venv --system-site-packages "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> Creating photos directory..."
mkdir -p "${INSTALL_DIR}/photos"

echo "==> Installing systemd service (photoframe.service)..."
sed \
  -e "s|__USER__|${CURRENT_USER}|g" \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  "${INSTALL_DIR}/photoframe.service" | sudo tee /etc/systemd/system/photoframe.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable photoframe.service

echo ""
echo "==> Setup complete!"
echo "    Reboot your Raspberry Pi to start the photo frame automatically:"
echo "      sudo reboot"
echo ""
echo "    Or start it right now with:"
echo "      sudo systemctl start photoframe.service"
echo ""
echo "    Then open the web interface from your phone or computer:"
echo "      http://$(hostname).local:5000"
echo "      (or http://$(hostname -I | awk '{print $1}'):5000)"
