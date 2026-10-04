"""PWA Levoit flashing (server/maintenance/levoit_flash.py, docs/design/pwa-levoit-flashing.md).

esptool is faked, so these pin the decisions, not the wire: the OEM backup gates the erase, an existing OEM
backup is reused rather than overwritten, our own edge nodes and the wrong chip are refused before anything is
written, a missing/mismatched image is reported, and the predicted name matches ESPHome's MAC suffix.
"""
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from server.maintenance import edge_flash as EF
from server.maintenance import levoit_flash as LF
from tests._harness import raises

MAC_BYTES = (0x12, 0x34, 0x56, 0xAB, 0xCD, 0xEF)   # fake — real MACs never enter git
OEM = (b"\x00" * 100 + b"VeSync cloud" + b"\x11" * 100).ljust(LF.FLASH_BYTES, b"\xff")


class FakeEsp:
    CHIP_NAME = "ESP32-C3"

    def __init__(self, log):
        self.log = log
        self._port = self

    def read_mac(self):
        return MAC_BYTES

    def change_baud(self, b):
        self.log.append(("baud", b))

    def close(self):
        self.log.append(("close",))


class FakeCmds:
    def __init__(self, *, chip="ESP32-C3", size="4MB", data=OEM, connect_ok=("no-reset",)):
        self.log, self.chip, self.size, self.data, self.connect_ok = [], chip, size, data, connect_ok

    def detect_chip(self, port, baud, connect_mode, connect_attempts):
        self.log.append(("connect", connect_mode))
        if connect_mode not in self.connect_ok:
            raise OSError("No serial data received.")
        e = FakeEsp(self.log)
        e.CHIP_NAME = self.chip
        return e

    def run_stub(self, esp):
        return esp

    def attach_flash(self, esp):
        pass

    def detect_flash_size(self, esp):
        return self.size

    def read_flash(self, esp, addr, size, output, no_progress=False):
        self.log.append(("read", addr, size))
        return self.data

    def write_flash(self, esp, addr_data, **kw):
        self.log.append(("write", addr_data[0][0], kw.get("erase_all")))

    def verify_flash(self, esp, addr_data):
        self.log.append(("verify",))

    def reset_chip(self, esp, mode):
        self.log.append(("reset", mode))


@contextmanager
def env(tmp_path, cmds, *, image=True, known=None):
    """Point the module at a tmp image + backup dir and the fake esptool; restore afterwards."""
    saved = (LF.IMAGE, LF.IMAGE_META, LF.BACKUP_DIR, LF._cmds, EF.known_node_for_mac,
             EF._PortLock.__init__.__defaults__)
    img_dir = tmp_path / "build"
    img_dir.mkdir()
    LF.IMAGE, LF.IMAGE_META, LF.BACKUP_DIR = img_dir / "firmware.factory.bin", img_dir / "meta.json", tmp_path / "bk"
    if image:
        LF.IMAGE.write_bytes(b"esphome-image")
        LF.IMAGE_META.write_text(json.dumps({"sha256": hashlib.sha256(b"esphome-image").hexdigest(),
                                             "built": "t", "esphome": "2026.6.5"}))
    LF._cmds = lambda: cmds
    EF.known_node_for_mac = lambda mac: known
    EF._PortLock.__init__.__defaults__ = (tmp_path / ".lock",)
    try:
        yield
    finally:
        (LF.IMAGE, LF.IMAGE_META, LF.BACKUP_DIR, LF._cmds, EF.known_node_for_mac,
         EF._PortLock.__init__.__defaults__) = saved


def test_predicted_name_is_esphome_mac_suffix():
    assert LF.predicted_name("12:34:56:AB:CD:EF") == "levoit-abcdef"


def test_full_flash_backs_up_before_erasing(tmp_path):
    f = FakeCmds()
    with env(tmp_path, f):
        r = LF.flash_levoit({"port": "/dev/ttyUSB0"})
        ops = [e[0] for e in f.log]
        assert ops.index("read") < ops.index("write"), "backup must precede the erase"
        assert ("write", 0, True) in f.log and ("verify",) in f.log
        assert r["name"] == "levoit-abcdef" and r["backup_markers"] == ["vesync"]
        bk = LF.BACKUP_DIR / r["backup"]
        assert bk.read_bytes() == OEM and "-oem-" in bk.name
        assert bk.with_suffix(".sha256").read_text().split()[0] == hashlib.sha256(OEM).hexdigest()
        assert (bk.stat().st_mode & 0o777) == 0o640


