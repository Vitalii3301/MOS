#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from mos.runtime import MOSRuntime
from mos.util import atomic_write_json


def main() -> None:
    state_root = ROOT / "state"
    snapshots = state_root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mos_r42_seed_") as tmp:
        runtime = MOSRuntime(Path(tmp) / "seed.sqlite3")
        try:
            runtime.set_goal("portable_cross_chat_continuity")
            snapshot_file = Path(tmp) / "snapshot.json"
            snapshot = runtime.relay.export(snapshot_file)
            verify = runtime.verify()
        finally:
            runtime.close()
    if verify["db_integrity"] != "ok" or not verify["event_chain"]:
        raise SystemExit("seed state verification failed")
    target = snapshots / f"{snapshot['snapshot_id']}.json"
    atomic_write_json(target, snapshot)
    head = {
        "schema": "mos-git-state-head/v1",
        "runtime_version": verify["runtime_version"],
        "schema_version": snapshot["payload"]["schema_version"],
        "revision": snapshot["payload"]["revision"],
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_path": f"snapshots/{snapshot['snapshot_id']}.json",
        "state_hash": snapshot["state_hash"],
        "parent_snapshot": None,
        "last_session_id": None,
        "last_cycle_id": None,
        "updated_at": snapshot["payload"]["created_at"],
    }
    atomic_write_json(state_root / "HEAD.json", head)
    print(json.dumps({"status": "created", "head": head, "verify": verify}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
