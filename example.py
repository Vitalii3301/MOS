from mos.meme import Meme
from mos.network import MemeNetwork
from mos.agent import UnifiedMemeticAgent, ThinkingStrategy


def sample_function(env):
    return env


if __name__ == "__main__":
    net = MemeNetwork()
    meme1 = Meme(sample_function, content_type="code")
    meme2 = Meme("hello world", content_type="text")
    net.add_meme(meme1)
    net.add_meme(meme2)

    print("Initial memes:", [m.to_dict() for m in net])
    net.evolve()
    print("After evolution:", [m.to_dict() for m in net])

    # demonstrate UnifiedMemeticAgent with a simple strategy
    meta_strategy = ThinkingStrategy(
        name="Самоанализ",
        level=2,
        trigger_topics=["я", "стратегия", "эмоция", "цель", "мысль", "ошибка"],
        action_plan="Провести самонаблюдение и оценить эффективность своих действий",
        activation_conditions={
            "emotions": ["тревога", "спокойствие", "усталость"],
            "goals": ["улучшение мышления", "самоанализ", "устранение противоречий"],
            "keywords": ["почему", "как", "что", "чувствую", "эффектив"],
        },
    )

    strategies = [
        ThinkingStrategy(
            name="Базовый анализ",
            level=1,
            trigger_topics=[],
            action_plan="Первичный анализ содержания",
            activation_conditions={"emotions": ["тревога", "спокойствие"]},
        ),
        ThinkingStrategy(
            name="Поиск противоречий",
            level=2,
            trigger_topics=["противореч"],
            action_plan="Выявление логических несоответствий",
            activation_conditions={
                "keywords": ["противореч", "конфликт"],
                "goals": ["устранение противоречий"],
            },
        ),
        meta_strategy,
    ]

    agent = UnifiedMemeticAgent()
    agent.build_strategy_hierarchy(strategies)
    agent.remember_goal("устранение противоречий")
    response = agent.think("Почему возникает ошибка в стратегии?")
    print("Agent response:", response)
