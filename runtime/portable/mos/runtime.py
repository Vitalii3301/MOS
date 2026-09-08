from __future__ import annotations
import json, threading, uuid
from pathlib import Path
from typing import Any
from .storage import SQLiteStore
from .memory import MemorySystem
from .strategies import StrategyGenome
from .memes import MemeEngine
from .epistemic import EpistemicLedger
from .hypotheses import HypothesisTracker
from .metacog import MetaCognition
from .context import ContextCompiler
from .checks import MandatoryCheckRouter
from .llm import LLMInterface
from .research import ResearchEngine
from .relay import StateRelay
from .mpl import MPLInterpreter
from .state import StateController
from .observer import SelfObserver
from .util import utcnow, sha256_json
from .models import CycleResult

DEFAULT_STATE = {
    "emotion": "motivated",
    "current_goal": "improve_system_reliably",
    "energy": 100.0,
    "energy_recovery_rate": 0.7,
    "confidence": 0.75,
    "debt": 0.0,
    "pain": 0.0,
    "context": {"language": "ru", "tone": "technical"},
    "cycle_count": 0,
    # This stored value describes support, not a live connection. state() and
    # verify() replace it with a dynamic, explicit support/active matrix.
    "capabilities": {},
}

class MOSRuntime:
    VERSION = "4.2.0"

    def __init__(self, db_path: str | Path, llm: LLMInterface | None = None, research_adapter=None):
        self.store = SQLiteStore(db_path)
        self.llm = llm or LLMInterface()
        self.cycle_lock = threading.RLock()
        for k, v in DEFAULT_STATE.items():
            if self.store.get_state(k, None) is None:
                self.store.set_state(k, v)
        self.memory = MemorySystem(self.store)
        self.strategies = StrategyGenome(self.store, self.memory)
        self.memes = MemeEngine(self.store)
        self.episodes = EpistemicLedger(self.store)
        self.hypotheses = HypothesisTracker(self.store)
        self.meta = MetaCognition(self.store)
        self.compiler = ContextCompiler(self.memory, self.strategies, self.episodes, self.meta, self.hypotheses, self.memes)
        self.checks = MandatoryCheckRouter()
        self.research = ResearchEngine(self.store, research_adapter)
        self.relay = StateRelay(self.store)
        self.mpl = MPLInterpreter(self.memes)
        self.state_controller = StateController(self.store)
        self.observer = SelfObserver(self)
        self.store.set_state("capabilities", self.capability_status())
        self._ensure_self_model()

    def _ensure_self_model(self):
        self.meta.self_item(
            "capability", "persistence", "Transactional SQLite-backed state is available in this host runtime.",
            confidence=1.0, evidence_refs=["runtime:v4"], scope_limit="Only while the persistent database file is retained."
        )
        self.meta.self_item(
            "capability", "llm_bridge", "External LLM invocation requires an explicit host adapter.",
            confidence=1.0, evidence_refs=["runtime:v4"], scope_limit="No hidden provider/network claim."
        )
        self.meta.self_item(
            "capability", "embedded_host_bridge", "A tool-enabled host chat can run MOS before drafting and commit its answer afterward.",
            confidence=1.0, evidence_refs=["runtime:v4.2", "tests:portable"],
            scope_limit="The Python runtime does not call the built-in host model directly."
        )
        self.meta.self_item(
            "mandatory_check", "evidence", "Verify externally checkable claims before promoting them to fact.",
            confidence=1.0, evidence_refs=["runtime:v4"], scope_limit="Research executes only through an explicit adapter."
        )
        self.meta.self_item(
            "capability", "meme_execution", "Versioned memes execute in a restricted subprocess sandbox with timeout and JSON boundaries.",
            confidence=1.0, evidence_refs=["runtime:v4", "tests:sandbox"], scope_limit="Best-effort restricted Python sandbox, not a hardened VM/container."
        )
        self.meta.self_item(
            "capability", "epistemic_replay", "Episodes and system events are persisted with an append-only hash chain.",
            confidence=1.0, evidence_refs=["runtime:v4", "tests:event_chain"], scope_limit="Integrity is local; no external signature authority is implied."
        )
        self.meta.self_item(
            "capability", "full_state_relay", "Relay snapshots transfer state, memory, hypotheses, strategies, memes, episodes, meta-memory, self-model and event history.",
            confidence=1.0, evidence_refs=["runtime:v4", "tests:full_relay"], scope_limit="Snapshot catalog itself is not recursively embedded."
        )
        self.meta.self_item(
            "weakness", "provider_integration", "No external LLM provider is active unless the host explicitly supplies a callable or subprocess bridge.",
            confidence=1.0, status="active", evidence_refs=["runtime:v4"], scope_limit="Plumbing is verified; provider execution is host-dependent."
        )
        self.meta.self_item(
            "weakness", "host_lifetime", "Background reflection stops when the host process is destroyed.",
            confidence=1.0, status="active", evidence_refs=["runtime:v4"], scope_limit="Use an external supervisor/service for always-on operation."
        )
        self.meta.self_item(
            "boundary", "self_improvement", "Strategy changes remain candidates until an explicit blind, regression-free holdout evaluation activates them.",
            confidence=1.0, status="active", evidence_refs=["runtime:v4.1", "tests:strategy_gate"],
            scope_limit="MOS does not train or modify base-model weights and does not invent program candidates autonomously."
        )

    def capability_status(self) -> dict[str, Any]:
        def item(supported: bool, active: bool, boundary: str) -> dict[str, Any]:
            return {"supported": bool(supported), "active": bool(active), "boundary": boundary}
        provider_active=bool(getattr(self.llm,"provider_active",False))
        research_active=bool(getattr(self.research,"available",False))
        observer_active=bool(getattr(self.observer,"running",False))
        embedded_active=getattr(self.llm,"mode","")=="embedded_host_pending"
        return {
            "persistent_state": item(True, True, "Only while the SQLite file is retained."),
            "strategy_learning": item(True, True, "Learns routing features; proposed descendants require a blind holdout gate."),
            "candidate_code_selection": item(True, True, "Selects only host-supplied code candidates in the restricted worker."),
            "epistemic_event_chain": item(True, True, "Local SHA-256 chain; no external signature authority."),
            "full_state_relay": item(True, True, "Snapshot import requires the same schema version."),
            "external_llm_bridge": item(True, provider_active, "A real provider is active only when the host supplies one."),
            "embedded_host_bridge": item(True, embedded_active, "The host chat generates the answer between begin-turn and commit-turn."),
            "research_adapter": item(True, research_active, "Internet is used only when connected evidence proves a network request."),
            "background_observer": item(True, observer_active, "A timer/maintenance loop, not introspection or consciousness."),
            "hidden_cross_chat_memory": item(False, False, "No hidden memory outside the retained database/snapshot."),
            "base_model_weight_change": item(False, False, "No model training or weight updates."),
            "consciousness_or_feelings": item(False, False, "State labels are heuristic numbers, not subjective experience."),
        }

    def state(self):
        state=self.store.all_state()
        state["capabilities"]=self.capability_status()
        return state

    def set_goal(self, goal: str):
        with self.cycle_lock:
            self.store.set_state("current_goal", goal)
            self.memory.add(goal, kind="goal", importance=.95, metadata={"source": "goal"})

    def _research_requested(self, selected, routed_checks) -> bool:
        return (
            any(s.action_kind == "research" for _, s in selected)
            or any(c.get("check_id") == "evidence_required" for c in routed_checks)
        )

    def process(self, user_input: str, *, hypothesis: str | None = None, decision: str | None = None) -> CycleResult:
        # A single runtime serializes cognitive cycles. Multi-node parallelism is
        # provided by MOSOrchestrator, avoiding state/event interleaving in one DB.
        with self.cycle_lock:
            state = self.state()
            goal = state.get("current_goal", "")
            cycle_id = f"CYCLE-{uuid.uuid4().hex[:12]}"
            episode_id = None
            hypothesis_id = None
            try:
                self.memory.add(user_input, kind="user", importance=.75, metadata={"cycle_id": cycle_id})
                selected = self.strategies.select(user_input, goal, top_k=5)
                state = self.state_controller.pre_cycle(user_input, len(selected))
                goal = state.get("current_goal", "")
                strategy_outputs = [self.strategies.render(s, user_input, state) for _, s in selected]
                context = self.compiler.compile(goal, user_input, state)
                routed = self.checks.route(context, user_input)

                research_result = None
                if self._research_requested(selected, routed):
                    research_result = self.research.run(user_input)
                    if research_result.get("status") == "completed":
                        for ev in research_result.get("evidence", []):
                            self.memory.add(
                                json.dumps(ev, ensure_ascii=False, sort_keys=True),
                                kind="research",
                                importance=.92,
                                metadata={"research_id": research_result["research_id"], "evidence_id": ev.get("evidence_id")},
                            )
                        # Recompile so retrieved research evidence can actually enter the LLM-facing context.
                        context = self.compiler.compile(goal, user_input, self.state())
                        routed = self.checks.route(context, user_input)

                hyp = hypothesis or f"Selected strategies and available evidence can make progress on '{goal}'."
                hypothesis_id = self.hypotheses.propose(hyp, expected_outcome="progress", probability=.5)
                episode_id = self.episodes.open(goal, hyp)
                self.episodes.add_evidence(episode_id, {"kind": "hypothesis_record", "hypothesis_id": hypothesis_id})
                self.episodes.add_evidence(episode_id, {"kind": "user_input", "content": user_input, "cycle_id": cycle_id})
                self.episodes.add_evidence(episode_id, {"kind": "compiled_context", "sha256": context["context_sha256"], "selected": context["budget"]["selected"]})
                self.episodes.add_evidence(episode_id, {"kind": "strategy_selection", "ids": [s.strategy_id for _, s in selected], "names": [s.name for _, s in selected]})
                for chk in routed:
                    self.episodes.add_evidence(episode_id, {"kind": "mandatory_check", "result": chk})
                if research_result is not None:
                    self.episodes.add_evidence(episode_id, {"kind": "research", "result": research_result})

                payload = {
                    "schema": "mos-llm-cycle/v4.1",
                    "runtime_version": self.VERSION,
                    "cycle_id": cycle_id,
                    "user_input": user_input,
                    "active_context": context,
                    "strategy_outputs": strategy_outputs,
                    "mandatory_checks": routed,
                    "research": research_result,
                    "hypothesis_id": hypothesis_id,
                    "state_boundary": {"base_model_weight_change": False, "hidden_cross_chat_memory": False, "consciousness_or_feelings": False},
                }
                response = self.llm.generate(payload)
                self.memory.add(response, kind="agent", importance=.72, metadata={
                    "cycle_id": cycle_id, "llm_mode": self.llm.mode, "observer_running": self.observer.running,
                })
                dec = decision or "respond_with_current_best_evidence"
                self.episodes.decide(episode_id, dec, f"LLM bridge mode={self.llm.mode}")
                self.strategies.record([s.strategy_id for _, s in selected])
                count = int(state.get("cycle_count", 0)) + 1
                self.store.set_state("cycle_count", count)
                self.store.set_state("last_input", user_input)
                self.store.set_state("last_response", response)
                learned = self.strategies.learn()
                receipt = {
                    "schema": "mos-cycle-receipt/v4.1",
                    "cycle_id": cycle_id,
                    "episode_id": episode_id,
                    "hypothesis_id": hypothesis_id,
                    "context_sha256": context["context_sha256"],
                    "selected_strategies": [s.name for _, s in selected],
                    "learned_strategies": learned,
                    "mandatory_checks": routed,
                    "research_status": None if research_result is None else research_result.get("status"),
                    "llm_mode": self.llm.mode,
                    "provider_active": bool(getattr(self.llm,"provider_active",False)),
                    "provider_receipt": getattr(self.llm,"last_receipt",None),
                    "response_sha256": sha256_json(response),
                    "created_at": utcnow(),
                }
                rh = self.store.append_event(f"{cycle_id}:receipt", "cycle_receipt", receipt)
                self.observer.observe("cycle")
                return CycleResult(
                    cycle_id, response, context["context_sha256"], episode_id,
                    [s.name for _, s in selected], routed, self.state(), rh,
                    hypothesis_id=hypothesis_id, research=research_result,
                )
            except Exception as exc:
                error_body = {"cycle_id": cycle_id, "error": f"{type(exc).__name__}: {exc}", "episode_id": episode_id, "hypothesis_id": hypothesis_id}
                if episode_id is not None:
                    try:
                        self.episodes.outcome(episode_id, f"cycle_error:{type(exc).__name__}", "repair_or_retry_failed_cycle")
                        self.meta.learn_from_episode(self.episodes.get(episode_id))
                    except Exception:
                        pass
                self.state_controller.outcome(False, revision=True)
                self.store.set_state("last_error", error_body)
                self.store.append_event(f"{cycle_id}:error", "cycle_error", error_body)
                raise

    def record_outcome(self, episode_id: str, outcome: str, revision: str | None = None, success: bool | None = None):
        with self.cycle_lock:
            self.episodes.outcome(episode_id, outcome, revision)
            ep = self.episodes.get(episode_id)
            self.meta.learn_from_episode(ep)
            proposed = []
            hypothesis_update = None
            selections = [x for x in ep.evidence if x.get("kind") == "strategy_selection"]
            ids = selections[-1].get("ids", []) if selections else []
            hypotheses = [x for x in ep.evidence if x.get("kind") == "hypothesis_record"]
            hid = hypotheses[-1].get("hypothesis_id") if hypotheses else None
            if success is not None:
                if ids:
                    self.strategies.feedback(ids, bool(success))
                if hid:
                    hypothesis_update = self.hypotheses.update(hid, bool(success))
                if success:
                    proposed = self.strategies.evolve(max_new=2)
                self.store.append_event(f"{episode_id}:feedback", "feedback", {
                    "episode_id": episode_id, "success": success, "strategy_ids": ids,
                    "strategy_candidates_proposed": proposed, "hypothesis_update": hypothesis_update,
                })
            self.state_controller.outcome(success, revision=bool(revision))
            return {"episode_id": episode_id, "success": success, "revision": revision,
                    "strategy_candidates_proposed": proposed, "evolved": [], "hypothesis_update": hypothesis_update}

    def commit_embedded_response(
        self,
        *,
        cycle_id: str,
        episode_id: str,
        response: str,
        host: str,
        source_state_commit: str | None = None,
    ) -> dict[str, Any]:
        """Commit the response produced by the LLM that hosts MOS.

        Embedded mode deliberately does not call the host model from Python.
        The host runs ``process()``, receives the MOS context, produces its
        answer, and then calls this method before the answer is shown.  The
        operation is idempotent for an identical response and rejects a second,
        different response for the same cycle.
        """
        response = str(response).strip()
        host = str(host).strip()
        if not response:
            raise ValueError("embedded host response is empty")
        if not host:
            raise ValueError("embedded host identity is empty")
        event_id = f"{cycle_id}:embedded_host_response"
        existing = self.store.conn.execute(
            "SELECT body,event_hash FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        response_hash = sha256_json(response)
        if existing:
            body = json.loads(existing["body"])
            if body.get("response_sha256") != response_hash:
                raise RuntimeError("a different embedded response is already committed for this cycle")
            return {**body, "event_hash": existing["event_hash"], "idempotent": True}

        episode = self.episodes.get(episode_id)
        cycle_evidence = [
            item for item in episode.evidence
            if item.get("kind") == "user_input" and item.get("cycle_id") == cycle_id
        ]
        if not cycle_evidence:
            raise ValueError("episode does not belong to the supplied cycle")

        body = {
            "schema": "mos-embedded-response/v1",
            "cycle_id": cycle_id,
            "episode_id": episode_id,
            "host": host,
            "source_state_commit": source_state_commit,
            "response_sha256": response_hash,
            "created_at": utcnow(),
        }
        self.memory.add(response, kind="host_llm", importance=.78, metadata={
            "cycle_id": cycle_id,
            "episode_id": episode_id,
            "host": host,
            "response_sha256": response_hash,
        })
        self.episodes.add_evidence(episode_id, {"kind": "embedded_host_response", **body})
        self.store.set_state("last_response", response)
        self.store.set_state("last_embedded_host", host)
        event_hash = self.store.append_event(event_id, "embedded_host_response", body)
        return {**body, "event_hash": event_hash, "idempotent": False}

    def activate_strategy_candidate(self, name: str, evaluation: dict[str, Any]) -> dict[str, Any]:
        with self.cycle_lock:
            return self.strategies.activate_candidate(name,evaluation)

    def validate_weakness(self, domain: str, *, passed: bool, evidence_ref: str) -> dict[str, Any]:
        with self.cycle_lock:
            return self.meta.validate_weakness(domain, passed=passed, evidence_ref=evidence_ref)

    def verify(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "runtime_version": self.VERSION,
            "event_chain": self.store.verify_event_chain(),
            "db_integrity": self.store.conn.execute("PRAGMA integrity_check").fetchone()[0],
            "strategies": len(self.strategies.all()),
            "strategy_candidates": len(self.strategies.candidates()),
            "quarantined_strategies": self.store.conn.execute("SELECT count(*) n FROM strategies WHERE status='quarantined'").fetchone()["n"],
            "rejected_strategies": self.store.conn.execute("SELECT count(*) n FROM strategies WHERE status='rejected'").fetchone()["n"],
            "memes": self.store.conn.execute("SELECT count(*) n FROM memes").fetchone()["n"],
            "episodes": self.store.conn.execute("SELECT count(*) n FROM episodes").fetchone()["n"],
            "hypotheses": self.store.conn.execute("SELECT count(*) n FROM hypotheses").fetchone()["n"],
            "memory": self.store.conn.execute("SELECT count(*) n FROM memory").fetchone()["n"],
            "meta_memory": self.store.conn.execute("SELECT count(*) n FROM meta_memory").fetchone()["n"],
            "self_model": self.store.conn.execute("SELECT count(*) n FROM self_model").fetchone()["n"],
            "llm_mode": self.llm.mode,
            "llm_provider_active": bool(getattr(self.llm,"provider_active",False)),
            "research_adapter": self.research.available,
            "observer_running": self.observer.running,
            "capabilities": self.capability_status(),
        }

    def start_background_reflection(self, interval: float = 10.0):
        return self.observer.start(interval)

    def stop_background_reflection(self):
        self.observer.stop()

    def close(self):
        self.observer.stop()
        self.store.close()
