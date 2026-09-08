from __future__ import annotations
from typing import Any, Protocol
from .storage import SQLiteStore
from .util import stable_id, utcnow, sha256_json

class ResearchAdapter(Protocol):
    def fetch(self, query: str) -> list[dict[str, Any]]: ...

class ResearchEngine:
    """Host-adapter research with evidence hashing and explicit network boundary."""
    def __init__(self, store: SQLiteStore, adapter: ResearchAdapter | None = None):
        self.store = store
        self.adapter = adapter

    @property
    def available(self) -> bool:
        return self.adapter is not None

    def run(self, query: str) -> dict[str, Any]:
        rid = stable_id("RG", {"query": query, "t": utcnow()})
        if self.adapter is None:
            result = {
                "research_id": rid,
                "query": query,
                "status": "adapter_unavailable",
                "evidence": [],
                "adapter_used": False,
                "network_used": False,
            }
        else:
            raw = self.adapter.fetch(query)
            if not isinstance(raw, list):
                raise TypeError("research adapter must return list[dict]")
            evidence = []
            for idx, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise TypeError(f"research evidence item {idx} must be dict")
                ev = dict(item)
                ev.setdefault("evidence_id", stable_id("EV", {"research_id": rid, "index": idx, "item": ev}))
                ev["evidence_sha256"] = sha256_json(ev)
                evidence.append(ev)
            result = {
                "research_id": rid,
                "query": query,
                "status": "completed",
                "evidence": evidence,
                "adapter_used": True,
                # Network use is proved per returned evidence item. Merely having
                # a network-capable adapter connected is not evidence of a request.
                "network_used": any(ev.get("network_used") is True for ev in evidence),
            }
        result["receipt_sha256"] = sha256_json(result)
        self.store.append_event(f"{rid}:research", "research", result)
        return result
