#!/usr/bin/env python3
"""Run LM Trader with self-signed HTTPS (for Upstox OAuth on :8000)."""

from pathlib import Path

import uvicorn

CERT_DIR = Path(__file__).resolve().parent / "certs"
CERT_FILE = CERT_DIR / "mypi.crt"
KEY_FILE = CERT_DIR / "mypi.key"


def main() -> None:
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        raise SystemExit(
            "Missing TLS cert files. Run: ./scripts/generate_certs.sh mypi.tail9906f3.ts.net"
        )

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        ssl_certfile=str(CERT_FILE),
        ssl_keyfile=str(KEY_FILE),
    )


if __name__ == "__main__":
    main()
