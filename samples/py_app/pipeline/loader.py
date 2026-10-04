import os


def _list_files(root):
    return [f for f in os.listdir(root) if f.endswith(".csv")]


def load_dataset(root):
    try:
        files = _list_files(root)
    except FileNotFoundError:
        return []
    rows = []
    for f in files:
        rows.extend(read_csv(os.path.join(root, f)))
    return rows


def read_csv(path):
    with open(path) as f:
        return f.read().splitlines()
