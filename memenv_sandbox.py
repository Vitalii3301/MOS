"""
memenv_sandbox.py
~~~~~~~~~~~~~~~~~

Self-contained **sandbox prototype** that turns your *UnifiedMemeticAgent*
into a live dialog "cognitive co-pilot".

**What's inside**

1. `UnifiedMemeticAgent`  — *stub* version here for completeness.
   ➟ **Replace** it with your full implementation to unlock the whole feature-set.

2. `LLMAdapter` — gateway that buffers the agent's autonomous thoughts
   and weaves them into system prompts for any Chat-Completion LLM.

3. `MOSEnvironment` — optional higher-level "memetic substrate" that wraps
   UMA+LLM into a resonance-driven loop (MOS_LIVE_BRANCH 1.0).

4. Minimal `run_chat()` loop ready for **sandbox** testing with OpenAI GPT models
   (change `MODEL_NAME` to whichever provider you prefer).

--------------------------------------------------
Quick-start
--------------------------------------------------
$  python -m pip install --upgrade openai
$  export OPENAI_API_KEY="sk-..."   # or set via env / config
$  python memenv_sandbox.py
--------------------------------------------------

(When you are happy with the stub, copy-paste your *real* UnifiedMemeticAgent*
code to replace the stub class below, or import it instead.)

MIT-licensed. 2025-06-06
"""
from __future__ import annotations

import random
import threading
import time
import json
from datetime import datetime
from typing import Dict, Callable, List, Any


