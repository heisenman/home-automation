#!/usr/bin/env python3
"""Mint a long-lived OPERATOR token for a wall panel (ADR-0019 / R9 ADR-0017).

Run on the DICTATOR (the box holding instance/auth_key — the R9 HS256 signing key). The panel presents the
token as `Authorization: Bearer <token>` when it POSTs /devices/<id>/command, and refreshes it via
POST /auth/refresh before it expires. Operator role can only REQUEST commands (the server still authorises
via policy + signs with the device secret) — it CANNOT change config, calibrate, override, or enroll nodes.

    python3 tools/mint_panel_token.py --sub panel-office --days 90

Provision the printed token onto the panel out-of-band (it is a bearer credential — treat it like a
password; keep it out of git). Rotate by re-minting (or rotate instance/auth_key to invalidate ALL tokens).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.api import auth_tokens as auth  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint an operator token for a wall panel.")
    ap.add_argument("--key", default="instance/auth_key", help="HS256 signing key path (default: instance/auth_key)")
    ap.add_argument("--sub", default="panel", help="token subject / identity, e.g. panel-office")
    ap.add_argument("--days", type=int, default=90, help="time-to-live in days (default: 90)")
    a = ap.parse_args()

    key_path = Path(a.key)
    if not key_path.exists():
        print(f"error: signing key not found at {key_path} — run on the dictator (it creates instance/auth_key "
              "when the control plane first mounts)", file=sys.stderr)
        return 2
    key = key_path.read_text().strip()
    token = auth.mint_token("operator", key, sub=a.sub, ttl_s=a.days * 86400)
    claims = auth.verify_token(token, key)
    print(token)
    print(f"# role=operator sub={claims['sub']} exp_epoch={claims['exp']} (~{a.days}d)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
