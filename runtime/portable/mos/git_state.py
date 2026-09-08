from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .relay import StateRelay
from .util import atomic_write_json, utcnow


STATE_ROOT = Path("runtime/portable/state")
HEAD_PATH = STATE_ROOT / "HEAD.json"


def _safe_ref_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-")
    return cleaned[:80] or fallback


class GitCommandError(RuntimeError):
    pass


class GitStateRepository:
    """Git-backed state transport that uses the Git protocol, not GitHub's LLM API.

    ``mos-state`` is an ordinary branch.  A fast-forward push is the optimistic
    lock.  When another chat publishes first, this client never overwrites it;
    the losing candidate is preserved on a conflict branch for later reduction.
    """

    def __init__(self, repo_root: str | Path, *, remote: str = "origin", branch: str = "mos-state"):
        self.repo_root = Path(repo_root).resolve()
        self.remote = remote
        self.branch = branch
        if not (self.repo_root / ".git").exists():
            raise ValueError(f"not a Git checkout: {self.repo_root}")

    @staticmethod
    def _git_env() -> dict[str, str]:
        env = os.environ.copy()
        token = env.get("MOS_GITHUB_TOKEN")
        if token:
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        return env

    @classmethod
    def clone(cls, url: str, destination: str | Path, *, branch: str = "main") -> Path:
        destination = Path(destination).resolve()
        if destination.exists() and any(destination.iterdir()):
            if not (destination / ".git").exists():
                raise ValueError(f"destination is not empty: {destination}")
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--branch", branch, url, str(destination)],
            text=True, capture_output=True, env=cls._git_env(), check=False,
        )
        if proc.returncode:
            raise GitCommandError(f"git clone failed: {proc.stderr.strip()}")
        return destination

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or self.repo_root, env=self._git_env(),
            text=True, capture_output=True, check=False,
        )
        if check and proc.returncode:
            raise GitCommandError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    @property
    def remote_ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.branch}"

    def fetch(self) -> str:
        spec = f"+refs/heads/{self.branch}:{self.remote_ref}"
        self._git("fetch", "--no-tags", self.remote, spec)
        return self._git("rev-parse", self.remote_ref).stdout.strip()

    def _show_json(self, commit: str, path: Path) -> dict[str, Any]:
        raw = self._git("show", f"{commit}:{path.as_posix()}").stdout
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"Git state file is not an object: {path}")
        return value

    def load(self) -> dict[str, Any]:
        commit = self.fetch()
        head = self._show_json(commit, HEAD_PATH)
        if head.get("schema") != "mos-git-state-head/v1":
            raise ValueError("unsupported Git state head schema")
        relative = Path(str(head.get("snapshot_path", "")))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("snapshots",):
            raise ValueError("unsafe snapshot path in Git state head")
        snapshot = self._show_json(commit, STATE_ROOT / relative)
        StateRelay._validate_envelope(snapshot)
        if snapshot.get("snapshot_id") != head.get("snapshot_id"):
            raise ValueError("Git state head points to the wrong snapshot")
        if snapshot.get("state_hash") != head.get("state_hash"):
            raise ValueError("Git state head hash mismatch")
        return {"state_commit": commit, "head": head, "snapshot": snapshot}

    def _worktree(self, base_commit: str) -> tuple[Path, Any]:
        root = Path(tempfile.mkdtemp(prefix="mos_state_worktree_"))
        root.rmdir()
        self._git("worktree", "add", "--detach", str(root), base_commit)

        def cleanup() -> None:
            self._git("worktree", "remove", "--force", str(root), check=False)
            if root.exists():
                shutil.rmtree(root)

        return root, cleanup

    def _commit_files(
        self,
        *,
        base_commit: str,
        files: dict[Path, dict[str, Any]],
        message: str,
    ) -> str:
        worktree, cleanup = self._worktree(base_commit)
        try:
            for relative, value in files.items():
                atomic_write_json(worktree / relative, value)
            self._git("add", "--", *(p.as_posix() for p in files), cwd=worktree)
            self._git(
                "-c", "user.name=MOS State Relay",
                "-c", "user.email=mos-state-relay@users.noreply.github.com",
                "commit", "-m", message,
                cwd=worktree,
            )
            return self._git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        finally:
            cleanup()

    def publish(
        self,
        *,
        expected_state_commit: str,
        snapshot: dict[str, Any],
        receipt: dict[str, Any],
        session_id: str,
        cycle_id: str,
    ) -> dict[str, Any]:
        StateRelay._validate_envelope(snapshot)
        current = self.fetch()
        safe_session = _safe_ref_part(session_id, "session")
        safe_cycle = _safe_ref_part(cycle_id, "cycle")
        snapshot_name = f"{snapshot['snapshot_id']}.json"
        snapshot_path = Path("snapshots") / snapshot_name
        files = {
            STATE_ROOT / snapshot_path: snapshot,
            STATE_ROOT / "sessions" / safe_session / f"{safe_cycle}.json": receipt,
        }

        if current != expected_state_commit:
            conflict_files = {
                STATE_ROOT / "conflicts" / safe_session / safe_cycle / "snapshot.json": snapshot,
                STATE_ROOT / "conflicts" / safe_session / safe_cycle / "receipt.json": {
                    **receipt,
                    "conflict": {"expected_state_commit": expected_state_commit, "actual_state_commit": current},
                },
            }
            commit = self._commit_files(
                base_commit=current,
                files=conflict_files,
                message=f"Preserve conflicting MOS cycle {cycle_id}",
            )
            conflict_branch = f"mos-conflict/{safe_session}/{safe_cycle}"
            self._git("push", self.remote, f"{commit}:refs/heads/{conflict_branch}")
            return {
                "status": "conflict",
                "expected_state_commit": expected_state_commit,
                "actual_state_commit": current,
                "conflict_branch": conflict_branch,
                "conflict_commit": commit,
            }

        head = {
            "schema": "mos-git-state-head/v1",
            "runtime_version": receipt["runtime_version"],
            "schema_version": snapshot["payload"]["schema_version"],
            "revision": snapshot["payload"]["revision"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_path": snapshot_path.as_posix(),
            "state_hash": snapshot["state_hash"],
            "parent_snapshot": snapshot["payload"].get("parent_snapshot"),
            "last_session_id": session_id,
            "last_cycle_id": cycle_id,
            "updated_at": utcnow(),
        }
        files[HEAD_PATH] = head
        commit = self._commit_files(
            base_commit=current,
            files=files,
            message=f"Checkpoint MOS cycle {cycle_id}",
        )
        pushed = self._git("push", self.remote, f"{commit}:refs/heads/{self.branch}", check=False)
        if pushed.returncode == 0:
            return {"status": "published", "state_commit": commit, "head": head}

        # A different writer won the race after our preflight check. Preserve
        # this exact candidate commit under a unique branch; never force-push.
        conflict_branch = f"mos-conflict/{safe_session}/{safe_cycle}"
        self._git("push", self.remote, f"{commit}:refs/heads/{conflict_branch}")
        actual = self.fetch()
        return {
            "status": "conflict",
            "expected_state_commit": expected_state_commit,
            "actual_state_commit": actual,
            "conflict_branch": conflict_branch,
            "conflict_commit": commit,
            "reason": "state branch advanced during publish",
        }