def test_existing_oem_backup_is_reused_not_overwritten(tmp_path):
    f = FakeCmds()
    with env(tmp_path, f):
        first = LF.flash_levoit({"port": "/dev/ttyUSB0"})["backup"]
        f.log.clear()
        f.data = b"esphome".ljust(LF.FLASH_BYTES, b"\x00")          # unit now runs ESPHome
        second = LF.flash_levoit({"port": "/dev/ttyUSB0"})
        assert second["backup"] == first
        assert not any(e[0] == "read" for e in f.log)


def test_blank_backup_read_stops_before_erase(tmp_path):
    f = FakeCmds(data=b"\xff" * LF.FLASH_BYTES)
    with env(tmp_path, f):
        with raises(LF.FlashError):
            LF.flash_levoit({"port": "/dev/ttyUSB0"})
        assert not any(e[0] == "write" for e in f.log)


def test_short_backup_read_stops_before_erase(tmp_path):
    f = FakeCmds(data=OEM[:1000])
    with env(tmp_path, f):
        with raises(LF.FlashError):
            LF.flash_levoit({"port": "/dev/ttyUSB0"})
        assert not any(e[0] == "write" for e in f.log)


def test_non_oem_content_saved_as_preflash(tmp_path):
    f = FakeCmds(data=b"esphome".ljust(LF.FLASH_BYTES, b"\x00"))
    with env(tmp_path, f):
        r = LF.flash_levoit({"port": "/dev/ttyUSB0"})
        assert "-preflash-" in r["backup"]
        assert LF.existing_backup("12:34:56:AB:CD:EF") is None    # never mistaken for the OEM image later


def test_our_edge_node_is_refused(tmp_path):
    f = FakeCmds()
    with env(tmp_path, f, known={"node_id": "c3_test", "manifest": "edge/esp32c3/nodes.yaml"}):
        with raises(LF.FlashError):
            LF.flash_levoit({"port": "/dev/ttyUSB0"})
        assert not any(e[0] in ("read", "write") for e in f.log)


def test_wrong_chip_or_flash_size_refused(tmp_path):
    for f in (FakeCmds(chip="ESP32-C6"), FakeCmds(size="8MB")):
        with env(_sub(tmp_path), f):
            with raises(LF.FlashError):
                LF.flash_levoit({"port": "/dev/ttyUSB0"})
            assert not any(e[0] in ("read", "write") for e in f.log)


_n = [0]


def _sub(tmp_path):
    _n[0] += 1
    p = tmp_path / f"s{_n[0]}"
    p.mkdir()
    return p


def test_falls_back_to_rts_reset_then_explains_download_mode(tmp_path):
    f = FakeCmds(connect_ok=("default-reset",))
    with env(tmp_path, f):
        LF.flash_levoit({"port": "/dev/ttyUSB0"})
        assert [e[1] for e in f.log if e[0] == "connect"] == ["no-reset", "default-reset"]
    g = FakeCmds(connect_ok=())
    with env(_sub(tmp_path), g):
        try:
            LF.flash_levoit({"port": "/dev/ttyUSB0"})
            raise AssertionError("expected FlashError")
        except LF.FlashError as e:
            assert "hold IO0" in str(e)


def test_missing_or_mismatched_image_refused_before_touching_board(tmp_path):
    f = FakeCmds()
    with env(tmp_path, f, image=False):
        assert LF.image_status()["ready"] is False
        with raises(LF.FlashError):
            LF.flash_levoit({"port": "/dev/ttyUSB0"})
        assert f.log == []
    with env(_sub(tmp_path), f):
        LF.IMAGE.write_bytes(b"something else")                       # rebuilt but not re-recorded
        st = LF.image_status()
        assert st["ready"] is False and "match" in st["reason"]


if __name__ == "__main__":
    from tests._harness import run_module
    run_module(globals())
