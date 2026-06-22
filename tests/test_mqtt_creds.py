"""Tests for the shared MQTT credential helper (server/util/mqtt_creds.py).

This is the gate for the broker auth cutover: every server MQTT client must read $HA_MQTT_USER/PASS so
'dictator' creds can be supplied via systemd env BEFORE allow_anonymous is flipped off."""
import os

from server.util import mqtt_creds as M
from tests._harness import run_module


class _FakeClient:
    def __init__(self):
        self.creds = None

    def username_pw_set(self, user, pw=None):
        self.creds = (user, pw)


def _with_env(**env):
    """Set/clear env vars, returning a restore() to undo."""
    saved = {k: os.environ.get(k) for k in env}

    def apply(d):
        for k, v in d.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    apply(env)
    return lambda: apply(saved)


def test_no_creds_is_anonymous():
    restore = _with_env(HA_MQTT_USER=None, HA_MQTT_PASS=None)
    try:
        c = _FakeClient()
        assert M.apply_credentials(c) is False     # nothing applied → anonymous (today's broker)
        assert c.creds is None
        assert M.mqtt_credentials() == (None, None)
    finally:
        restore()


def test_user_and_pass_applied():
    restore = _with_env(HA_MQTT_USER="dictator", HA_MQTT_PASS="s3cret")
    try:
        c = _FakeClient()
        assert M.apply_credentials(c) is True
        assert c.creds == ("dictator", "s3cret")
    finally:
        restore()


def test_user_without_pass_still_applies():
    restore = _with_env(HA_MQTT_USER="dictator", HA_MQTT_PASS=None)
    try:
        c = _FakeClient()
        assert M.apply_credentials(c) is True
        assert c.creds == ("dictator", None)
    finally:
        restore()


def test_pass_without_user_is_ignored():
    # paho requires a username; a stray password alone must not half-configure auth
    restore = _with_env(HA_MQTT_USER=None, HA_MQTT_PASS="orphan")
    try:
        c = _FakeClient()
        assert M.apply_credentials(c) is False
        assert c.creds is None
    finally:
        restore()


if __name__ == "__main__":
    run_module(globals())
