# provisioning/ntp — the dictator as LAN time anchor

The dictator serves NTP so edge nodes get a wall clock without the internet — required once the LAN is
**air-gapped** behind the OpenWRT router. Concretely it unblocks the **E1001** panel, whose SNTP client
points at the dictator (`192.168.0.210`); it degrades gracefully until the dictator actually answers `:123`.

Debian's default `systemd-timesyncd` is **client-only** — it never answers `:123`. So we run **chrony** as
both client (disciplines to the internet pool while reachable, pre-air-gap) and server (`allow` the LAN),
with `local stratum 10` so it keeps serving as a fallback reference *inside the gap* when the pool is gone.

## Apply (GATED — installs a package; run with Hugh's OK)

```sh
sudo provisioning/ntp/install-ntp-serve.sh          # defaults to 192.168.0.0/24
```

Idempotent and reusable: run the same script on the **next dictator box** to formalize chrony there
(the standing plan — the current `.245` failover is being deprecated, so it is intentionally *not* set up
as an anchor). Files:

- `chrony-ha-serve.conf` — the drop-in installed to `/etc/chrony/conf.d/ha-serve.conf` (`allow` + `local`).
- `install-ntp-serve.sh` — installs chrony, disables `systemd-timesyncd`, applies the drop-in, verifies `:123`.

## Verify

```sh
ss -ulpn | grep :123            # chronyd listening
chronyc serverstats             # NTP packets received (rises as clients query)
chronyc tracking                # this host's own sync state
```

The OpenWRT router becomes the segment's NTP server once onboarded (`provisioning/openwrt/…`), with the
dictator as its holdover source; until then, edge nodes point straight at the dictator.
