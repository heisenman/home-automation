# PWA dev ↔ prod synthesis worksheet

Working doc to reconcile the two PWA deployments into one agreed "best" version, then deploy
that version to **both**. We go through each item together; fill in **Decision** per item.

## Systems & how they're served

| System | URL | Serves | Source |
|---|---|---|---|
| **Dev** | `http://192.168.0.210:8123` (http) and `:8443` (https) | **v45** (my working-tree edits) | this repo checkout `server/web/` — edits go live on browser reload, no build/restart |
| **Prod** | `https://192.168.0.210` (:443 bridge → ha-2) | **v43** | ha-2's `server/web/` — air-gapped, deployed via scp |

## Research finding (authoritative, verified 2026-07-12)

Production is **byte-identical to `git HEAD` (00bae55) for every PWA file** — app.js, styles.css,
index.html, push.js, sw.js, manifest.webmanifest, icon.svg, vendor/preact-htm. **There is no
independent prod-only divergence / hand-edit to salvage.** The only differences between the two
live systems are the v44/v45 changes currently in the dev working tree (uncommitted).

So "reconcile in both directions" reduces to: **for each change dev made on top of the v43
baseline, decide keep / revert / modify.** Three of them *remove or restructure* something prod
still has (title, visible build, always-expanded actuator cards) — those are the genuine
"is dev actually better here?" calls. The rest are pure additions.

Files that differ: `app.js` (+99/−13 lines), `styles.css` (+4). All other files identical.

---

## Difference items (decide each)

### 1. Topbar — "Home Automation" title + visible build string
- **Prod (v43):** topbar shows `<h1>Home Automation</h1>` and a visible `<span class="build">v43…</span>`; status dot has no tooltip.
- **Dev (v45):** title removed, visible build removed; build id moved to the status-dot `title=` tooltip (still discoverable on hover).
- **Rationale:** you asked to de-clutter the topbar (remove title + version).
- **Recommendation:** keep dev (declutter). Build id stays reachable via dot tooltip.
- **Decision:** _(you already requested this — confirm to lock)_

### 2. Topbar — live clock  *(pure addition)*
- **Prod:** none.
- **Dev:** new `Clock` component — local wall-clock `H:MM AM/PM`, tabular-nums, ticks each second, placed in the right-side icon cluster.
- **Recommendation:** keep (bring to prod).
- **Decision:** _____

### 3. Actuators — full cards vs. tiles-unless-expanded  ⭐ the main "both directions" call
- **Prod:** every actuator always renders as a **full `DeviceCard`** — controls, decision history, override + manual controls, settings all visible at once.
- **Dev:** actuators render as compact **`ActuatorChip` tiles** in the sensor grid (name, room, RUN/IDLE pill, control reading, health, "auto off"). Click expands to the full `DeviceCard` (now with a close button).
- **Trade-off:** tiles = far less vertical clutter, consistent with sensors; but controls are one click away instead of always-on. This is the item most plausibly "not better in one direction."
- **Recommendation:** keep dev's tile model, but worth confirming you want controls behind a click. (Possible middle ground: default the *only* actuator to expanded, or remember expanded state.)
- **Decision:** _____

### 4. Expanded tile — explicit close (✕) button
- **Prod:** expanded **sensor** collapses only by clicking its header; actuators were never collapsible.
- **Dev:** explicit `✕` close button next to the `✎` edit button on both expanded **sensors** and expanded **actuators**; header title also still collapses (with a ▾ chevron).
- **Recommendation:** keep (bring to prod).
- **Decision:** _____

### 5. Room labels — word-wrap + font/spacing squeeze
- **Prod:** room name is a single-line SVG `<text>` at font-size 26 — long names overflow the room polygon.
- **Dev:** `wrapLabel()` greedy word-wraps a multi-word name into `<tspan>` lines that each fit the room's bbox width (≤92%), and squeezes any still-too-wide line with `textLength`/`lengthAdjust`. AQ badge + unplaced-device glance shift down (`labelDrop`) to clear the wrapped lines.
- **Recommendation:** keep (bring to prod).
- **Decision:** _____

### 6. CSS — supporting rules  *(pure addition, follows items 1/2/4)*
- **Dev adds:** `.clock`, `.clock-ampm`, `.close-btn`, and a comment on the edit+close cluster.
- **Recommendation:** keep (bring to prod, required by items 2 & 4).
- **Decision:** _____

---

## ⚠️ Runtime DATA differences (NOT PWA code) — discovered 2026-07-12

The PWA JS is ~identical between the two boxes. Several visible differences are **data**, driven by
the two boxes hearing different device sets / decode paths. Fixing these is ingest/firmware work,
**not** an app.js edit, and each is device-facing (needs discussion before acting).

### A. Battery indicators — prod LOST them (dev has them)
- **Symptom:** dev shows 🔋% on 11/12 sensor tiles; prod shows it on 1/21 (only `radon_crawlspace`).
- **Root cause:** the SwitchBot meters reach **prod (ha-2) via edge nodes**, whose forwarded metrics
  are `temperature_c, humidity_pct` only — **no `battery_pct`**. This dev box decodes the same meters
  with the local Python `switchbot.py`, which DOES emit battery (decoder line 99–115). Aranet radon
  keeps battery on both (its edge/decode path forwards it), which is why it's the lone survivor.
- **Verified:** ha-2 `readings` has only temp+hum for every `meter_*` (2 metrics); dev has 3.
- **Fix (TBD, device-facing):** edge firmware SwitchBot decode must forward battery → edge OTA; OR a
  local decode path on ha-2. Firmware/OTA effort, not a PWA change.

### B. Air-quality legend + band coloring — dev LACKS it (prod has it)
- **Symptom:** prod map shows the AQ band legend + colored room badges; dev (8123) shows none.
- **Root cause:** the gas fleet (SGP/BME) reports **air_quality to prod only**. Dev's `/api/v1/rooms`
  has `air_quality_legend: absent`, 0 rooms with `air_quality`; prod has the legend (5 bands) + 5 rooms.
  Dev's server HAS the unified-AQ code (same repo) — there's just no gas data to render, so the legend
  is (correctly) suppressed.
- **Fix (TBD):** get gas-node air_quality into dev's ingest (topology/broker subscription), not a PWA change.

**Framing:** "make both systems the same version" cleanly applies to the **PWA code** (trivial — deploy
the reconciled app.js/styles.css to both). The **data** differences above are separate infra tasks and
the two boxes are not meant to hear an identical fleet (prod = full house; dev = subset).

## Additional improvements to consider (not yet in either system)
- **Topbar → two rows** (requested 2026-07-12): user prefers **production's** top bar as the base, and
  wants a **second row** holding the °F units toggle **and everything to its right** (Notify, Admin/Lock/
  +Device). First row keeps dot + (title?) + (version?) + Scene + NightMode + (clock?). NOTE: this REVERSES
  the earlier "remove title + version" request — needs the title/version/clock decision (see open Qs).
  Do on prod first, then mirror to 8123.
-

---

## Deploy plan (once decisions locked)
1. Apply agreed synthesis to `server/web/` in the repo (revert/modify any item decided against dev).
2. Bump `BUILD`. Validate: `cp app.js x.mjs && node --check x.mjs`.
3. Dev is then live automatically on `:8123` / `:8443` (reload).
4. Deploy identical files to **ha-2** via scp (air-gap path; ha-api serves static, no restart needed for asset-only changes — confirm). Update the HA2-DEPLOY-PENDING ledger.
5. Verify prod `https://192.168.0.210/app/app.js` reports the new BUILD; both systems now match.
6. Commit + push + board post.
