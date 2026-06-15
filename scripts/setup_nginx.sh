#!/usr/bin/env bash
# Set up nginx as a reverse proxy for LM Trader.
# Run once after installing nginx:  sudo apt install nginx -y
#
# Usage:  sudo bash scripts/setup_nginx.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_SRC="${ROOT}/nginx/lm-trader.conf"
CONF_DST="/etc/nginx/sites-available/lm-trader"
LINK_DST="/etc/nginx/sites-enabled/lm-trader"

# ── 1. Check certs exist ──────────────────────────────────────────────────────
if [[ ! -f "${ROOT}/certs/mypi.crt" ]]; then
    echo "[ERROR] TLS cert not found.  Generate it first:"
    echo "        bash scripts/generate_certs.sh mypi.tail9906f3.ts.net"
    exit 1
fi

# ── 2. Install nginx if needed ────────────────────────────────────────────────
if ! command -v nginx &>/dev/null; then
    echo "[INFO] Installing nginx…"
    apt-get install -y nginx
fi

# ── 3. Disable the default site ───────────────────────────────────────────────
DEFAULT_LINK="/etc/nginx/sites-enabled/default"
if [[ -L "${DEFAULT_LINK}" ]]; then
    echo "[INFO] Removing nginx default site…"
    rm -f "${DEFAULT_LINK}"
fi

# ── 4. Install the LM Trader config ───────────────────────────────────────────
echo "[INFO] Installing ${CONF_DST}…"
cp "${CONF_SRC}" "${CONF_DST}"

if [[ ! -L "${LINK_DST}" ]]; then
    ln -sf "${CONF_DST}" "${LINK_DST}"
fi

# ── 5. Test and reload ────────────────────────────────────────────────────────
echo "[INFO] Testing nginx config…"
nginx -t

echo "[INFO] Reloading nginx…"
systemctl reload nginx
systemctl enable nginx

echo ""
echo "✓ nginx is now proxying HTTPS :443 → uvicorn :8000"
echo "  Open https://$(hostname) or https://$(hostname -I | awk '{print $1}')"
