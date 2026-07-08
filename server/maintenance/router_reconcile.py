#!/usr/bin/env python3
"""router_reconcile.py — config-as-code reconciler for the air-gap OpenWRT router(s).

The router is a first-class DICTATOR-MANAGED device (DJ-6): its config-of-record is the committed
UCI in provisioning/openwrt/etc/config/* + the gitignored secrets/MACs in instance/openwrt/<router>.env.
This tool asserts the router matches that record, corrects drift, and checks health — the same
render → push → reconcile pattern as the edge firmware. Run ON the dictator (ha-2), which reaches the
router over the id_cluster back-channel.

Subcommands:
  check   (default)  report drift vs the config-of-record; exit 1 if any drift
  apply              correct drift (uci set + commit + reload); idempotent
  health             assert the router is serving (LAN IP, DHCP, radios/SSID, reservations, WAN off)

See docs/airgap/MIGRATION-DESIGN-LOG.md (DJ-6, DJ-15).
"""
import argparse, os, subprocess, sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_ENV = os.path.join(REPO, "instance", "openwrt", "airgap_router.env")
DEFAULT_KEY = os.path.expanduser("~/.ssh/id_cluster")
DEFAULT_ROUTER = "192.168.1.1"

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"; X = "\033[0m"


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.split("#", 1)[0].strip().strip('"').strip("'")
    return env


def ssh(host, key, cmd):
    r = subprocess.run(
        ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8", f"root@{host}", cmd],
        capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def invariants(env):
    """The air-gap config-of-record as scalar uci key -> expected value.
    Wireless radio paths/keys are hardware/secret and left to the device (never blind-overwritten)."""
    return [
        ("network.lan.ipaddr", "192.168.1.1"),
        ("network.lan.netmask", "255.255.255.0"),
        ("dhcp.lan.start", "100"),
        ("dhcp.lan.limit", "50"),
        ("dhcp.@dnsmasq[0].noresolv", "1"),
        ("dhcp.wan.ignore", "1"),
        ("system.@system[0].hostname", "ha-router-airgap"),
        ("wireless.default_radio0.ssid", "autohome_airgap"),
        ("wireless.default_radio0.hidden", "1"),
        ("wireless.default_radio0.isolate", "0"),
        ("wireless.default_radio1.ssid", "autohome_airgap"),
        ("wireless.default_radio1.hidden", "1"),
        ("wireless.default_radio1.isolate", "0"),
    ]


def get_drift(host, key):
    drift = []
    for k, want in invariants(load_env(DEFAULT_ENV)):
        rc, got, _ = ssh(host, key, f"uci -q get {k}")
        got = got if rc == 0 else "<unset>"
        if got != want:
            drift.append((k, got, want))
    return drift


def cmd_check(host, key):
    drift = get_drift(host, key)
    if not drift:
        print(f"{G}✓ router {host}: no drift — matches config-of-record{X}")
        return 0
    print(f"{R}✗ router {host}: {len(drift)} drifted setting(s):{X}")
    for k, got, want in drift:
        print(f"  {k}: {R}{got}{X} -> should be {G}{want}{X}")
    return 1


def cmd_apply(host, key):
    drift = get_drift(host, key)
    if not drift:
        print(f"{G}✓ router {host}: already in sync — nothing to apply{X}")
        return 0
    print(f"{Y}applying {len(drift)} correction(s) to {host}...{X}")
    sets = "; ".join(f"uci set {k}='{want}'" for k, _, want in drift)
    rc, out, err = ssh(host, key, f"{sets}; uci commit; /etc/init.d/dnsmasq reload; reload_config")
    if rc != 0:
        print(f"{R}✗ apply failed:{X} {err or out}")
        return 1
    for k, got, want in drift:
        print(f"  {G}fixed{X} {k}: {got} -> {want}")
    # re-check
    return cmd_check(host, key)


def cmd_health(host, key):
    env = load_env(DEFAULT_ENV)
    checks = []

    def ck(name, ok):
        checks.append((name, ok))

    rc, lan, _ = ssh(host, key, "uci -q get network.lan.ipaddr")
    ck("LAN IP == 192.168.1.1", lan == "192.168.1.1")
    rc, _, _ = ssh(host, key, "pgrep dnsmasq >/dev/null")
    ck("dnsmasq (DHCP/DNS) running", rc == 0)
    rc, wan, _ = ssh(host, key, "uci -q get network.wan")
    ck("WAN absent/disabled (air gap)", wan in ("", "<unset>") or rc != 0 or
       ssh(host, key, "uci -q get network.wan.disabled")[1] == "1")
    rc, up, _ = ssh(host, key, "iwinfo 2>/dev/null | grep -c ESSID || echo 0")
    ck("at least one radio up (ESSID)", up.isdigit() and int(up) >= 1)
    rc, ssid, _ = ssh(host, key, "iwinfo 2>/dev/null | grep -oE 'ESSID: \"[^\"]*\"' | head -1")
    ck("broadcasting autohome_airgap", "autohome_airgap" in ssid)
    # ha-2 reservation present (dictator must come up at .1.210)
    ha2_mac = env.get("HA2_MAC", "").lower()
    rc, hosts, _ = ssh(host, key, "uci show dhcp | grep -iE \"192.168.1.210|" + ha2_mac + "\"")
    ck("ha-2 reservation (.1.210) present", "192.168.1.210" in hosts and (not ha2_mac or ha2_mac in hosts.lower()))

    fails = [n for n, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {G}✓{X} {name}" if ok else f"  {R}✗{X} {name}")
    if fails:
        print(f"{R}✗ router {host}: {len(fails)} health check(s) failed{X}")
        return 1
    print(f"{G}✓ router {host}: healthy ({len(checks)} checks){X}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", default="check", choices=["check", "apply", "health"])
    ap.add_argument("--router", default=os.environ.get("AIRGAP_ROUTER", DEFAULT_ROUTER))
    ap.add_argument("--key", default=os.environ.get("CLUSTER_KEY", DEFAULT_KEY))
    args = ap.parse_args()

    rc, _, err = ssh(args.router, args.key, "true")
    if rc != 0:
        print(f"{R}✗ cannot reach router {args.router} over ssh: {err}{X}", file=sys.stderr)
        return 2

    return {"check": cmd_check, "apply": cmd_apply, "health": cmd_health}[args.action](args.router, args.key)


if __name__ == "__main__":
    sys.exit(main())
