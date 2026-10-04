"""サンプル: 学習パイプライン"""
import argparse
import threading
from pipeline import loader
from pipeline.preprocess import normalize, Augmenter
from pipeline.model import Trainer


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=3)
    return ap.parse_args()


def worker(trainer):
    trainer.report()


def main():
    args = parse_args()
    data = loader.load_dataset("data/")
    if not data:
        print("no data")
        return 1
    data = normalize(data)
    aug = Augmenter(seed=1)
    data = aug.apply(data)
    trainer = Trainer(epochs=args.epochs)
    for epoch in range(args.epochs):
        loss = trainer.step(data)
        if loss < 0.1:
            break
    t = threading.Thread(target=worker, args=(trainer,))
    t.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
