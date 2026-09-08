from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

@dataclass
class MemoryRecord:
    memory_id: str
    content: str
    kind: str = "working"
    importance: float = 0.5
    strength: float = 1.0
    usage_count: int = 0
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    vector: list[float] = field(default_factory=list)

@dataclass
class StrategyDef:
    strategy_id: str
    name: str
    level: int
    triggers: list[str]
    action_kind: str
    action_arg: Optional[str] = None
    priority: float = 1.0
    parent_id: Optional[str] = None
    status: str = "active"
    version: int = 1
    learned_from: list[str] = field(default_factory=list)

@dataclass
class Episode:
    episode_id: str
    goal: str
    hypothesis: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    decision: Optional[str] = None
    action: Optional[str] = None
    outcome: Optional[str] = None
    revision: Optional[str] = None
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""

@dataclass
class ContextItem:
    item_id: str
    kind: str
    text: str
    source: str
    relevance: float = 0.5
    importance: float = 0.5
    uncertainty: float = 0.0
    contradiction_pressure: float = 0.0
    recency: float = 0.5
    attention_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class CycleResult:
    cycle_id: str
    response: str
    context_hash: str
    episode_id: str
    selected_strategies: list[str]
    routed_checks: list[dict[str, Any]]
    state: dict[str, Any]
    receipt_hash: str
    hypothesis_id: Optional[str] = None
    research: Optional[dict[str, Any]] = None

def to_dict(obj):
    return asdict(obj)
