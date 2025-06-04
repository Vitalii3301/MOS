import random
from typing import Dict, Iterable, List, Optional

from .meme import Meme


class MemeNetwork:
    """Collection of memes with simple evolutionary mechanics."""

    def __init__(self) -> None:
        self.memes: Dict[str, Meme] = {}

    def add_meme(self, meme: Meme) -> None:
        self.memes[str(meme.id)] = meme

    def remove_meme(self, meme_id: str) -> None:
        self.memes.pop(meme_id, None)

    def __iter__(self) -> Iterable[Meme]:
        return iter(self.memes.values())

    def evolve(self) -> None:
        if not self.memes:
            return
        for meme in self.memes.values():
            meme.fitness = random.random()
        sorted_memes: List[Meme] = sorted(self.memes.values(), key=lambda m: m.fitness, reverse=True)
        survivors = sorted_memes[: max(1, len(sorted_memes) // 2)]
        new_generation: List[Meme] = []
        for meme in survivors:
            clone = meme.replicate()
            clone.mutate()
            new_generation.append(clone)
        self.memes = {str(m.id): m for m in survivors + new_generation}

    def get(self, meme_id: str) -> Optional[Meme]:
        return self.memes.get(meme_id)
