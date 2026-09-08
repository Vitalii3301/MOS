from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .git_state import GitCommandError, GitStateRepository, STATE_ROOT, HEAD_PATH, _safe_ref_part
from .runtime import MOSRuntime
from .util import atomic_write_json, sha256_json, utcnow


class EmbeddedHostLLM:
    """Deferred bridge for a model that already hosts the Python runtime."""

    mode = "embedded_host_pending"
    provider_active = False
    last_receipt = None

    def __init__(self) -> None:
        self.pending_payload: dict[str, Any] | None = None

    def generate(self, payload: dict[str, Any]) -> str:
        self.pending_payload = payload
        return "[MOS embedded host response pending]"


def _repo_default() -> Path:
    return Path(__file__).resolve().parents[3]


def _session_dir(repo_root: Path, session_id: str) -> Path:
    return repo_root / ".mos_sessions" / _safe_ref_part(session_id, "session")


def _read_argument(text: str | None, path: str | None, *, label: str) -> str:
    supplied = int(text is not None) + int(path is not None)
    if supplied > 1:
        raise ValueError(f"use only one of --{label} or --{label}-file")
    value = Path(path).read_text(encoding="utf-8") if path else (text if text is not None else sys.stdin.read())
    value = value.strip()
    if not value:
        raise ValueError(f"{label} is empty")
    if len(value.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError(f"{label} exceeds 4 MiB")
    return value


def _clear_sqlite(path: Path) -> None:
    for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if item.exists():
            item.unlink()


def begin_turn(
    *,
    repo_root: str | Path,
    session_id: str,
    user_input: str,
    remote: str = "origin",
    state_branch: str = "mos-state",
) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    session = _session_dir(repo_root, session_id)
    session.mkdir(parents=True, exist_ok=True)
    pending_path = session / "pending.json"
    if pending_path.exists():
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if pending.get("status") == "pending":
            raise RuntimeError("this session already has an uncommitted turn")

    state_source = GitStateRepository(repo_root, remote=remote, branch=state_branch).load()
    remote_snapshot_path = session / "source_snapshot.json"
    atomic_write_json(remote_snapshot_path, state_source["snapshot"])

    db_path = session / "runtime.sqlite3"
    _clear_sqlite(db_path)
    bridge = EmbeddedHostLLM()
    runtime = MOSRuntime(db_path, llm=bridge)
    try:
        runtime.relay.import_snapshot(remote_snapshot_path, create_backup=False)
        before = runtime.verify()
        if before["db_integrity"] != "ok" or not before["event_chain"]:
            raise RuntimeError("restored MOS state failed integrity verification")
        result = runtime.process(user_input)
        after = runtime.verify()
        if bridge.pending_payload is None:
            raise RuntimeError("embedded host bridge did not capture the MOS payload")
    finally:
        runtime.close()

    context = {
        "schema": "mos-embedded-turn/v1",
        "status": "ready_for_host_llm",
        "session_id": session_id,
        "source": {
            "state_branch": state_branch,
            "state_commit": state_source["state_commit"],
            "snapshot_id": state_source["snapshot"]["snapshot_id"],
            "state_hash": state_source["snapshot"]["state_hash"],
        },
        "cycle": {
            "cycle_id": result.cycle_id,
            "episode_id": result.episode_id,
            "receipt_hash": result.receipt_hash,
            "context_hash": result.context_hash,
            "selected_strategies": result.selected_strategies,
            "mandatory_checks": result.routed_checks,
        },
        "llm_context": bridge.pending_payload,
        "runtime_verify": after,
        "host_contract": {
            "external_llm_api_required": False,
            "instruction": "Draft the answer with the host chat model using llm_context; then run commit-turn before showing the answer.",
            "facts_inference_unknowns": "Keep sourced facts, inference, and unknowns distinct.",
        },
    }
    context_hash = sha256_json(context)
    context["turn_context_sha256"] = context_hash
    context_path = session / "turn_context.json"
    atomic_write_json(context_path, context)
    pending = {
        "schema": "mos-portable-pending/v1",
        "status": "pending",
        "session_id": session_id,
        "state_branch": state_branch,
        "remote": remote,
        "source_state_commit": state_source["state_commit"],
        "source_snapshot_id": state_source["snapshot"]["snapshot_id"],
        "source_state_hash": state_source["snapshot"]["state_hash"],
        "cycle_id": result.cycle_id,
        "episode_id": result.episode_id,
        "cycle_receipt_hash": result.receipt_hash,
        "turn_context_sha256": context_hash,
        "input_sha256": sha256_json(user_input),
        "created_at": utcnow(),
    }
    atomic_write_json(pending_path, pending)
    return {**context, "local": {"context_file": str(context_path), "pending_file": str(pending_path)}}


def commit_turn(
    *,
    repo_root: str | Path,
    session_id: str,
    response: str,
    host: str,
    transport: str = "auto",
) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    session = _session_dir(repo_root, session_id)
    pending_path = session / "pending.json"
    if not pending_path.exists():
        raise RuntimeError("no pending MOS turn for this session")
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if pending.get("status") != "pending":
        raise RuntimeError(f"MOS turn is not pending: {pending.get('status')}")

    db_path = session / "runtime.sqlite3"
    if not db_path.exists():
        raise RuntimeError("session runtime database is missing")
    runtime = MOSRuntime(db_path)
    snapshot_path = session / "publish_snapshot.json"
    try:
        embedded_receipt = runtime.commit_embedded_response(
            cycle_id=pending["cycle_id"],
            episode_id=pending["episode_id"],
            response=response,
            host=host,
            source_state_commit=pending["source_state_commit"],
        )
        snapshot = runtime.relay.export(snapshot_path, pending["source_snapshot_id"])
        verify = runtime.verify()
        if verify["db_integrity"] != "ok" or not verify["event_chain"]:
            raise RuntimeError("MOS state failed verification before publish")
    finally:
        runtime.close()

    receipt = {
        "schema": "mos-portable-turn-receipt/v1",
        "runtime_version": verify["runtime_version"],
        "session_id": session_id,
        "cycle_id": pending["cycle_id"],
        "episode_id": pending["episode_id"],
        "source_state_commit": pending["source_state_commit"],
        "source_snapshot_id": pending["source_snapshot_id"],
        "input_sha256": pending["input_sha256"],
        "turn_context_sha256": pending["turn_context_sha256"],
        "response_sha256": sha256_json(response),
        "embedded_response_event_hash": embedded_receipt["event_hash"],
        "new_snapshot_id": snapshot["snapshot_id"],
        "new_state_hash": snapshot["state_hash"],
        "verify": verify,
        "created_at": utcnow(),
    }
    bundle_root = session / "publish_bundle"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    snapshot_relative = Path("snapshots") / f"{snapshot['snapshot_id']}.json"
    receipt_relative = Path("sessions") / _safe_ref_part(session_id, "session") / f"{_safe_ref_part(pending['cycle_id'], 'cycle')}.json"
    head = {
        "schema": "mos-git-state-head/v1",
        "runtime_version": verify["runtime_version"],
        "schema_version": snapshot["payload"]["schema_version"],
        "revision": snapshot["payload"]["revision"],
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_path": snapshot_relative.as_posix(),
        "state_hash": snapshot["state_hash"],
        "parent_snapshot": snapshot["payload"].get("parent_snapshot"),
        "last_session_id": session_id,
        "last_cycle_id": pending["cycle_id"],
        "updated_at": utcnow(),
    }
    bundle_values = {
        STATE_ROOT / snapshot_relative: snapshot,
        STATE_ROOT / receipt_relative: receipt,
        HEAD_PATH: head,
    }
    bundle_files = []
    for repo_path, value in bundle_values.items():
        local_path = bundle_root / repo_path
        atomic_write_json(local_path, value)
        bundle_files.append({
            "repository_path": repo_path.as_posix(),
            "local_path": str(local_path),
            "content_sha256": sha256_json(value),
        })
    bundle = {
        "schema": "mos-github-connector-bundle/v1",
        "repository": "Vitalii3301/MOS",
        "branch": pending["state_branch"],
        "expected_parent_commit": pending["source_state_commit"],
        "commit_message": f"Checkpoint MOS cycle {pending['cycle_id']}",
        "files": bundle_files,
    }
    atomic_write_json(bundle_root / "BUNDLE.json", bundle)

    if transport not in {"auto", "direct", "bundle"}:
        raise ValueError("transport must be auto, direct, or bundle")
    if transport == "bundle":
        published = {"status": "upload_required", "bundle": bundle}
    else:
        try:
            published = GitStateRepository(
                repo_root, remote=pending["remote"], branch=pending["state_branch"]
            ).publish(
                expected_state_commit=pending["source_state_commit"],
                snapshot=snapshot,
                receipt=receipt,
                session_id=session_id,
                cycle_id=pending["cycle_id"],
            )
        except GitCommandError as exc:
            if transport == "direct":
                raise
            published = {"status": "upload_required", "reason": str(exc), "bundle": bundle}

    pending["status"] = published["status"]
    pending["new_snapshot_id"] = snapshot["snapshot_id"]
    pending["new_state_hash"] = snapshot["state_hash"]
    pending["published"] = published
    pending["committed_at"] = utcnow()
    atomic_write_json(pending_path, pending)
    return {
        "schema": "mos-portable-commit-result/v1",
        "status": published["status"],
        "session_id": session_id,
        "cycle_id": pending["cycle_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "state_hash": snapshot["state_hash"],
        "runtime_verify": verify,
        "git": published,
        "receipt": receipt,
    }


def acknowledge_connector_publish(*, repo_root: str | Path, session_id: str) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    session = _session_dir(repo_root, session_id)
    pending_path = session / "pending.json"
    if not pending_path.exists():
        raise RuntimeError("no MOS publication to acknowledge")
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if pending.get("status") == "published":
        return {"status": "published", "idempotent": True, "pending": pending}
    if pending.get("status") != "upload_required":
        raise RuntimeError(f"publication cannot be acknowledged from status {pending.get('status')}")
    source = GitStateRepository(
        repo_root, remote=pending["remote"], branch=pending["state_branch"]
    ).load()
    head = source["head"]
    if head.get("snapshot_id") != pending.get("new_snapshot_id"):
        raise RuntimeError("remote MOS state does not contain the expected snapshot")
    if head.get("state_hash") != pending.get("new_state_hash"):
        raise RuntimeError("remote MOS state hash does not match the expected publication")
    pending["status"] = "published"
    pending["published"] = {
        "status": "published",
        "state_commit": source["state_commit"],
        "transport": "github_connector",
    }
    pending["acknowledged_at"] = utcnow()
    atomic_write_json(pending_path, pending)
    return {
        "status": "published",
        "idempotent": False,
        "state_commit": source["state_commit"],
        "snapshot_id": head["snapshot_id"],
        "state_hash": head["state_hash"],
    }


def verify_remote_state(
    *, repo_root: str | Path, remote: str = "origin", state_branch: str = "mos-state"
) -> dict[str, Any]:
    source = GitStateRepository(repo_root, remote=remote, branch=state_branch).load()
    with tempfile.TemporaryDirectory(prefix="mos_portable_verify_") as tmp:
        tmp_path = Path(tmp)
        snapshot_path = tmp_path / "snapshot.json"
        atomic_write_json(snapshot_path, source["snapshot"])
        runtime = MOSRuntime(tmp_path / "runtime.sqlite3")
        try:
            runtime.relay.import_snapshot(snapshot_path, create_backup=False)
            verify = runtime.verify()
        finally:
            runtime.close()
    status = "verified" if verify["db_integrity"] == "ok" and verify["event_chain"] else "failed"
    return {
        "schema": "mos-git-state-verification/v1",
        "status": status,
        "state_commit": source["state_commit"],
        "head": source["head"],
        "runtime_verify": verify,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="mos-portable", description="MOS R4.2 portable embedded chat bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--repo", default="https://github.com/Vitalii3301/MOS.git")
    bootstrap.add_argument("--workspace", default="MOS")
    bootstrap.add_argument("--source-branch", default="main")
    bootstrap.add_argument("--state-branch", default="mos-state")

    begin = sub.add_parser("begin-turn")
    begin.add_argument("--repo-root", default=str(_repo_default()))
    begin.add_argument("--session", required=True)
    begin.add_argument("--text")
    begin.add_argument("--text-file")
    begin.add_argument("--remote", default="origin")
    begin.add_argument("--state-branch", default="mos-state")
    begin.add_argument("--output")

    commit = sub.add_parser("commit-turn")
    commit.add_argument("--repo-root", default=str(_repo_default()))
    commit.add_argument("--session", required=True)
    commit.add_argument("--response")
    commit.add_argument("--response-file")
    commit.add_argument("--host", required=True)
    commit.add_argument("--transport", choices=["auto", "direct", "bundle"], default="auto")
    commit.add_argument("--output")

    acknowledge = sub.add_parser("ack-publish")
    acknowledge.add_argument("--repo-root", default=str(_repo_default()))
    acknowledge.add_argument("--session", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--repo-root", default=str(_repo_default()))
    verify.add_argument("--remote", default="origin")
    verify.add_argument("--state-branch", default="mos-state")

    args = parser.parse_args()
    if args.command == "bootstrap":
        root = GitStateRepository.clone(args.repo, args.workspace, branch=args.source_branch)
        result = verify_remote_state(repo_root=root, state_branch=args.state_branch)
        result["workspace"] = str(root)
    elif args.command == "begin-turn":
        text = _read_argument(args.text, args.text_file, label="text")
        result = begin_turn(
            repo_root=args.repo_root, session_id=args.session, user_input=text,
            remote=args.remote, state_branch=args.state_branch,
        )
        if args.output:
            atomic_write_json(args.output, result)
    elif args.command == "commit-turn":
        response = _read_argument(args.response, args.response_file, label="response")
        result = commit_turn(
            repo_root=args.repo_root, session_id=args.session,
            response=response, host=args.host, transport=args.transport,
        )
        if args.output:
            atomic_write_json(args.output, result)
    elif args.command == "ack-publish":
        result = acknowledge_connector_publish(repo_root=args.repo_root, session_id=args.session)
    else:
        result = verify_remote_state(
            repo_root=args.repo_root, remote=args.remote, state_branch=args.state_branch,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "failed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
