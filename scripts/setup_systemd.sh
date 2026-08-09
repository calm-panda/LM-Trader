#!/usr/bin/env bash
# Install LM Trader as a systemd service (auto-restart on boot/crash).
#
# Usage:  sudo bash scripts/setup_systemd.sh
#
# Edit deploy/lm-trader.service first if your paths/user differ.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${ROOT}/deploy/lm-trader.service"
UNIT_DST="/etc/systemd/system/lm-trader.service"

if [[ ! -f "${UNIT_SRC}" ]]; then
    echo "[ERROR] Missing ${UNIT_SRC}"
    exit 1
fi

if [[ ! -x "${ROOT}/.venv/bin/uvicorn" ]]; then
    echo "[ERROR] ${ROOT}/.venv/bin/uvicorn not found. Create the venv and install deps first."
    exit 1
fi

echo "[INFO] Installing ${UNIT_DST}…"
cp "${UNIT_SRC}" "${UNIT_DST}"

# Rewrite WorkingDirectory / paths if the repo is not under /home/panda/LM-Trader
if [[ "${ROOT}" != "/home/panda/LM-Trader" ]]; then
    echo "[INFO] Rewriting unit paths for ROOT=${ROOT}"
    OWNER="$(stat -c '%U' "${ROOT}")"
    sed -i \
        -e "s|/home/panda/LM-Trader|${ROOT}|g" \
        -e "s|^User=.*|User=${OWNER}|" \
        -e "s|^Group=.*|Group=${OWNER}|" \
        "${UNIT_DST}"
fi

systemctl daemon-reload
systemctl enable lm-trader.service
systemctl restart lm-trader.service

echo ""
echo "✓ lm-trader.service installed and started"
echo "  status:  systemctl status lm-trader"
echo "  logs:    journalctl -u lm-trader -f"
echo "  alerts:  ${ROOT}/data/alerts.log"
