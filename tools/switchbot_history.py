"""
SwitchBot Meter Pro — BLE on-device history fetcher (reverse-engineered).

Pulls a meter's stored log (Meter Pro: ~68 days) directly over BLE — no cloud, no app.
Protocol is undocumented; see docs/switchbot-ble-history-protocol.md for the RE notes.

Idempotent: inserts via INSERT OR IGNORE on the readings table's
UNIQUE(device_id, ts, metric) index. Re-pulling the full window only lands new rows
(insert/merge, never duplicate-append).

Modes:
  # Decode a captured btsnoop and print samples — NO radio, NO DB (verifies the decoder):
  python3 tools/switchbot_history.py --offline instance/research/<file>.btsnoop

  # Live pull from a device, decode, and INSERT OR IGNORE into the DB:
  python3 tools/switchbot_history.py --device AA:BB:CC:00:00:03 \
      --db instance/db/hot.db --device-id meter_pro_master_bed --area master_bedroom

Status: Meter Pro value-decode CONFIRMED against a real capture. Live fetch + per-sample
timestamp anchoring need on-hardware iteration (a BT dongle). Outdoor Meter (Format B)
history is a DIFFERENT protocol — not yet captured; `decode_outdoor` is a stub.
"""

import argparse
import logging
import sqlite3
import struct
import sys
from pathlib import Path

log = logging.getLogger("ha.sbhistory")

# SwitchBot custom GATT (control/notify) — see protocol doc
SVC_UUID = "cba20d00-224d-11e6-9fb8-0002a5d5c51b"
CMD_CHAR = "cba20002-224d-11e6-9fb8-0002a5d5c51b"   # write commands (handle 0x0013 in capture)
NOTIFY_CHAR = "cba20003-224d-11e6-9fb8-0002a5d5c51b"  # notifications (history stream)

# btsnoop epoch (0000-01-01) → unix: subtract this many microseconds
_BTSNOOP_UNIX_OFFSET_US = 0x00dcddb30f2f8000


# ── Record decode (Meter Pro) ──────────────────────────────────────────────────

def decode_meter_pro(notifications: list[bytes]) -> list[tuple[float, int]]:
    """
    Decode Meter Pro history notifications into (temperature_c, humidity_pct) samples.

    Data notifications are 16 bytes: 0x01 status + 15 data bytes = three 5-byte groups,
    each `[t1, h1, frac, t2, h2]` packing TWO samples that share a fraction byte:
      sample A: temp = (t1 & 0x7f) + (frac >> 4)*0.1 , hum = h1 & 0x7f
      sample B: temp = (t2 & 0x7f) + (frac & 0x0f)*0.1, hum = h2 & 0x7f
    Temperature sign bit is t & 0x80 (set = positive); we treat unset/low bytes as padding.
    """
    samples: list[tuple[float, int]] = []
    for v in notifications:
        if len(v) != 16 or v[0] != 0x01:
            continue  # skip metadata (len 15) / acks (len 1)
        d = v[1:]
        for i in range(0, 15, 5):
            t1, h1, frac, t2, h2 = d[i:i + 5]
            for tb, hb, fn in ((t1, h1, frac >> 4), (t2, h2, frac & 0x0f)):
                if not (tb & 0x80):
                    continue  # padding / not a valid sample
                temp = round((tb & 0x7f) + fn * 0.1, 1)
                hum = hb & 0x7f
                if -40.0 <= temp <= 60.0 and 0 <= hum <= 100:
                    samples.append((temp, hum))
    return samples


def decode_outdoor(notifications: list[bytes]) -> list[tuple[float, int]]:
    """Outdoor Meter (Format B) history — DIFFERENT protocol, not yet reverse-engineered.
    Need an Outdoor Meter btsnoop capture to implement. See protocol doc."""
    raise NotImplementedError(
        "Outdoor Meter history protocol not captured yet — grab a btsnoop of the app "
        "pulling an Outdoor Meter's history, then we build/verify decode_outdoor()."
    )


