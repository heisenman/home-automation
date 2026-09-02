# Air-gap router control — how to actually change something on it

Runbook for the OpenWRT air-gap router (`192.168.1.1`, R7800, hostname `ha-router-airgap`). Written
2026-09-02 after un-hiding `autohome_airgap`, because every step below cost an experiment and none of it
was obvious. Companion to `MIGRATION-DESIGN-LOG.md` (DJ-6, DJ-15) and `server/maintenance/router_reconcile.py`.

**Read §1 before you change anything.** It is the trap that will silently undo your work.

---

## 1. THE CONFIG-OF-RECORD IS PYTHON, NOT THE uci FILES

There are three places that look authoritative. Only one wins.

| Place | Reality |
|---|---|
| `provisioning/openwrt/etc/config/*` | **Documentation only** for the wireless keys. `router_reconcile.py` never reads it. Editing it changes nothing. |
| The router's own `uci` state | The live config, but it is **overwritten on a timer** — see below. |
| `invariants()` in `server/maintenance/router_reconcile.py` | **The real config-of-record.** A hardcoded Python list of `(uci key, expected value)`. |

`ha-router-reconcile.timer` on ha-2 runs **`router_reconcile.py apply`** — not `check` — every ~15 min. So:

> **Any `uci set` you make that disagrees with `invariants()` is reverted within 15 minutes.**

This bit us immediately: un-hiding the SSID on the router while `invariants()` still said `hidden == "1"`
meant the next tick would have re-hidden it and re-broken the fleet. And because the invariant *matched the
router* beforehand, `check` cheerfully reported **"no drift"** while the committed uci file said otherwise.

**Correct order for any wireless/network change:**

1. Edit `invariants()` in `server/maintenance/router_reconcile.py` (add a comment saying *why*).
2. Edit `provisioning/openwrt/etc/config/*` to match, so the documentation doesn't lie.
3. `scp` **both** to ha-2 and verify by checksum ([[airgap-checkout-drift]] — a commit is not a deploy).
4. `router_reconcile.py check` on ha-2 → should now report the drift you intend.
5. `router_reconcile.py apply`, **or** `uci set` by hand and let the timer converge.
6. Verify at the **runtime** layer (§4), not just `uci`.

## 2. Getting a shell command onto the router

The router is only reachable from ha-2, over the `id_cluster` back-channel. From .210 that is two hops:

```
.210  --(air-gap wifi wlp2s0)-->  ha-2 (192.168.1.210)  --(id_cluster)-->  router (192.168.1.1)
```

**Nested quoting through two `ssh` layers gets mangled** — this is how the first attempt silently did
nothing. Don't fight the quoting; **base64 the script**:

```bash
B64=$(printf 'uci set wireless.default_radio1.hidden=0\nuci commit wireless\n' | base64 -w0)
ssh -o BatchMode=yes visko@192.168.1.210 \
  "echo $B64 | base64 -d | ssh -i ~/.ssh/id_cluster -o BatchMode=yes \
   -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@192.168.1.1 sh"
```

This is robust for arbitrary multi-line scripts and needs no escaping thought.

## 3. `wifi reload` — two gotchas

**(a) busybox has NO `nohup`.** The first dispatch failed with exactly this and nothing else:

```
sh: nohup: not found
```

`uci commit` had succeeded, so `uci get` reported the new value while the radio kept the old one — the most
misleading possible half-state. **Background it with a subshell instead:**

```sh
( wifi reload ) >/tmp/reload.log 2>&1 </dev/null &
```

Redirect all three fds or the parent `ssh` hangs waiting on them.

**(b) `uci commit` does not touch the running radio.** You must `wifi reload` (regenerates
`/var/run/hostapd-*.conf` and restarts hostapd). Commit-without-reload is the state described above.

**Reloading wifi drops .210's own air-gap leg** — it rides this radio, so your ssh chain dies mid-command.
That's why it must be backgrounded on the router. The leg comes back in ~20-30 s, and
`ha-airgap-linkwatch` (ADR-0039) will force a reassociation if it doesn't.

## 4. Verifying — `uci` lies, check the runtime

Three layers, and only the last two prove anything:

```sh
uci get wireless.default_radio1.hidden          # intent (survives reboot)
grep -h ignore_broadcast_ssid /var/run/hostapd-*.conf   # RUNTIME truth: 0 = broadcasting
iwinfo                                          # radios actually up, with ESSID
```

