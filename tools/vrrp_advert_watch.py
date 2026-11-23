#!/usr/bin/env python3
"""Passive diagnostic: why does a keepalived BACKUP miss the MASTER's VRRP adverts on an interface?

Listens on IFACE (root, read-only: sends nothing), and when the gap between two adverts from one sender exceeds
--gap seconds, writes the gap plus a per-second table of what the interface heard around it:

  uni / bc / mc   frames received addressed to this host / broadcast / multicast
  inactive_ms     `iw station dump` "inactive time" (ms since the AP was last heard), WiFi only
  rx_pk tx_retry  station counters' change over the second

Reading the table: multicast at 0 while unicast keeps arriving = group-frame delivery (client power save / DTIM,
or the AP); everything at 0 with inactive_ms climbing = the radio went deaf (scan, driver stall, interference).

Written 2026-11-23 for the air-gap standby's false promotions (docs/FOLLOWUPS.md). Run:
  sudo systemd-run --unit=diag-vrrp-advert-watch --collect \
      /usr/bin/python3 tools/vrrp_advert_watch.py wlp2s0 instance/vrrp-advert-watch.log
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import re
import socket
import subprocess
import threading
import time

VRRP = 112
KEEP_S = 60


def station(iface: str) -> dict:
    try:
        out = subprocess.run(["iw", "dev", iface, "station", "dump"], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return {}
    g = lambda k: (re.search(rf"{k}:\s+(-?\d+)", out) or [None, None])[1]
    return {k: int(v) for k, v in (("inactive_ms", g("inactive time")), ("rx_pk", g("rx packets")),
                                   ("tx_retry", g("tx retries")), ("signal", g("signal"))) if v is not None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("iface")
    ap.add_argument("out")
    ap.add_argument("--gap", type=float, default=1.5, help="report advert gaps longer than this (s)")
    a = ap.parse_args()
    secs: dict[int, dict] = collections.OrderedDict()       # epoch second -> counters
    lock = threading.Lock()

    def slot(t: float) -> dict:
        s = secs.setdefault(int(t), {"uni": 0, "bc": 0, "mc": 0, "vrrp": 0})
        while len(secs) > KEEP_S:
            secs.popitem(last=False)
        return s

    def sampler() -> None:
        prev = {}
        while True:
            st = station(a.iface)
            with lock:
                s = slot(time.time())
                s["inactive_ms"] = st.get("inactive_ms")
                s["signal"] = st.get("signal")
                for k in ("rx_pk", "tx_retry"):
                    s[k] = st[k] - prev[k] if k in st and k in prev else None
            prev = st
            time.sleep(1.0)

    threading.Thread(target=sampler, daemon=True).start()
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0003))
    sock.bind((a.iface, 0))
    f = open(a.out, "a", buffering=1)
    stamp = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%F %T.%f")[:-3]
    f.write(f"# start {stamp(time.time())} UTC on {a.iface}, gap > {a.gap}s\n")
    last: dict[str, float] = {}
    pending: list[tuple[float, str]] = []                   # (write-after time, header)
    while True:
        pkt, addr = sock.recvfrom(2048)
        now = time.time()
        kind = {0: "uni", 1: "bc", 2: "mc"}.get(addr[2])
        if kind is None:                                    # outgoing / other host
            continue
        with lock:
            s = slot(now)
            s[kind] += 1
            is_vrrp = addr[1] == 0x0800 and len(pkt) >= 20 and pkt[9] == VRRP
            if is_vrrp:
                s["vrrp"] += 1
        if is_vrrp:
            src = socket.inet_ntoa(pkt[12:16])
            prev, last[src] = last.get(src), now
            if prev is not None and now - prev > a.gap:
                pending.append((now + 5, f"{stamp(now)} gap {now - prev:.2f}s from {src} (silent from "
                                         f"{stamp(prev)[11:]})"))
        while pending and now >= pending[0][0]:
            _, head = pending.pop(0)
            with lock:
                rows = list(secs.items())[-30:]
            f.write(head + "\n   sec(UTC)  uni  bc  mc vrrp inactive_ms rx_pk tx_retry signal\n")
            for t, s in rows:
                f.write("   %s %4d %3d %3d %4d %11s %5s %8s %6s\n" % (
                    stamp(t)[11:19], s["uni"], s["bc"], s["mc"], s["vrrp"], s.get("inactive_ms"), s.get("rx_pk"),
                    s.get("tx_retry"), s.get("signal")))


if __name__ == "__main__":
    main()
