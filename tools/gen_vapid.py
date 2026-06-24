#!/usr/bin/env python3
"""Generate a VAPID (RFC 8292) P-256 keypair for Web Push. Writes instance/vapid.json (gitignored —
it holds the PRIVATE key; never commit it). The `public` value is the base64url uncompressed EC point
the browser subscribes with (applicationServerKey); `private` is the base64url raw scalar.

  python3 tools/gen_vapid.py [--out instance/vapid.json] [--subject mailto:you@example.com] [--force]
"""
import argparse
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a VAPID keypair for Web Push")
    ap.add_argument("--out", default="instance/vapid.json", type=Path)
    ap.add_argument("--subject", default="mailto:hugh.eisenman@gmail.com",
                    help="VAPID 'sub' contact (mailto: or https:)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing keyfile")
    a = ap.parse_args()
    if a.out.exists() and not a.force:
        print(f"refusing to overwrite {a.out} (use --force)", file=sys.stderr)
        sys.exit(1)
    priv = ec.generate_private_key(ec.SECP256R1())
    d = priv.private_numbers().private_value.to_bytes(32, "big")
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({"public": _b64u(point), "private": _b64u(d), "subject": a.subject}, indent=2))
    a.out.chmod(0o600)
    print(f"wrote {a.out} (mode 600)")
    print(f"public key (applicationServerKey): {_b64u(point)}")


if __name__ == "__main__":
    main()
