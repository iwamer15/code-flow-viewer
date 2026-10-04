"""Python 用の解析器 (標準ライブラリ ast による正確な解析)。"""
from __future__ import annotations

import ast

from .languages import SPECS, analyze_generic, generic_flow

ENTRY_FILE_NAMES = {"main", "app", "run", "manage", "cli", "server", "train", "start", "wsgi", "asgi"}
_MATCH = getattr(ast, "Match", ())   # Python 3.10 未満には match 文が無い
ROUTE_DECORATORS = {"route", "get", "post", "put", "patch", "delete", "websocket", "command", "task",
                    "callback", "on_event", "listener", "slot", "Slot", "event", "group"}


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _call_target(func):
    """呼び出し式から (名前, レシーバ) を取り出す。"""
    if isinstance(func, ast.Name):
        return func.id, ""
    if isinstance(func, ast.Attribute):
        recv = _dotted(func.value)
        return func.attr, recv if recv is not None else "()"
    return None, None


def _ref_target(node):
    """引数として渡された関数参照 (target=worker, self.on_click) を拾う。"""
    if isinstance(node, ast.Name):
        return node.id, ""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
        return node.attr, node.value.id
    return None, None


def _instance_types(nodes, self_only=False) -> dict:
    """x = Klass(...) / self.x = Klass(...) から「変数名 -> クラス名」を推定。"""
    types = {}
    stack = [n for n in nodes if n is not None]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not self_only:
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            fn = node.value.func
            cname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
            if cname and cname[:1].isupper():
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    key = _dotted(t)
                    if key and (not self_only or key.startswith("self.")):
                        types[key] = cname
        stack.extend(ast.iter_child_nodes(node))
    return types


def _apply_types(calls, types):
    if types:
        for c in calls:
            if c[1] in types:
                c[1] = types[c[1]]
    return calls


def _collect_calls(nodes) -> list:
    out = []
    stack = [n for n in nodes if n is not None]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            name, recv = _call_target(node.func)
            if name:
                out.append((node.lineno, node.col_offset, [name, recv, node.lineno, 0]))
            for arg in list(node.args) + [k.value for k in node.keywords]:
                rn, rr = _ref_target(arg)
                if rn:
                    # 4番目の要素 2 = 「参照渡し」(コールバック登録など)
                    out.append((arg.lineno, arg.col_offset, [rn, rr, arg.lineno, 2]))
        stack.extend(ast.iter_child_nodes(node))
    out.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in out]


def _is_main_guard(st) -> bool:
    if not isinstance(st, ast.If):
        return False
    t = st.test
    if isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq):
        sides = [t.left] + list(t.comparators)
        has_name = any(isinstance(x, ast.Name) and x.id == "__name__" for x in sides)
        has_main = any(isinstance(x, ast.Constant) and x.value == "__main__" for x in sides)
        return has_name and has_main
    return False


