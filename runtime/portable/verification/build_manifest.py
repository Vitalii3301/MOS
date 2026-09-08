#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {"__pycache__", ".mos_sessions", "dist"}
EXCLUDED_NAMES = {"MANIFEST.json"}


def main() -> None:
    files = {}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.name in EXCLUDED_NAMES:
            continue
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_PARTS for part in relative.parts) or path.suffix == ".pyc":
            continue
        files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "schema": "mos-portable-manifest/v1",
        "runtime_version": "4.2.0",
        "file_count": len(files),
        "files": files,
    }
    (ROOT / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "created", "file_count": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
