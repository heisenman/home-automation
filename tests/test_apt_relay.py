"""tools/ha2_apt_relay.py — the pure parts: sources parsing and apt's list-file naming."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("ha2_apt_relay", Path(__file__).parent.parent / "tools" / "ha2_apt_relay.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def test_sources_one_line_and_deb822_binary_only():
    text = """deb http://deb.debian.org/debian trixie main non-free-firmware
deb-src http://deb.debian.org/debian trixie main
# deb http://example.org/x y z

Types: deb
URIs: https://deb.nodesource.com/node_22.x
Suites: nodistro
Components: main
Signed-By: /usr/share/keyrings/nodesource.gpg
"""
    s = R.binary_sources(text)
    assert [(x["uri"], x["suite"], x["comps"]) for x in s] == [
        ("http://deb.debian.org/debian", "trixie", ["main", "non-free-firmware"]),
        ("https://deb.nodesource.com/node_22.x", "nodistro", ["main"])]
    assert s[1]["signed_by"] == "/usr/share/keyrings/nodesource.gpg"


def test_lists_name_matches_apt():
    assert R.lists_name("https://deb.nodesource.com/node_22.x") == "deb.nodesource.com_node%5f22.x"
    assert R.lists_name("http://security.debian.org/debian-security") == "security.debian.org_debian-security"
