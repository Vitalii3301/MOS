import json
from datetime import datetime
import random
import threading
import time
from pathlib import Path
from typing import List, Dict, Optional


def normalize_text(text: str) -> str:
    punctuation = ".,;:!?\"'()-—[]{}"
    for p in punctuation:
        text = text.replace(p, "")
    return text.lower()


def keyword_in_text(text: str, keywords: List[str]) -> bool:
    norm_text = normalize_text(text)
    return any(kw in norm_text for kw in keywords)


class ThinkingStrategy:
    """Strategy object used by :class:`UnifiedMemeticAgent`."""

    def __init__(
        self,
        name: str,
        level: int,
        trigger_topics: List[str],
        action_plan: str,
        activation_conditions: Optional[Dict] = None,
        children: Optional[List["ThinkingStrategy"]] = None,
    ) -> None:
        self.name = name
        self.level = level
        self.trigger_topics = trigger_topics
        self.action_plan = action_plan
        self.activation_conditions = activation_conditions or {}
        self.children = children or []

    def is_applicable(self, emotion: str, goal: str, thought_text: str, energy: int) -> bool:
        if self.level == 1 and energy < 20:
            return False
        if self.activation_conditions:
            if "emotions" in self.activation_conditions and emotion not in self.activation_conditions["emotions"]:
                return False
            if "goals" in self.activation_conditions and goal not in self.activation_conditions["goals"]:
                return False
            if "keywords" in self.activation_conditions:
                if not keyword_in_text(thought_text, self.activation_conditions["keywords"]):
                    return False
        if self.trigger_topics and not keyword_in_text(thought_text, self.trigger_topics):
            return False
        return True


