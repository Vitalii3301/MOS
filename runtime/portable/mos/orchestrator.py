from __future__ import annotations
import threading
from pathlib import Path
from typing import Any, Callable
from .runtime import MOSRuntime
from .llm import LLMInterface

class MOSOrchestrator:
    """Persistent multi-node pool with explicit dispatch and memory synchronization."""
    def __init__(self, root: str | Path, n_nodes: int = 2, llm_factory: Callable[[int], LLMInterface] | None = None):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.nodes: list[MOSRuntime] = []
        self.lock = threading.RLock()
        for i in range(n_nodes):
            llm = llm_factory(i) if llm_factory else None
            r = MOSRuntime(self.root/f"node_{i}.sqlite3", llm=llm)
            r.store.set_state("node_id", f"node_{i}")
            self.nodes.append(r)

    def _score(self, r: MOSRuntime) -> float:
        s = r.state()
        return float(s.get("pain", 0))*2 + float(s.get("debt", 0)) + max(0, 30-float(s.get("energy", 100)))/10

    def pick(self) -> MOSRuntime:
        with self.lock:
            return min(self.nodes, key=self._score)

    def dispatch(self, text: str):
        return self.pick().process(text)

    def sync_memories(self, per_node_limit: int = 100) -> dict[str, int]:
        """Copy missing high-value memories between nodes without claiming P2P networking."""
        copied = 0
        with self.lock:
            material = []
            for idx, node in enumerate(self.nodes):
                rows = list(node.store.conn.execute(
                    "SELECT content,kind,importance,metadata FROM memory ORDER BY importance DESC,created_at DESC LIMIT ?",
                    (per_node_limit,),
                ))
                for row in rows:
                    material.append((idx, row["content"], row["kind"], float(row["importance"]), row["metadata"]))
            for source_idx, content, kind, importance, metadata_raw in material:
                for target_idx, target in enumerate(self.nodes):
                    if target_idx == source_idx: continue
                    exists = target.store.conn.execute("SELECT 1 FROM memory WHERE content=? AND kind=? LIMIT 1", (content, kind)).fetchone()
                    if exists: continue
                    import json
                    metadata = json.loads(metadata_raw)
                    metadata = {**metadata, "synced_from_node": f"node_{source_idx}"}
                    target.memory.add(content, kind=kind, importance=importance, metadata=metadata)
                    copied += 1
            for node in self.nodes:
                node.store.append_event(f"ORCH-SYNC-{node.state().get('cycle_count',0)}-{copied}-{node.state().get('node_id')}", "orchestrator_sync", {"copied_total": copied})
        return {"copied": copied, "nodes": len(self.nodes)}

    def start_reflection_all(self, interval: float = 10.0) -> int:
        return sum(1 for r in self.nodes if r.start_background_reflection(interval))

    def stop_reflection_all(self):
        for r in self.nodes: r.stop_background_reflection()

    def status(self) -> list[dict[str, Any]]:
        return [{"node_id": r.state().get("node_id"), "score": self._score(r), "verify": r.verify()} for r in self.nodes]

    def verify_all(self) -> list[dict[str, Any]]:
        return [r.verify() for r in self.nodes]

    def close(self):
        for r in self.nodes: r.close()
