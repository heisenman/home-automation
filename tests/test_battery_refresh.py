"""battery_refresh signs with the node's shared monotonic (ts, seq) — the firmware refuses a /cmd without ts."""
import hashlib
import hmac
import json

from server.mesh.battery_refresh import sweep


def test_batt_refresh_is_stamped_and_monotonic(tmp_path):
    out = []
    lut = {"n1": {"cmd_secret": "k1"}, "n2": {}}
    sent = sweep(lambda t, pl: out.append((t, pl)), lut, stagger_s=0, state_dir=tmp_path)
    sent += sweep(lambda t, pl: out.append((t, pl)), lut, stagger_s=0, state_dir=tmp_path)
    assert sent == ["n1", "n1"] and all(t == "home/edge/n1/cmd" for t, _ in out)
    inners = []
    for _, pl in out:
        env = json.loads(pl)
        assert hmac.new(b"k1", env["p"].encode(), hashlib.sha256).hexdigest() == env["s"]
        inners.append(json.loads(env["p"]))
    assert all(i["op"] == "batt_refresh" and isinstance(i["ts"], int) for i in inners)
    assert (inners[1]["ts"], inners[1]["seq"]) > (inners[0]["ts"], inners[0]["seq"])     # shared counter advances
    assert (tmp_path / ".n1_cmd_seq").exists()
