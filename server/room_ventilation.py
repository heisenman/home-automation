"""server/room_ventilation.py — is a room closed off / accumulating? A humidity-based proxy (ADR-0035 amendment
2026-10-10, Phase 2b, SHADOW). Pure functions; the sampler (gas_quality_persist) supplies the data.

Why: the gas sensors are not occupancy sensors (Study 2 F12) — a sleeper adds CO₂ and water vapour, not much
VOC. Until there is CO₂ in the bedrooms, water vapour from the ordinary T/RH meters is the cleanest tracer we
have. Hugh (2026-10-10): "humidity readings should correlate alongside gas readings for door status indication."
Two features per room meter, each against the HOUSE (median of the other main-floor living areas):

  coupling  = correlation of the room's 15-min water-vapour changes with the house's, over the last 2 h. An open
              room moves with the house; a shut one drifts on its own.
  excess    = room − house water vapour (g/m³), minus the room's usual (28 d) difference — the build-up.

"Accumulating" = coupling < COUPLING_THR AND excess > EXCESS_THR. Shadow-tune (Aug 3–Oct 9, 00–07 h, ≥ 1 h
flagged): c_bed (Catherine, door SHUT nightly) 43/68 nights; h_office (Hugh, door OPEN) 19/68; h_bed (empty;
its meter sits in the closet) 15/68; kitchen 10/68. It discriminates but is NOT a door detector — it also
fires on any local moisture source (kitchen ~20 h cooking, an occupied office) and on a meter that is itself
enclosed. Hence the label "accumulating / closed off", not "door shut". Gas coupling is the planned second
leg once the SGP41s land (today's bedroom gas sensors — SGP30, BME680 — don't separate shut from open).
"""
from server.gas_compensation import absolute_humidity_gm3

STEP_S = 900                     # 15-min resolution for the change series
COUPLING_WINDOW_S = 2 * 3600
COUPLING_MIN_POINTS = 6          # ≥ 6 paired 15-min changes in the 2 h window
EXCESS_WINDOW_S = 28 * 86400
EXCESS_MIN_N = 72                # ≥ 72 paired hours before a room's usual offset counts
COUPLING_THR = 0.2
EXCESS_THR = 0.15                # g/m³
HOUSE_MIN_AREAS = 3
# Areas that are not a "room air" reference: wet rooms spike on showers, closets/mechanical are enclosed by
# design, attic/crawlspace aren't living space.
EXCLUDED_TYPES = {"bathroom", "closet", "mechanical", "monolithic", "porch"}


def ah_series(t_by_bucket: dict, rh_by_bucket: dict) -> dict:
    """{bucket: T} + {bucket: RH} → {bucket: water vapour g/m³} on the shared buckets."""
    return {b: absolute_humidity_gm3(t_by_bucket[b], rh_by_bucket[b]) for b in t_by_bucket if b in rh_by_bucket}


def median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def house_series(series_by_area: dict, exclude_area) -> dict:
    """Per bucket, the median over the OTHER areas (needs ≥ HOUSE_MIN_AREAS of them)."""
    buckets = sorted({b for a, s in series_by_area.items() if a != exclude_area for b in s})
    out = {}
    for b in buckets:
        vals = [s[b] for a, s in series_by_area.items() if a != exclude_area and b in s]
        if len(vals) >= HOUSE_MIN_AREAS:
            out[b] = median(vals)
    return out


def coupling(room: dict, house: dict):
    """Pearson r of consecutive-bucket changes, room vs house, over the buckets both have. None if too few."""
    common = sorted(set(room) & set(house))
    dr, dh = [], []
    for b0, b1 in zip(common, common[1:]):
        if b1 - b0 == STEP_S:
            dr.append(room[b1] - room[b0])
            dh.append(house[b1] - house[b0])
    if len(dr) < COUPLING_MIN_POINTS:
        return None
    n = len(dr)
    mr, mh = sum(dr) / n, sum(dh) / n
    sr = sum((x - mr) ** 2 for x in dr) ** 0.5
    sh = sum((y - mh) ** 2 for y in dh) ** 0.5
    if sr == 0 or sh == 0:
        return None
    return sum((x - mr) * (y - mh) for x, y in zip(dr, dh)) / (sr * sh)


def ventilation_state(coup, excess) -> dict:
    """The verdict + a one-line note for the explanation layer."""
    if coup is None or excess is None:
        return {"state": "unknown", "note": "Not enough humidity history yet."}
    if coup < COUPLING_THR and excess > EXCESS_THR:
        return {"state": "accumulating",
                "note": "Moisture is building up here and the room isn't mixing with the house — closed off or "
                        "occupied (or a local source like cooking). A CO₂ sensor would confirm."}
    return {"state": "mixed", "note": "Room air is mixing with the rest of the house."}