# ── btsnoop parsing (for --offline verification without a radio) ────────────────

def _parse_btsnoop_notifications(path: Path) -> list[bytes]:
    """Extract ATT Handle-Value-Notification payloads from a btsnoop_hci.log."""
    data = path.read_bytes()
    if data[:8] != b"btsnoop\x00":
        raise ValueError("not a btsnoop file")
    off = 16
    recs = []
    while off + 24 <= len(data):
        _o, ilen, _f, _d, _ts = struct.unpack(">IIIIq", data[off:off + 24])
        off += 24
        recs.append(data[off:off + ilen])
        off += ilen

    bufs: dict[int, list] = {}
    notifs: list[bytes] = []
    for p in recs:
        if not p or p[0] != 0x02:  # ACL only
            continue
        h, tl = struct.unpack("<HH", p[1:5])
        pb = (h >> 12) & 0x3
        conn = h & 0x0fff
        payload = p[5:5 + tl]
        if pb in (0, 2):  # L2CAP start
            if len(payload) >= 4:
                l2len, cid = struct.unpack("<HH", payload[:4])
                bufs[conn] = [cid, l2len, payload[4:]]
        elif pb == 1 and conn in bufs:  # continuation
            bufs[conn][2] += payload
        b = bufs.get(conn)
        if b and len(b[2]) >= b[1]:
            cid, l2len, dd = b
            frame = dd[:l2len]
            del bufs[conn]
            if cid == 0x0004 and frame and frame[0] == 0x1b:  # ATT notification
                notifs.append(frame[3:])
    return notifs


# ── DB insert (idempotent) ──────────────────────────────────────────────────────

