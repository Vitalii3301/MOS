from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mos.portable import begin_turn, commit_turn, verify_remote_state
from mos.runtime import MOSRuntime
from mos.util import atomic_write_json


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


class PortableGitRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mos_portable_test_")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        seed = self.root / "seed"
        seed.mkdir()
        git(seed, "init", "-b", "main")
        git(seed, "config", "user.name", "MOS Test")
        git(seed, "config", "user.email", "mos-test@example.invalid")
        git(seed, "remote", "add", "origin", str(self.remote))

        state_root = seed / "runtime" / "portable" / "state"
        snapshot_dir = state_root / "snapshots"
        snapshot_dir.mkdir(parents=True)
        runtime = MOSRuntime(self.root / "seed.sqlite3")
        try:
            snapshot_file = self.root / "seed_snapshot.json"
            snapshot = runtime.relay.export(snapshot_file)
            self.assertTrue(runtime.verify()["event_chain"])
        finally:
            runtime.close()
        target = snapshot_dir / f"{snapshot['snapshot_id']}.json"
        shutil.copyfile(snapshot_file, target)
        atomic_write_json(state_root / "HEAD.json", {
            "schema": "mos-git-state-head/v1",
            "runtime_version": "4.2.0",
            "schema_version": snapshot["payload"]["schema_version"],
            "revision": snapshot["payload"]["revision"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_path": f"snapshots/{snapshot['snapshot_id']}.json",
            "state_hash": snapshot["state_hash"],
            "parent_snapshot": None,
            "last_session_id": None,
            "last_cycle_id": None,
            "updated_at": snapshot["payload"]["created_at"],
        })
        (seed / "README.md").write_text("MOS relay test\n", encoding="utf-8")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "Seed MOS state")
        git(seed, "push", "origin", "HEAD:refs/heads/main", "HEAD:refs/heads/mos-state")
        self.chat_a = self.root / "chat-a"
        self.chat_b = self.root / "chat-b"
        subprocess.run(["git", "clone", "--branch", "main", str(self.remote), str(self.chat_a)], check=True, capture_output=True)
        subprocess.run(["git", "clone", "--branch", "main", str(self.remote), str(self.chat_b)], check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_two_phase_turn_publishes_and_restores_host_answer(self) -> None:
        begun = begin_turn(repo_root=self.chat_a, session_id="chat-a", user_input="Проверь переносимый цикл")
        self.assertEqual(begun["status"], "ready_for_host_llm")
        self.assertFalse(begun["host_contract"]["external_llm_api_required"])
        self.assertEqual(begun["runtime_verify"]["db_integrity"], "ok")
        self.assertTrue(begun["runtime_verify"]["event_chain"])

        committed = commit_turn(
            repo_root=self.chat_a,
            session_id="chat-a",
            response="Ответ встроенной модели сохранён.",
            host="unit-test-host",
            transport="direct",
        )
        self.assertEqual(committed["status"], "published")
        checked = verify_remote_state(repo_root=self.chat_b)
        self.assertEqual(checked["status"], "verified")
        self.assertEqual(checked["runtime_verify"]["episodes"], 1)

        loaded = __import__("mos.git_state", fromlist=["GitStateRepository"]).GitStateRepository(self.chat_b).load()
        snapshot_file = self.root / "restored.json"
        atomic_write_json(snapshot_file, loaded["snapshot"])
        runtime = MOSRuntime(self.root / "restored.sqlite3")
        try:
            runtime.relay.import_snapshot(snapshot_file, create_backup=False)
            self.assertEqual(runtime.state()["last_response"], "Ответ встроенной модели сохранён.")
            self.assertTrue(runtime.verify()["event_chain"])
        finally:
            runtime.close()

    def test_parallel_chat_never_overwrites_winner(self) -> None:
        first = begin_turn(repo_root=self.chat_a, session_id="parallel-a", user_input="Первый параллельный ход")
        second = begin_turn(repo_root=self.chat_b, session_id="parallel-b", user_input="Второй параллельный ход")
        self.assertEqual(first["source"]["state_commit"], second["source"]["state_commit"])

        winner = commit_turn(
            repo_root=self.chat_a, session_id="parallel-a", response="Первый ответ",
            host="unit-test-a", transport="direct",
        )
        loser = commit_turn(
            repo_root=self.chat_b, session_id="parallel-b", response="Второй ответ",
            host="unit-test-b", transport="direct",
        )
        self.assertEqual(winner["status"], "published")
        self.assertEqual(loser["status"], "conflict")
        refs = git(self.chat_b, "ls-remote", "--heads", "origin", "mos-conflict/*")
        self.assertIn(loser["git"]["conflict_commit"], refs)
        checked = verify_remote_state(repo_root=self.chat_b)
        self.assertEqual(checked["head"]["last_cycle_id"], winner["cycle_id"])

    def test_bundle_transport_prepares_connector_write_without_claiming_publish(self) -> None:
        begin_turn(repo_root=self.chat_a, session_id="connector-chat", user_input="Подготовь connector bundle")
        result = commit_turn(
            repo_root=self.chat_a, session_id="connector-chat", response="Ответ для bundle",
            host="connector-test", transport="bundle",
        )
        self.assertEqual(result["status"], "upload_required")
        bundle = result["git"]["bundle"]
        self.assertEqual(bundle["schema"], "mos-github-connector-bundle/v1")
        self.assertEqual(len(bundle["files"]), 3)
        for item in bundle["files"]:
            self.assertTrue(Path(item["local_path"]).exists())


if __name__ == "__main__":
    unittest.main()
