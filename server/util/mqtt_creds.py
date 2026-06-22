"""Shared MQTT credential helper for the broker auth/ACL cutover (provisioning/broker-auth-cutover.md).

Every server service connects as the `dictator` identity (full pub/sub per server/config/acl). Creds
come from the environment so they can be set per-unit (systemd `Environment=`/`EnvironmentFile=`) without
touching code, and are *latent-safe*: on today's anonymous broker an unset/ignored credential is fine,
so this can be deployed BEFORE `allow_anonymous` is flipped (cutover steps 4→5).

Usage (call right after constructing the paho client, before connect()):

    from server.util.mqtt_creds import apply_credentials
    apply_credentials(client)
"""
from __future__ import annotations

import os


def mqtt_credentials() -> tuple[str | None, str | None]:
    """Return (username, password) from $HA_MQTT_USER / $HA_MQTT_PASS, or (None, None) if unset."""
    user = os.environ.get("HA_MQTT_USER") or None
    pw = os.environ.get("HA_MQTT_PASS") or None
    return user, pw


def apply_credentials(client) -> bool:
    """If $HA_MQTT_USER is set, apply it (and $HA_MQTT_PASS) to a paho client via username_pw_set.

    Returns True if credentials were applied, False if none were configured (anonymous). A password
    without a username is ignored (paho requires a username), matching broker behaviour.
    """
    user, pw = mqtt_credentials()
    if not user:
        return False
    client.username_pw_set(user, pw)
    return True