def insert_samples(db: Path, device_id: str, device_type: str, area: str,
                   samples: list[tuple[int, float, int]]) -> int:
    """samples = [(ts_unix, temp_c, hum_pct), ...]. Returns rows actually inserted (new)."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        before = conn.total_changes
        from datetime import datetime, timezone
        rows = []
        for ts_unix, temp, hum in samples:
            iso = datetime.fromtimestamp(ts_unix, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append((iso, device_id, device_type, area, "ble-history", "temperature_c", float(temp), "degC", 1))
            rows.append((iso, device_id, device_type, area, "ble-history", "humidity_pct", float(hum), "%", 1))
        conn.executemany(
            "INSERT OR IGNORE INTO readings (ts, device_id, device_type, area, transport, "
            "metric, value, unit, schema_v) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


# ── Live fetch (needs a BT radio) ───────────────────────────────────────────────

import struct as _struct
import time as _time

# Command sequence, byte-for-byte from the btsnoop capture (see protocol doc).
_HANDSHAKE_PREFIX = bytes.fromhex("570005030400000000")   # + current unix time (BE u32)
_SETUP_CMDS = [
    bytes.fromhex("570f68050401030802000b0102000e10"),
    bytes.fromhex("570f690801"),
    bytes.fromhex("570f69080202"),
    bytes.fromhex("570f69080201"),
]
_READ_PREFIX = bytes.fromhex("570f690803020000")          # + addr (BE u16) + 0x06
_REC_STRIDE = 6                                           # read address increments by 6


def _parse_metadata(notifs: list[bytes]):
    """From setup-phase notifications, return (base_ts, newest_ptr, oldest_ptr).
    Metadata notif (len 15): 01 69 .. .. .. <ts:4 BE> 00 00 <ptr:2 BE> 00 78"""
    ptrs = []
    for v in notifs:
        if len(v) == 15 and v[1] == 0x69:
            ts = _struct.unpack(">I", v[5:9])[0]
            ptr = _struct.unpack(">H", v[11:13])[0]
            ptrs.append((ts, ptr))
    if not ptrs:
        return None, None, None
    newest = max(ptrs, key=lambda p: p[1])
    oldest = min(ptrs, key=lambda p: p[1])
    return newest[0], newest[1], oldest[1]


async def fetch_live(mac: str, window_records: int | None = None, settle: float = 1.5):
    """Connect, run the handshake/setup, discover the live buffer pointers, then page the
    history backward from the newest pointer. Returns (notifications, meta) where meta has
    base_ts / newest_ptr / oldest_ptr. window_records=None reads the whole stored range.

    Connects by address (no scan) so it won't disturb a running scanner's discovery."""
    import asyncio
    from bleak import BleakClient
    notifs: list[bytes] = []

    def on_notify(_char, data: bytearray):
        notifs.append(bytes(data))

    client = BleakClient(mac, timeout=25.0)
    await client.connect()
    try:
        await client.start_notify(NOTIFY_CHAR, on_notify)
        now = int(_time.time())
        await client.write_gatt_char(CMD_CHAR, _HANDSHAKE_PREFIX + _struct.pack(">I", now), response=True)
        for cmd in _SETUP_CMDS:
            await client.write_gatt_char(CMD_CHAR, cmd, response=True)
            await asyncio.sleep(0.2)
        await asyncio.sleep(settle)

        base_ts, newest, oldest = _parse_metadata(notifs)
        meta = {"base_ts": base_ts, "newest_ptr": newest, "oldest_ptr": oldest}
        if newest is None:
            log.warning("no metadata pointer parsed; returning setup notifications only")
            return notifs, meta

        start = oldest if window_records is None else max(oldest or 0, newest - window_records * _REC_STRIDE)
        addr = start
        while addr < newest:
            await client.write_gatt_char(CMD_CHAR, _READ_PREFIX + _struct.pack(">H", addr) + b"\x06", response=True)
            addr += _REC_STRIDE
            await asyncio.sleep(0.03)
        await asyncio.sleep(settle)
        return notifs, meta
    finally:
        try:
            await client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass
        await client.disconnect()


# ── Entry point ─────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="SwitchBot Meter Pro BLE history fetcher")
    ap.add_argument("--offline", type=Path, help="decode a saved btsnoop file (no radio/DB)")
    ap.add_argument("--device", help="MAC to pull from (live)")
    ap.add_argument("--device-id", help="device_id for DB rows")
    ap.add_argument("--area", default="unknown")
    ap.add_argument("--device-type", default="switchbot_meter_pro")
    ap.add_argument("--db", type=Path, help="hot.db for INSERT OR IGNORE")
    ap.add_argument("--dry-run", action="store_true", help="decode but don't insert")
    ap.add_argument("--window", type=int, default=0,
                    help="live: records to read back from newest (0 = whole stored range)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")

    if args.offline:
        notifs = _parse_btsnoop_notifications(args.offline)
        samples = decode_meter_pro(notifs)
        log.info("notifications=%d  decoded samples=%d", len(notifs), len(samples))
        temps = [s[0] for s in samples]
        if temps:
            log.info("temp %.1f–%.1f°C  first 20: %s", min(temps), max(temps), samples[:20])
        return

    if args.device:
        import asyncio
        window = args.window if args.window > 0 else None
        notifs, meta = asyncio.run(fetch_live(args.device, window_records=window))
        samples = decode_meter_pro(notifs)
        log.info("live notifications=%d  decoded samples=%d  meta=%s", len(notifs), len(samples), meta)
        if samples:
            temps = [s[0] for s in samples]
            log.info("temp %.1f–%.1f°C  newest 12: %s", min(temps), max(temps), samples[-12:])
        # Timestamp anchoring (newest sample == current live reading) is NOT verified yet, so
        # we never insert from a live pull until that's confirmed — wrong timestamps would
        # corrupt the series. --dry-run is the only supported live mode for now.
        if not args.dry_run and args.db:
            log.warning("refusing to insert: per-sample timestamps not yet anchored on hardware. "
                        "Use --dry-run until anchoring is verified.")
        return

    ap.error("need --offline FILE or --device MAC")


if __name__ == "__main__":
    main()
