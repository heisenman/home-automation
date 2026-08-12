"""ADR-0038 — the Android shell PINS the server certificate, so the bundled copy is load-bearing.

The app ships `instance/tls/server.crt` inside the APK as its only trust anchor for the house
(`app/android/.../res/xml/network_security_config.xml`). That is what buys a genuine secure context on a
phone without installing a CA profile per device — but it also means the app's trust is a **frozen copy**
of a file that lives outside its build.

Two ways that silently breaks a phone in the field, both guarded here:

1. **The server cert is rotated and the APK is not rebuilt.** Every phone stops connecting, and the app
   is right to refuse — the failure looks like "the app is broken", not "the pin is stale".
2. **The cert expires.** This one has a date on it already: ADR-0033 flags TLS expiry as one of the
   air-gap lifecycle time-bombs that silently kill an isolated box over months. A test that only fails
   *after* expiry is a test that tells you on the worst possible day, so the tripwire is 90 days early.

A third guard covers a subtler one: a host may only be pinned if the certificate actually vouches for it.
Adding a domain to the network-security config that is absent from the cert's SAN list produces an app
that refuses the very address it was just told to use.
"""
import datetime as dt
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVER_CRT = REPO / "instance" / "tls" / "server.crt"
BUNDLED_CRT = REPO / "app" / "android" / "app" / "src" / "main" / "res" / "raw" / "ha_server.crt"
NSC = REPO / "app" / "android" / "app" / "src" / "main" / "res" / "xml" / "network_security_config.xml"

# How much warning we want before the pinned cert stops working. Long enough to rotate the cert, rebuild
# the APK, and get it onto every phone in the house without anyone being locked out mid-week.
EXPIRY_WARNING_DAYS = 90


def _openssl(crt: Path, *args: str) -> str:
    return subprocess.run(
        ["openssl", "x509", "-in", str(crt), "-noout", *args],
        capture_output=True, text=True, check=True,
    ).stdout


def test_bundled_cert_exists():
    assert BUNDLED_CRT.exists(), (
        f"{BUNDLED_CRT.relative_to(REPO)} is missing — the app would have no trust anchor for the house "
        "and could not reach it at all."
    )


def test_bundled_cert_matches_the_server_cert():
    """The pin must be a copy of what the server actually presents, byte for byte."""
    if not SERVER_CRT.exists():
        pytest.skip(f"{SERVER_CRT.relative_to(REPO)} not present on this box")
    assert BUNDLED_CRT.read_bytes() == SERVER_CRT.read_bytes(), (
        "The Android app pins a STALE certificate. Every phone will refuse to connect until the APK is "
        f"rebuilt. Fix: cp {SERVER_CRT.relative_to(REPO)} {BUNDLED_CRT.relative_to(REPO)}, rebuild "
        "(see docs/app/ANDROID.md), and reinstall on each phone."
    )


def test_pinned_cert_is_not_expired_or_about_to_be():
    if not BUNDLED_CRT.exists():
        pytest.skip("no bundled cert")
    raw = _openssl(BUNDLED_CRT, "-enddate").strip()          # notAfter=Apr  5 00:00:00 2028 GMT
    not_after = dt.datetime.strptime(
        raw.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=dt.timezone.utc)
    days_left = (not_after - dt.datetime.now(dt.timezone.utc)).days
    assert days_left > 0, (
        f"The pinned certificate EXPIRED {abs(days_left)} days ago ({not_after:%Y-%m-%d}). Every phone "
        "running the app is locked out of the house right now."
    )
    assert days_left > EXPIRY_WARNING_DAYS, (
        f"The pinned certificate expires in {days_left} days ({not_after:%Y-%m-%d}). Rotate "
        "instance/tls/server.crt, rebuild the APK, and redistribute BEFORE that date — after it, the app "
        "fails closed on every phone at once. (ADR-0033 air-gap lifecycle time-bomb.)"
    )


def test_every_pinned_host_is_covered_by_the_certificate():
    """A pinned domain the cert does not vouch for is an address the app can never successfully use."""
    if not BUNDLED_CRT.exists() or not NSC.exists():
        pytest.skip("android app not present")
    san = _openssl(BUNDLED_CRT, "-ext", "subjectAltName")
    covered = {
        part.split(":", 1)[1].strip()
        for part in san.replace("\n", ",").split(",")
        if part.strip().startswith(("IP Address:", "DNS:"))
    }
    pinned = {d.text.strip() for d in ET.parse(NSC).getroot().iter("domain") if d.text}
    assert pinned, "network_security_config.xml pins no domains — the app trusts nothing for the house."
    missing = pinned - covered
    assert not missing, (
        f"network_security_config.xml pins {sorted(missing)}, which the bundled certificate does not "
        f"cover (SANs: {sorted(covered)}). The app would refuse its own configured endpoint. Either add "
        "the host to the cert's SAN list and reissue, or stop pinning it."
    )


def test_only_the_public_certificate_is_bundled():
    """The APK is a redistributable artifact — a private key must never ride along in it."""
    raw_dir = BUNDLED_CRT.parent
    if not raw_dir.exists():
        pytest.skip("android app not present")
    for f in raw_dir.iterdir():
        body = f.read_text(errors="ignore")
        assert "PRIVATE KEY" not in body, (
            f"{f.relative_to(REPO)} contains private key material and would be shipped to every phone."
        )