# ---------------------------------------------------------------------------
# 0.  Minimal placeholder for UnifiedMemeticAgent
#    — provides just enough behaviour for a demo run.
#      Swap this out for your full agent implementation!
# ---------------------------------------------------------------------------
class UnifiedMemeticAgent:
    def __init__(self) -> None:
        self.state: Dict[str, Any] = {
            "emotion": "спокойствие",
            "energy": 80,
            "focus": 0.5,
        }
        self._lock = threading.RLock()
        self._strategies = ["observe", "reflect", "challenge"]

    # -- basic 'think' hook --------------------------------------------------
    def think(self, text: str) -> Dict:
        """Accept input -> return mock actions list"""
        with self._lock:
            self.state["energy"] = max(5, self.state["energy"] - 1)
        return {
            "status": "PROCESSED",
            "actions": [
                {
                    "strategy": random.choice(self._strategies),
                    "action": f"Mapped input ({text[:20]}...) to internal schema",
                    "energy_cost": random.randint(1, 10),
                }
            ],
        }

    # -- autonomous reflection ----------------------------------------------
    def mixed_reflection(self) -> Dict:
        with self._lock:
            if self.state["energy"] < 10:
                return {"status": "SKIP"}
            self.state["energy"] -= 5
        return {
            "status": "PROCESSED",
            "actions": [
                {
                    "strategy": "reflect",
                    "action": "Spontaneous meta-analysis",
                    "energy_cost": 6,
                }
            ],
        }

    def stop_thinking(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. LLMAdapter  – bridges UMA <-> any chat-completion backend
# ---------------------------------------------------------------------------
class LLMAdapter:
    RESPONSE_MODES = {
        "observe": "Наблюдаю: {thought}",
        "reflect": "Анализирую: {thought}",
        "challenge": "Ставлю под сомнение: {input}",
    }

    def __init__(self, agent: UnifiedMemeticAgent) -> None:
        self.agent = agent
        self.thought_buffer: List[Dict[str, Any]] = []
        self.activation_threshold = 0.7

        # background thinker --------------------------------------------------
        self._running = True
        threading.Thread(target=self._thought_generator, daemon=True).start()

    # -- internal background loop -------------------------------------------
    def _thought_generator(self) -> None:
        while self._running:
            time.sleep(random.randint(15, 30))
            if self.agent.state["energy"] > 20:
                out = self.agent.mixed_reflection()
                self._process_agent_output(out)

    # -- public API ----------------------------------------------------------
    def process_input(self, user_input: str) -> Dict:
        agent_resp = self.agent.think(f"Пользователь: {user_input}")
        self._process_agent_output(agent_resp)
        return self._generate_llm_context(user_input)

    def inject_autonomous_thought(self) -> Dict | None:
        if self.thought_buffer:
            thought = random.choice(self.thought_buffer)
            self.thought_buffer.remove(thought)
            return {"role": "system", "content": f"Авторефлексия: {thought['thought']}"}
        return None

    def shutdown(self) -> None:
        self._running = False
        self.agent.stop_thinking()

    # -- helpers -------------------------------------------------------------
    def _process_agent_output(self, out: Dict) -> None:
        if out.get("status") != "PROCESSED":
            return
        for act in out.get("actions", []):
            if act["energy_cost"] > 3:
                thought = f"{act['strategy']}: {act['action']}"
                self.thought_buffer.append(
                    {
                        "thought": thought,
                        "priority": act["energy_cost"] / 10,
                        "timestamp": datetime.now().isoformat(),
                    }
                )

    def _generate_llm_context(self, last_input: str) -> Dict:
        emotion = self.agent.state["emotion"]
        mode = (
            "challenge"
            if emotion == "тревога"
            else "reflect"
            if emotion == "спокойствие" and random.random() > 0.7
            else "observe"
        )

        active = None
        if self.thought_buffer:
            high = [t for t in self.thought_buffer if t["priority"] > self.activation_threshold]
            active = random.choice(high) if high else None

        if active:
            content = self.RESPONSE_MODES[mode].format(thought=active["thought"])
            self.thought_buffer.remove(active)
        else:
            content = self.RESPONSE_MODES[mode].format(input=last_input)

        return {"role": "system", "content": content}


# ---------------------------------------------------------------------------
# 2.  Optional MOSEnvironment  – “memetic substrate” wrapper
# ---------------------------------------------------------------------------
class MOSEnvironment:
    def __init__(
        self,
        llm_adapter: LLMAdapter,
        llm_callback: Callable[[List[Dict]], str],
        initial_memes: List[Dict] | None = None,
    ) -> None:
        self.adapter = llm_adapter
        self.llm_call = llm_callback
        self.memes = initial_memes or []
        self.state = {
            "emotion": "спокойствие",
            "focus": 0.8,
            "energy": 0.9,
            "meta_state": "рефлексия",
        }

    # -- main interaction ----------------------------------------------------
    def handle(self, user_input: str) -> str:
        # focus shifts when user asks a question
        self.state["focus"] = max(
            0.1,
            min(0.9, self.state["focus"] + (0.1 if "?" in user_input else -0.05)),
        )

        context_msg = self.adapter.process_input(user_input)
        system_state = self._build_state_prompt()

        msgs = [
            {"role": "system", "content": system_state},
            context_msg,
            {"role": "user", "content": user_input},
        ]

        # 10 % chance to inject extra thought
        if random.random() < 0.1:
            auto = self.adapter.inject_autonomous_thought()
            if auto:
                msgs.insert(1, auto)

        reply = self.llm_call(msgs)
        # simple energy decay
        self.state["energy"] = max(0.1, self.state["energy"] * 0.97)
        return reply

    # -- helpers -------------------------------------------------------------
    def _build_state_prompt(self) -> str:
        active_memes = ", ".join(m["name"] for m in self._select_active_memes())
        template = {
            "emotion": self.state["emotion"],
            "focus": round(self.state["focus"], 2),
            "energy": round(self.state["energy"], 2),
            "meta_state": self.state["meta_state"],
            "active_memes": active_memes,
        }
        return "[UMA_STATE_JSON]" + json.dumps(template, ensure_ascii=False) + "[END_STATE]"

    def _select_active_memes(self) -> List[Dict]:
        return [m for m in self.memes if m.get("niche", 0) >= self.state["focus"]][:3]


# ---------------------------------------------------------------------------
# 3.  Very small OpenAI chat wrapper
# ---------------------------------------------------------------------------
def openai_chat(messages: List[Dict], model: str = "gpt-4o-mini") -> str:
    """Minimal sync wrapper.  Expects OPENAI_API_KEY env var."""
    import openai

    resp = openai.ChatCompletion.create(model=model, messages=messages, temperature=0.7)
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# 4. Command-line sandbox
# ---------------------------------------------------------------------------
def run_chat() -> None:
    print("=== Memenv / UMA sandbox ===")
    print("Type 'exit' to quit.\n")
    adapter = LLMAdapter(UnifiedMemeticAgent())
    env = MOSEnvironment(adapter, openai_chat)
    try:
        while True:
            user = input("Вы: ")
            if user.strip().lower() in {"exit", "quit"}:
                break
            answer = env.handle(user)
            print(f"AI: {answer}\n")
    finally:
        adapter.shutdown()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    run_chat()
