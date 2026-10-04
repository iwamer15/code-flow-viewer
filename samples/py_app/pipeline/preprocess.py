from .loader import read_csv


def normalize(rows):
    return [r.strip().lower() for r in rows]


class Augmenter:
    def __init__(self, seed=0):
        self.seed = seed
        self._init_rng()

    def _init_rng(self):
        self.rng = self.seed * 31

    def apply(self, rows):
        out = []
        for r in rows:
            match self.rng % 3:
                case 0:
                    out.append(self.flip(r))
                case _:
                    out.append(r)
        return out

    def flip(self, r):
        return r[::-1]
