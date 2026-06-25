"""
Rebuild the Parquet hash manifest from the partitions currently on disk (ADR-0004 / ADR-0018).

The compactor (server/storage/compactor.py) writes manifest.json only for the partitions IT compacts.
The parquet deep-reconcile (failover/reconcile-parquet.sh) rebuilds and *adds* partitions out-of-band
(a record-keeping-elevation seed pulls months of history from a peer), which leaves manifest.json stale:
changed partitions fail their SHA-256 check and newly-seeded ones are absent. ha-verify-hashes would then
report MISMATCH/MISSING (exit 1) on an archive that is in fact correct.

This tool re-derives the manifest from the actual files so it matches reality after a reconcile/seed. It is
byte-compatible with the compactor + verifier: same streamed SHA-256, same {sha256,size_bytes,rows} fields,
same top-level shape. It is the manifest half of the ADR-0018 provision-peer procedure (stage 3.5), and is
idempotent — re-running on an unchanged tree reproduces the same manifest (modulo timestamps).

Excludes the stray year=0/ partition a bad compaction can leave behind (same exclusion as reconcile-parquet).

Exit codes: 0 = manifest written (or --check found it already in sync); 1 = --check found drift; 2 = no
parquet dir / nothing to do.

Usage:
  python3 tools/rebuild_parquet_manifest.py --parquet-dir instance/db/parquet
  python3 tools/rebuild_parquet_manifest.py --parquet-dir instance/db/parquet --dry-run
  python3 tools/rebuild_parquet_manifest.py --parquet-dir instance/db/parquet --check
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path


def _sha256_file(path: Path) -> str:
    # identical to compactor._sha256_file / verify_hashes._sha256_file (streamed 1 MiB chunks)
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_count(path: Path) -> int:
    import duckdb
    return duckdb.connect().execute(
        "SELECT COUNT(*) FROM read_parquet([?])", [str(path)]
    ).fetchone()[0]


def _partitions(parquet_dir: Path) -> list[Path]:
    # all *.parquet under the root, excluding the stray year=0 partition (matches reconcile-parquet.sh)
    return sorted(
        p for p in parquet_dir.rglob("*.parquet")
        if "year=0" not in p.relative_to(parquet_dir).parts[0:1] and not str(p.relative_to(parquet_dir)).startswith("year=0/")
    )


def build_manifest(parquet_dir: Path, prior: dict) -> dict:
    """Re-derive {files:{rel:{sha256,size_bytes,rows,updated_ts}}, updated_ts}. Preserve a file's prior
    updated_ts when its sha256 is unchanged, so a no-op rebuild does not churn per-file timestamps."""
    prior_files = prior.get("files", {}) if prior else {}
    files: dict = {}
    for p in _partitions(parquet_dir):
        rel = str(p.relative_to(parquet_dir))
        sha = _sha256_file(p)
        prev = prior_files.get(rel, {})
        files[rel] = {
            "sha256": sha,
            "size_bytes": p.stat().st_size,
            "rows": _row_count(p),
            # keep the original stamp if the bytes did not change; else stamp now
            "updated_ts": prev.get("updated_ts") if prev.get("sha256") == sha else _utc_now_iso(),
        }
    return {"files": files, "updated_ts": _utc_now_iso()}


def _load(manifest_path: Path) -> dict:
    if manifest_path.exists():
        with manifest_path.open() as f:
            return json.load(f)
    return {}


def _diff(old: dict, new: dict) -> list[str]:
    of, nf = old.get("files", {}), new.get("files", {})
    msgs = []
    for rel in sorted(set(of) | set(nf)):
        o, n = of.get(rel), nf.get(rel)
        if o is None:
            msgs.append(f"  + ADD     {rel}  ({n['rows']} rows)")
        elif n is None:
            msgs.append(f"  - DROP    {rel}  (was in manifest, not on disk)")
        elif o.get("sha256") != n.get("sha256") or o.get("size_bytes") != n.get("size_bytes"):
            msgs.append(f"  ~ CHANGE  {rel}  sha {str(o.get('sha256'))[:12]}…->{n['sha256'][:12]}… "
                        f"size {o.get('size_bytes')}->{n['size_bytes']} rows {o.get('rows')}->{n['rows']}")
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the Parquet hash manifest from on-disk partitions")
    ap.add_argument("--parquet-dir", default="instance/db/parquet", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="show the diff; do not write")
    ap.add_argument("--check", action="store_true", help="exit 1 if the manifest is out of sync (no write)")
    args = ap.parse_args()

    pdir: Path = args.parquet_dir
    if not pdir.is_dir():
        print(f"ERROR: no parquet dir at {pdir}", file=sys.stderr)
        return 2

    prior = _load(pdir / "manifest.json")
    new = build_manifest(pdir, prior)
    if not new["files"]:
        print(f"no partitions under {pdir} — nothing to do")
        return 2

    diff = _diff(prior, new)
    if args.check:
        if diff:
            print(f"manifest OUT OF SYNC ({len(diff)} change(s)):")
            print("\n".join(diff))
            return 1
        print(f"manifest in sync ({len(new['files'])} partition(s))")
        return 0

    print(f"{len(new['files'])} partition(s) on disk; {len(diff) or 'no'} change(s) vs current manifest")
    if diff:
        print("\n".join(diff))
    if args.dry_run:
        print("(dry-run — manifest not written)")
        return 0

    # atomic write (tmp + os.replace within the same dir)
    mpath = pdir / "manifest.json"
    tmp = mpath.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(new, f, indent=2)
    os.replace(tmp, mpath)
    print(f"wrote {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
