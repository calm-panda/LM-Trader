#!/usr/bin/env bash
# Generate a self-signed TLS cert for local HTTPS (Upstox OAuth redirect).
# ./scripts/generate_certs.sh mypi.tail9906f3.ts.net

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="${ROOT}/certs"
HOSTNAME="${1:-mypi.tail9906f3.ts.net}"

mkdir -p "${CERT_DIR}"

openssl req -x509 -newkey rsa:4096 -sha256 -days 365 -nodes \
  -keyout "${CERT_DIR}/mypi.key" \
  -out "${CERT_DIR}/mypi.crt" \
  -subj "/CN=${HOSTNAME}" \
  -addext "subjectAltName=DNS:${HOSTNAME}"

echo "Wrote ${CERT_DIR}/mypi.crt and ${CERT_DIR}/mypi.key for ${HOSTNAME}"