class UnifiedMemeticAgent:
    """Simple agent managing memes, memory and thinking strategies."""

    def __init__(self, storage_path: str = "mos_memory.json", stats_path: str = "strategy_stats.json"):
        self.memes: Dict[str, str] = {}
        self.memory: Dict[str, List] = {"goals": [], "thoughts": [], "log": []}
        self.strategy_stats: Dict[str, Dict[str, int]] = {}
        self.storage_path = storage_path
        self.stats_path = stats_path

        self.auto_thought_pool = [
            "Что бы улучшило текущую цель?",
            "Какие мемы наиболее пригодны?",
            "Что я могу забыть?",
            "Какую стратегию стоит попробовать?",
            "Есть ли конфликт между целями?",
        ]

        self.meta_thought_pool = [
            "Как я себя чувствую?",
            "Какие стратегии я использовал сегодня?",
            "Почему я выбрал стратегию X?",
            "Что можно улучшить в моем мышлении?",
            "Какая моя цель сейчас и соответствует ли она моим действиям?",
        ]

        self.thinking = False
        self.strategy_tree: List[ThinkingStrategy] = []
        self.state = {
            "emotion": "тревога",
            "current_goal": "устранение противоречий",
            "energy": 90,
            "reputation": 0.87,
        }

        self.load_from_file()
        self.load_stats()

    # --- meme manipulation -------------------------------------------------
    def add_meme(self, name: str, content: str) -> None:
        self.memes[name] = content
        self.log(f"Мем добавлен: {name}")
        self.save_to_file()

    def mutate_meme(self, name: str) -> None:
        if name in self.memes:
            self.memes[name] += " (модифицирован)"
            self.log(f"Мем мутировал: {name}")
            self.save_to_file()

    def get_meme(self, name: str) -> str:
        return self.memes.get(name, "Мем не найден")

    # --- memory ------------------------------------------------------------
    def remember_goal(self, goal: str) -> None:
        self.memory["goals"].append(goal)
        self.log(f"Цель добавлена: {goal}")
        self.save_to_file()

    def log(self, event: str) -> None:
        timestamp = datetime.now().isoformat()
        self.memory["log"].append({"time": timestamp, "event": event})

    def save_to_file(self) -> None:
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump({"memes": self.memes, "memory": self.memory}, f, indent=2, ensure_ascii=False)

    def load_from_file(self) -> None:
        if Path(self.storage_path).exists():
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.memes = data.get("memes", {})
                self.memory = data.get("memory", self.memory)

    def load_stats(self) -> None:
        if Path(self.stats_path).exists():
            with open(self.stats_path, "r", encoding="utf-8") as f:
                self.strategy_stats = json.load(f)

    def save_stats(self) -> None:
        with open(self.stats_path, "w", encoding="utf-8") as f:
            json.dump(self.strategy_stats, f, indent=2, ensure_ascii=False)

    # --- thinking ----------------------------------------------------------
    def think(self, thought: str) -> Dict:
        self.memory["thoughts"].append(thought)
        self.log(f"Мысль: {thought}")
        self.state["energy"] = max(0, self.state["energy"] - random.randint(1, 3))
        applicable = self.analyze_thought(thought)
        results = []
        for strategy in applicable:
            name = strategy.name
            if name not in self.strategy_stats:
                self.strategy_stats[name] = {"uses": 0, "success": 0, "fail": 0}
            self.strategy_stats[name]["uses"] += 1
            result = {
                "strategy": name,
                "action": strategy.action_plan,
                "energy_cost": strategy.level * 2,
                "triggered_topics": [t for t in strategy.trigger_topics if keyword_in_text(thought, [t])],
            }
            self.log(f"Стратегия применена: {name}")
            success = any(keyword_in_text(thought, [g]) for g in self.memory["goals"])
            if success:
                self.strategy_stats[name]["success"] += 1
            else:
                self.strategy_stats[name]["fail"] += 1
            results.append(result)
        self.save_to_file()
        self.save_stats()
        return {
            "thought": thought,
            "status": "PROCESSED" if results else "NO_STRATEGY_FOUND",
            "actions": results,
            "remaining_energy": self.state["energy"],
        }

    def analyze_thought(self, thought: str) -> List[ThinkingStrategy]:
        applicable: List[ThinkingStrategy] = []

        def check(strategy: ThinkingStrategy) -> None:
            if strategy.is_applicable(self.state["emotion"], self.state["current_goal"], thought, self.state["energy"]):
                applicable.append(strategy)
                for child in strategy.children:
                    check(child)

        for strategy in self.strategy_tree:
            check(strategy)
        return applicable

    def build_strategy_hierarchy(self, strategies: List[ThinkingStrategy]) -> None:
        self.strategy_tree = []
        for s in strategies:
            s.children = []
        by_level: Dict[int, List[ThinkingStrategy]] = {}
        for s in strategies:
            by_level.setdefault(s.level, []).append(s)
        if 1 in by_level:
            self.strategy_tree.extend(by_level[1])
        max_level = max(by_level.keys())
        for lvl in range(2, max_level + 1):
            for strat in by_level.get(lvl, []):
                parent = next((p for p in by_level.get(lvl - 1, []) if p), None)
                if parent:
                    parent.children.append(strat)

    def evolve_strategies(self) -> None:
        mutated: List[ThinkingStrategy] = []
        for name, stats in self.strategy_stats.items():
            if stats["success"] > stats["fail"] and stats["uses"] >= 3:
                original = self.find_strategy_by_name(name)
                if original:
                    new_name = f"{original.name} v{random.randint(2, 99)}"
                    new_keywords = original.activation_conditions.get("keywords", []) + ["рост", "обучение"]
                    mutated_strategy = ThinkingStrategy(
                        name=new_name,
                        level=min(original.level + 1, 5),
                        trigger_topics=original.trigger_topics[:],
                        action_plan=original.action_plan + " (эволюционировавший)",
                        activation_conditions={
                            "emotions": list(original.activation_conditions.get("emotions", [])),
                            "goals": list(original.activation_conditions.get("goals", [])),
                            "keywords": new_keywords,
                        },
                    )
                    mutated.append(mutated_strategy)
                    self.log(f"Создана мутировавшая стратегия: {new_name}")
        if mutated:
            self.build_strategy_hierarchy(self.get_all_strategies() + mutated)
            self.save_to_file()

    def find_strategy_by_name(self, name: str) -> Optional[ThinkingStrategy]:
        for s in self.get_all_strategies():
            if s.name == name:
                return s
        return None

    def get_all_strategies(self) -> List[ThinkingStrategy]:
        result: List[ThinkingStrategy] = []

        def traverse(s: ThinkingStrategy) -> None:
            result.append(s)
            for c in s.children:
                traverse(c)

        for root in self.strategy_tree:
            traverse(root)
        return result

    # -- reflection helpers -------------------------------------------------
    def auto_reflect(self) -> Dict:
        return self.think("[auto] " + random.choice(self.auto_thought_pool))

    def meta_reflect(self) -> Dict:
        return self.think("[meta] " + random.choice(self.meta_thought_pool))

    def mixed_reflection(self) -> Dict:
        if random.random() < 0.7:
            return self.auto_reflect()
        return self.meta_reflect()

    def periodic_thinking(self, interval: int = 10) -> None:
        def run() -> None:
            while self.thinking:
                self.mixed_reflection()
                time.sleep(interval)

        self.thinking = True
        threading.Thread(target=run, daemon=True).start()

    def stop_thinking(self) -> None:
        self.thinking = False
