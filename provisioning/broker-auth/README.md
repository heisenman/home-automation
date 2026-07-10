# .210 dictator broker auth cutover — 2026-07-10 (LIVE)

The `.210` household/dev broker was flipped from anonymous to **authenticated + ACL'd** (broker-auth-posture
decision: anon-on-LAN until the air gap exists → now it does). No edge devices connect directly to `.210`
(they're on the air-gap VIP `.1.200`), so the only direct clients are local `ha-*` services + two cluster
bridges.

**Identities** (`/etc/mosquitto/passwd`, hashed, gitignored — NOT in repo):
- `dictator` — every local `ha-*` service (already sends it via `instance/mqtt.env` `HA_MQTT_USER/PASS`). ACL: full.
- `cluster245` — the two `ha/cluster/#` bridges into `.210`: the `.245` fileserver bridge AND the box-local
  standby broker (`~/ha-airgap-standby/failover/mosquitto/cluster-bridge.conf`). ACL: `ha/cluster/#` only.

**Live config:** `/etc/mosquitto/conf.d/homeauto.conf` gained `password_file /etc/mosquitto/passwd` +
`acl_file /etc/mosquitto/acl` and `allow_anonymous false` (all 3 listeners). ACL structure in
`dictator.acl.example`.

**Cutover method (safe):** built passwd+ACL → verified services send `u'dictator'` → added passwd/acl with
anon still ON (verified telemetry survives) → flipped `allow_anonymous false` under a systemd auto-revert net
→ cred'd both cluster bridges → verified 0 rejects, telemetry storing, anon rejected. Gotcha hit: the standby
broker's own cluster bridge was a credless client (fixed).

**To reproduce on a new dictator:** create passwd (`mosquitto_passwd -b`) with `dictator`=`mqtt.env` pass +
`cluster245`=(bridge pass), install this ACL, add `password_file`/`acl_file`/`allow_anonymous false` to the
listener conf, and set `remote_username`/`remote_password` on every bridge stanza pointing at it.
