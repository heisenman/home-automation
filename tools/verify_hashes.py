"""
Parquet hash manifest verifier (ADR-0004).

Verifies every Parquet partition against the SHA-256 manifest written by the
compactor. Run on a schedule (weekly systemd timer) and before any restore.

Exit codes: 0 = all OK, 1 = mismatch(es) found, 2 = manifest missing.

Usage:
  python3 tools/verify_hashes.py --parquet-dir instance/db/parquet
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(parquet_dir: Path) -> int:
    manifest_path = parquet_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest not found at {manifest_path}", file=sys.stderr)
        return 2

    with manifest_path.open() as f:
        manifest = json.load(f)

    files = manifest.get("files", {})
    if not files:
        print("WARNING: manifest is empty — no partitions recorded yet")
        return 0

    ok = 0
    fail = 0
    missing = 0

    for rel_path, meta in sorted(files.items()):
        full_path = parquet_dir / rel_path
        if not full_path.exists():
            print(f"MISSING  {rel_path}")
            missing += 1
            continue

        actual_sha = _sha256_file(full_path)
        expected_sha = meta.get("sha256", "")
        actual_size = full_path.stat().st_size
        expected_size = meta.get("size_bytes", 0)

        if actual_sha != expected_sha or actual_size != expected_size:
            print(
                f"MISMATCH {rel_path}\n"
                f"  sha256  expected={expected_sha[:16]}… got={actual_sha[:16]}…\n"
                f"  size    expected={expected_size}  got={actual_size}"
            )
            fail += 1
        else:
            print(f"OK       {rel_path}  ({meta.get('rows', '?')} rows)")
            ok += 1

    print(f"\nResult: {ok} OK  {fail} mismatch  {missing} missing  (of {len(files)} total)")
    return 0 if (fail == 0 and missing == 0) else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Verify Parquet hash manifest")
    p.add_argument("--parquet-dir", default="instance/db/parquet", type=Path)
    args = p.parse_args()
    sys.exit(verify(args.parquet_dir))


if __name__ == "__main__":
    main()