**Scan-based verification has a trap.** From an *associated* client, `iw scan` prints the SSID for the AP
it is joined to **even when the beacon hides it** — the SSID is known from the association, not the beacon.
So this looks like success and isn't:

```
BSS 78:d2:94:b8:ef:59 -- associated
        SSID: autohome_airgap        <-- from OUR association, NOT the beacon
BSS 78:d2:94:b8:ef:59
        (no SSID line)               <-- the beacon. THIS is the one that matters.
```

Read the **non-associated** BSS entry, or just trust `ignore_broadcast_ssid`. Also `iw dev <if> scan flush`
first — cached scan results will happily show you the old world.

## 5. Radio numbering is INVERTED between the two files

| | radio0 | radio1 |
|---|---|---|
| Live router (`uci show wireless`) | **5 GHz**, ch 149, iface `default_radio0` / `phy0-ap0` | **2.4 GHz**, ch 1, iface `default_radio1` / `phy1-ap0` |
| `provisioning/openwrt/etc/config/wireless` | 2 GHz, section `wifinet0` | 5 GHz, section `wifinet1` |

Both the band assignment **and** the section names differ. The edge nodes (ESP32) are **2.4 GHz only**, so
on the live router the one that matters for the sensor fleet is **`default_radio1` / `phy1-ap0`**. Getting
this backwards means "fixing" the band nothing is on.

## 6. Worked example — un-hiding the SSID (2026-09-02)

Why: a brownout cold-booted the fleet; ESP32 does not reliably re-associate to a hidden SSID, so
`hbed_c6` and `s3-crawlspace` never came back and were dark **2 days**. Un-hiding brought `hbed_c6` back
**on its own, with no reflash**, within seconds of the beacon appearing. See ADR-0039 and
[[airgap-hidden-ssid-stranded-nodes]].

```bash
# 1. invariants(): hidden "1" -> "0" for BOTH default_radio0 and default_radio1
# 2. provisioning/openwrt/etc/config/wireless: hidden '1' -> '0' both wifi-ifaces
# 3. scp both to ha-2, verify checksums
# 4. set + commit (verify BEFORE disturbing the radio)
B64=$(printf 'uci set wireless.default_radio0.hidden=0\nuci set wireless.default_radio1.hidden=0\nuci commit wireless\nuci show wireless | grep hidden\n' | base64 -w0)
ssh visko@192.168.1.210 "echo $B64 | base64 -d | ssh -i ~/.ssh/id_cluster -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@192.168.1.1 sh"
# 5. reload, backgrounded (drops your path)
B64=$(printf '( wifi reload ) >/tmp/reload.log 2>&1 </dev/null &\necho dispatched\n' | base64 -w0)
ssh visko@192.168.1.210 "echo $B64 | base64 -d | ssh -i ~/.ssh/id_cluster -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@192.168.1.1 sh"
# 6. wait ~30s, then verify RUNTIME + that reconcile agrees
#    grep -h ignore_broadcast_ssid /var/run/hostapd-*.conf   -> 0, 0
#    venv/bin/python3 -m server.maintenance.router_reconcile check  -> no drift
```

**`autohome_airgap` is now BROADCAST on both bands and must stay that way** (Hugh, 2026-09-02: "keep the
SSIDs open, no need to tempt future fate"). Hiding an SSID provides no security — it is trivially
recoverable from any associated client's traffic — and on an already air-gapped WPA2-PSK network behind a
default-deny firewall it bought nothing while costing sensor uptime.

## 7. If you lose the router entirely

The router is reachable **only** over the air-gap wifi from .210 (via ha-2). If a radio change leaves it
unreachable there is no second path today. Mitigations, in order:

1. `ha-airgap-linkwatch` on .210 reassociates a live-but-dead leg automatically (ADR-0039).
2. ha-2 reaches the router independently of .210's wifi — try the hop from ha-2 directly.
3. **Offered but not yet wired:** .210 has a spare ethernet port that could be cabled to the air-gap
   router, giving a wired management path immune to radio changes. `net.ipv4.ip_forward=0` keeps the gap
   intact either way. Worth doing before the next radio change.
4. Physical: OpenWRT failsafe mode (power-cycle, hold reset during boot).

**Never make a radio change you cannot verify within one reconcile interval**, and never while nobody can
physically reach the router.
