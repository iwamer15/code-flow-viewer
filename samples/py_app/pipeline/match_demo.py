"""match 文 (Python 3.10+) のフローチャート表示の例。main.py からは使わない (3.9 でも実行できるように)。"""


def describe(cmd):
    match cmd.split():
        case ["go", direction]:
            return f"move {direction}"
        case ["stop"]:
            return "stop"
        case _:
            return "unknown"
