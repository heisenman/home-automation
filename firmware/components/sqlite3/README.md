# `sqlite3` — vendored SQLite + a minimal FATFS VFS for ESP32 (ADR-0020 / ADR-0022)

Vendored **SQLite amalgamation 3.45.1** (public domain) plus a small SD-card VFS, so a node
can read/write a compact SQLite database on its FAT-formatted microSD. Registry-free
(air-gap clean), mirroring the repo's other vendored drivers.

This is the storage substrate for **ADR-0022**: the server keeps the raw archive as parquet,
but the **rollup rungs are compact SQLite** — readable on the server, the standby, *and* the
ESP32-P4 panel. `ha_replica` (panel-side) replicates the rung DB to SD and graphs from it
fully offline.

## What's here
- `sqlite3.c` / `include/sqlite3.h` / `include/sqlite3ext.h` — the unmodified amalgamation.
- `esp_fatfs_vfs.c` — a minimal `sqlite3_vfs` (`"esp-fatfs"`, registered as default) over the
  ESP-IDF FATFS POSIX layer. **No-op locking** — valid only because the DB file is never
  accessed concurrently (server writes whole-file replicas; panel only reads; a loader writes
  once, exclusively). **Do not reuse for any multi-writer workload.**
- `sqlite_assert_shim.h` — force-included ahead of `sqlite3.c` (see gotcha #1).
- `CMakeLists.txt` — build config (`SQLITE_OS_OTHER=1`, `THREADSAFE=0`, `OMIT_WAL`, temp in RAM…).

## REQUIRES
`fatfs vfs esp_hw_support freertos`. Caller must serialize all access (single task).

## Two non-obvious gotchas (both cost real debugging — read before touching this)

**1. IDF's `assert` evaluates its argument even under `NDEBUG`.**
This project sets `CONFIG_COMPILER_ASSERT_NDEBUG_EVALUATE=y`, so IDF's `assert.h` defines
`assert(e)` as `((void)(e))` — it still *compiles the expression*. SQLite's release build
assumes `NDEBUG` makes `assert()` vanish; many of its asserts reference `SQLITE_DEBUG`-only
symbols, so an evaluating assert fails to compile (`'bInverse' has no member`, etc.). Fix:
`sqlite_assert_shim.h` is `-include`d before `sqlite3.c` — it pulls IDF's `#pragma once`
`assert.h` (firing its guard) then redefines `assert` to a true no-op, so SQLite's own later
`#include <assert.h>` is skipped and our definition wins. Component-local; no global config change.

**2. FatFs `f_lseek()` beyond EOF EXTENDS the file (POSIX does not).**
Seeking past end-of-file on a *writable* FatFs handle grows the file with zeros — unlike POSIX,
where only a write extends. SQLite's pager `xRead`s past EOF while probing a new/empty database;
a naive `lseek+read` therefore silently grows a 0-byte file to e.g. 24 zero bytes, and SQLite
then reads that all-zero "header" and returns **`SQLITE_NOTADB` ("file is not a database")**.
Fix: `espRead` checks the file size first and zero-fills / short-reads anything at/after EOF
*without seeking there*. (`espWrite`'s extend-on-seek is left intact — it's correct for SQLite's
occasional sparse page writes.)

## Validated on the reTerminal D1001 (ESP32-P4, SDMMC 4-bit, 2026-07-02)
Rung schema `(res, device_id, metric, bucket_start INTEGER, vmin, vmax, vmean, vcount, vlast)`,
`PRIMARY KEY(...) WITHOUT ROWID` (row lives in the PK btree → range query is a covering scan):

| Measurement | Result |
|---|---|
| Fixture | 518,400 rows (90 d × 4 series × 1 min) |
| Bulk insert | 124 s @ ~4,180 rows/s (`journal=OFF, synchronous=OFF`) |
| On-disk | 36.9 MB → **74 bytes/row** |
| **Indexed range query, 1001 rows out of 518k** | **64 ms cold / 8.9 ms warm** |
| Reopen persisted DB cold + query (the panel graph path) | 67 ms cold / 9.6 ms warm |

Takeaway: query latency is decoupled from table size, so a resolution-selected ≤1000-point
graph query (ADR-0022) stays interactive at any history depth. Use **INTEGER epoch timestamps**
(not TEXT) and **WITHOUT ROWID** for the rung tables — that 74 B/row is a big improvement over a
TEXT-timestamp table and is the shape `rollup-ladder-server` should emit.
