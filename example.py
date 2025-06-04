from mos.meme import Meme
from mos.network import MemeNetwork


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
