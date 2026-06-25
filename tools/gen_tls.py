#!/usr/bin/env python3
"""Generate a local TLS key + cert for ha-api (ADR-0017, R9). Air-gap-friendly: no external CA. Default is
a self-signed cert with SAN covering the VIP, the host name, and the box IP, so HTTPS works for the PWA over
the LAN (a secure context — unblocks crypto.subtle + ServiceWorker/Push). A local-CA variant can come later.

  python3 tools/gen_tls.py [--out-dir instance/tls] [--san 192.168.0.200,192.168.0.210,ha-dev] [--days 825] [--force]

Writes <out-dir>/server.key (0600) + server.crt. Point uvicorn at them: --ssl-keyfile/--ssl-certfile.
"""
import argparse
import datetime as _dt
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _san_entry(value: str):
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a local TLS key+cert for ha-api (ADR-0017)")
    ap.add_argument("--out-dir", default="instance/tls", type=Path)
    ap.add_argument("--san", default="192.168.0.200,192.168.0.210,ha-dev,localhost",
                    help="comma-separated SANs (IPs or DNS names) the cert is valid for")
    ap.add_argument("--days", type=int, default=825, help="validity (<=825 keeps modern clients happy)")
    ap.add_argument("--cn", default="ha-api", help="certificate common name")
    ap.add_argument("--force", action="store_true", help="overwrite existing key/cert")
    a = ap.parse_args()

    key_path, crt_path = a.out_dir / "server.key", a.out_dir / "server.crt"
    if (key_path.exists() or crt_path.exists()) and not a.force:
        print(f"refusing to overwrite {a.out_dir}/server.{{key,crt}} (use --force)", file=sys.stderr)
        sys.exit(1)

    sans = [_san_entry(s.strip()) for s in a.san.split(",") if s.strip()]
    key = ec.generate_private_key(ec.SECP256R1())
    # Fixed epoch (avoid wall-clock dependence); validity is a wide window from a stable not-before.
    not_before = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, a.cn)])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)            # self-signed
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_before + _dt.timedelta(days=a.days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))

    a.out_dir.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"wrote {key_path} (0600) + {crt_path}")
    print(f"  SANs: {a.san}")
    print(f"  uvicorn ... --ssl-keyfile {key_path} --ssl-certfile {crt_path}")


if __name__ == "__main__":
    main()