def module_name(rel: str) -> str:
    p = rel[:-3] if rel.endswith(".py") else rel.rsplit(".", 1)[0]
    parts = p.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def analyze_python(rel: str, src: str) -> dict:
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError) as e:
        res = analyze_generic(rel, src, SPECS["python"])
        res["error"] = f"構文エラーのため簡易解析: {e}"
        return res

    symbols = []
    hints = []
    nlines = src.count("\n") + 1

    def add_def(node, cls, prefix, kind):
        qual = f"{prefix}.{node.name}" if prefix else node.name
        end = getattr(node, "end_lineno", node.lineno)
        start = node.lineno
        if node.decorator_list:
            for d in node.decorator_list:
                dn = d.func if isinstance(d, ast.Call) else d
                name = dn.attr if isinstance(dn, ast.Attribute) else (dn.id if isinstance(dn, ast.Name) else "")
                if name in ROUTE_DECORATORS:
                    hints.append((qual, start, 35, f"@{_dotted(dn) or name} で登録されるハンドラ"))
        sym = {"name": node.name, "qual": qual, "kind": kind, "cls": cls, "start": start, "end": end,
               "b": None, "calls": []}
        symbols.append(sym)
        return sym, qual

    def visit(stmts, prefix, cls, in_def, cls_types=None):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym, qual = add_def(st, cls, prefix, "method" if cls else "function")
                calls = _collect_calls(st.body + st.decorator_list
                                       + list(st.args.defaults) + list(st.args.kw_defaults or []))
                types = dict(cls_types or {})
                types.update(_instance_types(st.body))
                sym["calls"] = _apply_types(calls, types)
                visit(st.body, qual, None, True)
            elif isinstance(st, ast.ClassDef):
                _sym, qual = add_def(st, None, prefix, "class")
                visit(st.body, qual, st.name, in_def, _instance_types(st.body, self_only=True))
            else:
                # if/try/with の中で定義される関数も拾う
                for field in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list):
                        inner = []
                        for x in sub:
                            if isinstance(x, ast.ExceptHandler):
                                inner.extend(x.body)
                            else:
                                inner.append(x)
                        visit(inner, prefix, cls, in_def)
                if isinstance(st, _MATCH):
                    for case in st.cases:
                        visit(case.body, prefix, cls, in_def)

    visit(tree.body, "", None, False)

    # モジュール直下のコード / if __name__ == "__main__":
    top_stmts = []
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        if _is_main_guard(st):
            sym = {"name": "__main__", "qual": "__main__", "kind": "toplevel", "cls": None,
                   "start": st.lineno, "end": getattr(st, "end_lineno", st.lineno), "b": None,
                   "calls": _apply_types(_collect_calls(st.body), _instance_types(st.body))}
            symbols.append(sym)
            hints.append(("__main__", st.lineno, 100, 'if __name__ == "__main__":'))
            continue
        top_stmts.append(st)
    top_calls = _apply_types(_collect_calls(top_stmts), _instance_types(top_stmts))
    stem = rel.rsplit("/", 1)[-1][:-3] if rel.endswith(".py") else ""
    if top_calls or stem == "__main__":
        symbols.append({"name": "<module>", "qual": "<module>", "kind": "toplevel", "cls": None,
                        "start": 1, "end": nlines, "b": None, "calls": top_calls})
        if stem == "__main__":
            hints.append(("<module>", 1, 90, "__main__.py (python -m で実行)"))
        elif stem in ENTRY_FILE_NAMES:
            hints.append(("<module>", 1, 55, f"{stem}.py のトップレベル処理"))
        elif any(c[3] != 2 for c in top_calls):
            hints.append(("<module>", 1, 12, "トップレベルで処理を実行"))

    # import
    imports = []
    pkg = module_name(rel).split(".")
    is_init = rel.endswith("__init__.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    imports.append({"mod": a.name, "line": node.lineno, "kind": "module", "local": a.asname, "orig": None, "mode": "py"})
                else:
                    imports.append({"mod": a.name, "line": node.lineno, "kind": "module", "local": a.name, "orig": None, "mode": "py"})
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg if is_init else pkg[:-1]
                up = node.level - 1
                base = base[: len(base) - up] if up else base
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            for a in node.names:
                if a.name == "*":
                    imports.append({"mod": mod, "line": node.lineno, "kind": "module", "local": None, "orig": None, "mode": "py"})
                    continue
                imports.append({"mod": mod, "line": node.lineno, "kind": "from", "local": a.asname or a.name,
                                "orig": a.name, "mode": "py"})
    return {"lang": "python", "symbols": symbols, "imports": imports, "hints": hints}


# ---------------------------------------------------------------------------
# 関数内フロー
# ---------------------------------------------------------------------------

def _src(node, n=90) -> str:
    try:
        t = ast.unparse(node)
    except Exception:  # noqa: BLE001
        t = type(node).__name__
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _names(node) -> list:
    out = []
    for c in _collect_calls([node]):
        if c[3] != 2 and c[0] not in out:
            out.append(c[0])
    return out


def _flow(stmts) -> list:
    items = []
    for st in stmts:
        line = st.lineno
        if isinstance(st, ast.If):
            items.append({"t": "if", "x": _src(st.test), "l": line, "c": _names(st.test),
                          "a": _flow(st.body), "b": _flow(st.orelse)})
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            items.append({"t": "loop", "x": f"for {_src(st.target, 30)} in {_src(st.iter, 50)}", "l": line,
                          "c": _names(st.iter), "a": _flow(st.body)})
        elif isinstance(st, ast.While):
            items.append({"t": "loop", "x": f"while {_src(st.test)}", "l": line, "c": _names(st.test), "a": _flow(st.body)})
        elif isinstance(st, ast.Try) or type(st).__name__ == "TryStar":
            items.append({"t": "try", "x": "try", "l": line, "c": [], "a": _flow(st.body),
                          "h": [{"x": "except " + (_src(h.type, 40) if h.type else ""), "a": _flow(h.body)} for h in st.handlers],
                          "f": _flow(st.finalbody), "e": _flow(st.orelse)})
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            head = "with " + ", ".join(_src(i.context_expr, 40) for i in st.items)
            items.append({"t": "stmt", "x": head, "l": line,
                          "c": [n for i in st.items for n in _names(i.context_expr)]})
            items.extend(_flow(st.body))
        elif isinstance(st, _MATCH):
            items.append({"t": "switch", "x": "match " + _src(st.subject), "l": line, "c": _names(st.subject),
                          "cases": [{"x": _src(c.pattern, 40), "a": _flow(c.body)} for c in st.cases]})
        elif isinstance(st, (ast.Return, ast.Raise)):
            items.append({"t": "return", "x": _src(st), "l": line, "c": _names(st)})
        elif isinstance(st, ast.Break):
            items.append({"t": "break", "x": "break", "l": line})
        elif isinstance(st, ast.Continue):
            items.append({"t": "continue", "x": "continue", "l": line})
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            items.append({"t": "stmt", "x": f"定義: {st.name}", "l": line, "c": [], "def": 1})
        elif isinstance(st, (ast.Import, ast.ImportFrom, ast.Pass)) or (
                isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)):
            continue   # import / pass / docstring は省略
        else:
            items.append({"t": "stmt", "x": _src(st), "l": line, "c": _names(st)})
    return items


def python_flow(src: str, start_line: int) -> list:
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return generic_flow(src, SPECS["python"], start_line)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno == start_line:
            return _flow(node.body)
        if isinstance(node, ast.If) and node.lineno == start_line and _is_main_guard(node):
            return _flow(node.body)
    # トップレベル
    body = [st for st in tree.body if not _is_main_guard(st)]
    return _flow(body)
