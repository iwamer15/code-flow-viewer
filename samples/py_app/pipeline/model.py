import time


class Trainer:
    def __init__(self, epochs):
        self.epochs = epochs
        self.history = []

    def step(self, data):
        loss = self._forward(data)
        self.history.append(loss)
        return loss

    def _forward(self, data):
        time.sleep(0.2)  # 学習処理の代わり (実行パネルで進み具合が見えるように)
        return 1.0 / (len(self.history) + 1)

    def report(self):
        print(self.history)
