#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mos.relay import StateRelay
from mos.runtime import MOSRuntime
from mos.util import atomic_write_json


def main() -> None:
    manifest = json.loads((ROOT / "MANIFEST.json").read_text(encoding="utf-8"))
    mismatches = []
    for relative, expected in manifest["files"].items():
        path = ROOT / relative
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if actual != expected:
            mismatches.append({"path": relative, "expected": expected, "actual": actual})

    head = json.loads((ROOT / "state" / "HEAD.json").read_text(encoding="utf-8"))
    snapshot_path = ROOT / "state" / head["snapshot_path"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    StateRelay._validate_envelope(snapshot)
    if snapshot["snapshot_id"] != head["snapshot_id"] or snapshot["state_hash"] != head["state_hash"]:
        raise ValueError("seed HEAD does not match its snapshot")

    with tempfile.TemporaryDirectory(prefix="mos_portable_verify_") as tmp:
        temp = Path(tmp)
        local_snapshot = temp / "snapshot.json"
        atomic_write_json(local_snapshot, snapshot)
        runtime = MOSRuntime(temp / "runtime.sqlite3")
        try:
            runtime.relay.import_snapshot(local_snapshot, create_backup=False)
            verify = runtime.verify()
        finally:
            runtime.close()
    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_portable.py", "-v"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    ok = not mismatches and verify["db_integrity"] == "ok" and verify["event_chain"] and tests.returncode == 0
    receipt = {
        "schema": "mos-portable-verification/v1",
        "status": "verified" if ok else "failed",
        "manifest_file_count": manifest["file_count"],
        "manifest_mismatches": mismatches,
        "seed_snapshot_id": snapshot["snapshot_id"],
        "seed_state_hash": snapshot["state_hash"],
        "runtime_verify": verify,
        "portable_tests": {
            "returncode": tests.returncode,
            "summary": (tests.stderr or tests.stdout).strip().splitlines()[-1] if (tests.stderr or tests.stdout).strip() else "",
        },
    }
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    if not ok:
        if tests.returncode:
            print(tests.stdout, file=sys.stderr)
            print(tests.stderr, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
