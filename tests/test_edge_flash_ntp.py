"""edge_flash profile → NTP (2026-10-12): an air-gap profile must carry a reachable time server, or the flashed
node can never set its clock and rejects every signed command, OTA included."""
from server.maintenance import edge_flash as EF

CFG = {"ntp_server": "pool.ntp.org", "profiles": {
    "prod": {"wifi_ssid": "air", "wifi_psk": "x", "broker_uri": "mqtt://192.168.1.200:1883",
             "ota_host": "192.168.1.210", "ntp_server": "192.168.1.200"},
    "dev": {"wifi_ssid": "home", "wifi_psk": "y", "broker_uri": "mqtt://192.168.0.210:1883", "ota_host": "192.168.0.210"}}}


def test_profile_ntp_wins_over_global(monkeypatch):
    monkeypatch.setattr(EF, "_load_profiles", lambda: CFG)
    assert EF.apply_defaults({"profile": "prod"})["ntp_server"] == "192.168.1.200"
    assert EF.apply_defaults({"profile": "dev"})["ntp_server"] == "pool.ntp.org"        # no profile value → global
    assert EF.apply_defaults({"profile": "prod", "ntp_server": "10.0.0.1"})["ntp_server"] == "10.0.0.1"


def test_survey_reports_each_profiles_ntp(monkeypatch):
    monkeypatch.setattr(EF, "_load_profiles", lambda: CFG)
    got = {p["id"]: p["ntp_server"] for p in EF.provision_defaults()["profiles"]}
    assert got == {"prod": "192.168.1.200", "dev": "pool.ntp.org"}
