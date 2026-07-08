# ADR-0029 — Paginated panel data transfer (chunked tile fetch)

**Date:** 2026-07-08
**Status:** **Accepted** (Hugh, 2026-07-08). Dev2-authored after the E1001 wall panel silently blanked
("press Green to fetch") — see `e1001-fetch-buffer-bug`. Extends **[[ADR-0019]]** (BFF as the single
UI-truth source; PWA + panels render the server spec).

## Context — the firehose doesn't fit, and "make the buffer bigger" is a treadmill

Panels fetch `GET /api/v1/sensors` — the **whole fleet** in one JSON body — and paginate client-side
(the E1001 shows 6 tiles/page, caps at 40). ESPHome's `http_request` must buffer the entire body up to
`max_response_buffer_size`; when `content-length` exceeds it, the body is **refused (0 bytes)**, not
truncated. So when the fleet grew 16 → 21 sensors this session (BME680 fleet + gas nodes +
`air_quality`/`voc_index`, ~10 KB → 14.3 KB), it crossed the E1001's deployed buffer and the display went
blank — HTTP 200, 0 bytes, 0 tiles. Small fetches (alerts, house scene) were unaffected.

The tempting fix — raise `max_response_buffer_size` (256 KB fits comfortably in the S3's 8 MB PSRAM) —
only **moves the ceiling**. Home fleets don't stop at 21: curtain actuators, locks, valves, per-outlet
meters push toward the hundreds. A buffer sized to "current fleet + slack" is coupled to fleet size, which
is the actual defect. It's also wasteful: pulling a growing dump over WiFi to a slow ePaper node to render
6 tiles.

## Decision — the server chunks; each request is one bounded page

Because the server and panel firmware are a **bespoke pair**, we change the transfer contract instead of
chasing the buffer. Data moves **one page at a time**; the panel fetches only the page it's showing.

**Server — a bounded panel endpoint:**
```
GET /api/v1/panel/tiles?page=N&per=M
→ { "page": N, "pages": P, "per": M, "total": T, "sensors": [ …≤M renderable sensors… ] }
```
- **`sensors[]` is the SAME per-sensor shape** as `/api/v1/sensors` (`room`/`device_id`, `metrics`,
  self-contained `graphs`), just sliced — so the panel's existing parse is unchanged; only its fetch loop
  changes.
- **Renderable-only:** a sensor is included iff it has ≥1 graphable metric present (`graphs` non-empty),
  matching the panel's own skip rule, so `pages`/`total` equal what the panel actually shows (no blank
  tiles).
- **Stable order:** `build_sensor_list` already sorts by (room, device_id), so pages stay coherent across
  fetches without a snapshot token. (A change mid-browse at worst shifts a tile by one page on the next
  refresh — acceptable for an occasionally-refreshing ePaper.)
- **`per` clamped** server-side (1..40): a panel page is small; the cap is the memory guarantee.

**Response size is O(per_page) — constant in fleet size.** A page is ~1–2 KB whether the fleet is 20 or
500 devices, so the panel's response buffer can be a small **fixed** value (~8 KB) forever.

**Panel firmware:** on wake / green / page-nav, fetch `page=<current>&per=<page size>`; render the ≤M
sensors; take `maxp` from `pages`. Page navigation now costs a ~500 ms fetch — negligible beside the
ePaper's own ~2 s refresh.

## Rejected alternatives
- **Bigger buffer (256 KB–1 MB from PSRAM).** Works today, fails later; couples buffer to fleet size;
  wastes WiFi/latency. Kept only as a *guardrail default* so a future overrun degrades to slow, not blank.
- **HTTP chunked transfer-encoding / streaming SAX parse.** Would avoid buffering, but ESPHome
  `http_request` captures the whole body — no incremental-parse hook — so it doesn't help the panel. More
  moving parts for no win here.
- **Server push (MQTT tile stream).** Panels are pull/wake-driven (battery + ePaper); a pull page-fetch
  fits the existing model with no broker-side state.

## Consequences
- Panels no longer outgrow their buffer as the fleet scales — the failure class is designed out.
- One reusable endpoint for **every panel**: E1001 adopts first (it's broken); D1001 migrates when its
  map/grid outgrows the full fetch (works today, no rush).
- Slightly more requests (one per page view), each tiny; panels refresh infrequently, so net traffic drops.
- Pagination consistency is eventual, not snapshot-isolated — fine for a glanceable panel; revisit with an
  `epoch` token only if a real coherence complaint appears.

Follow-ups: `e1001-fetch-buffer-bug` (server endpoint + firmware rework + reflash). D1001 adoption tracked
separately when needed.
