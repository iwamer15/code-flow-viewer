"""言語定義と、Python 以外の言語向けの汎用(正規表現+括弧対応)解析器。

方針:
  * 構文木を作らず「コメント/文字列を空白化したテキスト」に対して
    定義(関数・メソッド・クラス)と呼び出しを正規表現で拾う。高速で依存ライブラリ不要。
  * 空白化しても文字数・改行位置は元と同じなので、位置→行番号の変換がそのまま使える。
  * 括弧({}, (), [])の対応表をファイルごとに一度だけ作り、本体範囲の特定や
    関数内フロー(if/for/switch/try…)の解析に使い回す。
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 言語仕様
# ---------------------------------------------------------------------------


@dataclass
class Spec:
    name: str
    exts: tuple
    monaco: str
    line_comments: tuple = ("//",)
    block_comments: tuple = (("/*", "*/"),)
    strings: tuple = ('"', "'")
    char_quote: bool = False      # ' は1文字リテラルのみ (C/Java/Rust...)
    hash_needs_ws: bool = False   # '#' は語頭のみコメント (shell の $# 対策)
    body: str = "brace"           # brace | indent | end
    flow: str = "brace"           # brace | lines  (関数内フローチャートの作り方)
    def_patterns: tuple = ()      # (regex, kind)  kind: def | arrow
    cfunc: bool = False           # 「型 名前(...) {」形式の定義を推定する
    js_like: bool = False
    class_patterns: tuple = ()
    import_patterns: tuple = ()   # (regex, mode)  mode: path | dotted
    script_toplevel: bool = False  # 関数外のトップレベルコードを「スクリプト本体」とみなす
    main_names: tuple = ()
    ctor: str | None = None       # コンストラクタ名 ("@class" はクラス名と同じ)
    expr_eq: bool = False         # 「fun f() = 式」の式本体を許す
    extra_calls: tuple = ()       # 括弧なし呼び出しの拾い方 (shell, ruby など)


def _c(p, flags=re.M):
    return re.compile(p, flags)


ID = r"[A-Za-z_$][\w$]*"
ARROW_PARAMS = r"(?:\((?:[^()]|\([^()]*\))*\)|" + ID + r")"

JS_DEFS = (
    (_c(r"\bfunction\b\s*\*?\s*(?P<name>" + ID + r")\s*(?:<[^>()]*>)?\s*\("), "def"),
    (_c(r"\b(?:const|let|var)\s+(?P<name>" + ID + r")\s*(?::[^=;\n]+)?=\s*(?:async\s+)?function\b"), "def"),
    (_c(r"\b(?:const|let|var)\s+(?P<name>" + ID + r")\s*(?::[^=;\n]+)?=\s*(?:async\s+)?(?:<[^>()]*>)?"
        + ARROW_PARAMS + r"\s*(?::\s*[^=;{]+?)?\s*=>"), "arrow"),
    # クラスフィールド / オブジェクトプロパティのアロー関数
    (_c(r"^[ \t]*(?:(?:public|private|protected|static|readonly|override)\s+)*(?P<name>" + ID
        + r")\s*(?::[^=;\n]+)?=\s*(?:async\s+)?" + ARROW_PARAMS + r"\s*(?::\s*[^=;{]+?)?\s*=>"), "arrow"),
    (_c(r"^[ \t]*(?P<name>" + ID + r")\s*:\s*(?:async\s+)?" + ARROW_PARAMS + r"\s*=>"), "arrow"),
    (_c(r"^[ \t]*(?P<name>" + ID + r")\s*:\s*(?:async\s+)?function\b"), "def"),
    # exports.foo = function / module.exports.foo = () => / this.foo = function
    (_c(r"\b(?:" + ID + r"\.)+(?P<name>" + ID + r")\s*=\s*(?:async\s+)?function\b"), "def"),
    (_c(r"\b(?:" + ID + r"\.)+(?P<name>" + ID + r")\s*=\s*(?:async\s+)?" + ARROW_PARAMS + r"\s*=>"), "arrow"),
)
JS_IMPORTS = (
    (_c(r"\bimport\s+(?P<clause>[^;'\"`]*?)\s*from\s*['\"](?P<mod>[^'\"]+)['\"]"), "js"),
    (_c(r"\bimport\s*['\"](?P<mod>[^'\"]+)['\"]"), "path"),
    (_c(r"\bexport\s+[^;'\"]*?\s+from\s*['\"](?P<mod>[^'\"]+)['\"]"), "path"),
    (_c(r"(?:\b(?:const|let|var)\s+(?P<clause>\{[^}]*\}|" + ID + r")\s*=\s*)?\brequire\s*\(\s*['\"](?P<mod>[^'\"]+)['\"]\s*\)"), "js"),
    (_c(r"\bimport\s*\(\s*['\"](?P<mod>[^'\"]+)['\"]\s*\)"), "path"),
)

SPECS: dict[str, Spec] = {}


def _add(spec: Spec):
    SPECS[spec.name] = spec


_add(Spec("javascript", (".js", ".mjs", ".cjs", ".jsx"), "javascript",
          strings=('"', "'", "`"), def_patterns=JS_DEFS, cfunc=True, js_like=True,
          class_patterns=(_c(r"\bclass\s+(?P<name>" + ID + r")"),),
          import_patterns=JS_IMPORTS, script_toplevel=True, ctor="constructor"))
_add(Spec("typescript", (".ts", ".tsx", ".mts", ".cts"), "typescript",
          strings=('"', "'", "`"), def_patterns=JS_DEFS, cfunc=True, js_like=True,
          class_patterns=(_c(r"\b(?:class|interface)\s+(?P<name>" + ID + r")"),),
          import_patterns=JS_IMPORTS, script_toplevel=True, ctor="constructor"))
_add(Spec("java", (".java",), "java", char_quote=True, cfunc=True,
          class_patterns=(_c(r"\b(?:class|interface|enum|record)\s+(?P<name>\w+)"),),
          import_patterns=((_c(r"^\s*import\s+(?:static\s+)?(?P<mod>[\w.]+(?:\.\*)?)\s*;"), "dotted"),),
          main_names=("main",), ctor="@class"))
_add(Spec("kotlin", (".kt", ".kts"), "kotlin", strings=('"""', '"'), char_quote=True, expr_eq=True,
          def_patterns=((_c(r"\bfun\s+(?:<[^>]*>\s*)?(?:(?P<recv>[\w.]+)\.)?(?P<name>\w+)\s*\("), "def"),),
          class_patterns=(_c(r"\b(?:class|interface|object)\s+(?P<name>\w+)"),),
          import_patterns=((_c(r"^\s*import\s+(?P<mod>[\w.]+(?:\.\*)?)"), "dotted"),),
          main_names=("main",), ctor="init"))
_add(Spec("scala", (".scala", ".sc"), "scala", strings=('"""', '"'), char_quote=True, expr_eq=True,
          def_patterns=((_c(r"\bdef\s+(?P<name>\w+)"), "def"),),
          class_patterns=(_c(r"\b(?:class|object|trait)\s+(?P<name>\w+)"),),
          import_patterns=((_c(r"^\s*import\s+(?P<mod>[\w.]+)"), "dotted"),),
          main_names=("main",)))
_add(Spec("c", (".c", ".h"), "c", char_quote=True, cfunc=True,
          import_patterns=((_c(r"^\s*#\s*include\s*\"(?P<mod>[^\"]+)\""), "path"),),
          main_names=("main",)))
_add(Spec("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".ino"), "cpp", char_quote=True, cfunc=True,
          class_patterns=(_c(r"\b(?:class|struct)\s+(?P<name>\w+)(?:\s+final)?\s*(?::[^{;]*)?(?=\{)"),),
          import_patterns=((_c(r"^\s*#\s*include\s*\"(?P<mod>[^\"]+)\""), "path"),),
          main_names=("main", "setup", "loop"), ctor="@class"))
_add(Spec("csharp", (".cs",), "csharp", char_quote=True, cfunc=True,
          class_patterns=(_c(r"\b(?:class|interface|struct|record)\s+(?P<name>\w+)"),),
          import_patterns=((_c(r"^\s*using\s+(?:static\s+)?(?P<mod>[\w.]+)\s*;"), "dotted"),),
          main_names=("Main",), ctor="@class"))
_add(Spec("go", (".go",), "go", strings=('"', "`"), char_quote=True,
          def_patterns=((_c(r"\bfunc\s+(?:\(\s*\w*\s*\*?\s*(?P<recv>\w+)(?:\[[^\]]*\])?\s*\)\s*)?(?P<name>\w+)\s*(?:\[[^\]]*\])?\s*\("), "def"),),
          import_patterns=((_c(r"^\s*import\s+(?:\w+\s+)?\"(?P<mod>[^\"]+)\""), "gopkg"),
                           (_c(r"^\s*import\s*\((?P<block>[^)]*)\)"), "goblock")),
          main_names=("main",)))
_add(Spec("rust", (".rs",), "rust", char_quote=True,
          def_patterns=((_c(r"\bfn\s+(?P<name>\w+)"), "def"),),
          class_patterns=(_c(r"\bimpl(?:\s*<[^{]*?>)?\s+(?:[\w:<>, ]+?\s+for\s+)?(?P<name>[A-Za-z_]\w*)"),
                          _c(r"\btrait\s+(?P<name>\w+)")),
          import_patterns=((_c(r"^\s*(?:pub\s+)?use\s+(?P<mod>[\w:]+)"), "rust"),
                           (_c(r"^\s*(?:pub\s+)?mod\s+(?P<mod>\w+)\s*;"), "rustmod")),
          main_names=("main",), ctor="new"))
_add(Spec("swift", (".swift",), "swift", strings=('"""', '"'),
          def_patterns=((_c(r"\bfunc\s+(?P<name>\w+)"), "def"), (_c(r"\b(?P<name>init)\s*[?!]?\s*\("), "def")),
          class_patterns=(_c(r"\b(?:class|struct|enum|protocol|extension|actor)\s+(?P<name>\w+)"),),
          import_patterns=((_c(r"^\s*import\s+(?P<mod>\w+)"), "dotted"),),
          script_toplevel=True, main_names=("main",), ctor="init"))
_add(Spec("php", (".php",), "php", line_comments=("//", "#"),
          def_patterns=((_c(r"\bfunction\s+&?\s*(?P<name>\w+)\s*\("), "def"),),
          class_patterns=(_c(r"\b(?:class|interface|trait)\s+(?P<name>\w+)"),),
          import_patterns=((_c(r"\b(?:require|include)(?:_once)?\s*\(?\s*(?:__DIR__\s*\.\s*)?['\"](?P<mod>[^'\"]+)['\"]"), "path"),
                           (_c(r"^\s*use\s+(?P<mod>[\w\\]+)"), "dotted")),
          script_toplevel=True, ctor="__construct"))
_add(Spec("dart", (".dart",), "dart", strings=("'''", '"""', '"', "'"), cfunc=True,
          class_patterns=(_c(r"\b(?:class|mixin|extension)\s+(?P<name>\w+)"),),
          import_patterns=((_c(r"^\s*import\s+['\"](?P<mod>[^'\"]+)['\"]"), "path"),),
          main_names=("main",), ctor="@class"))
_add(Spec("r", (".r", ".R"), "r", line_comments=("#",), block_comments=(),
          def_patterns=((_c(r"(?P<name>[A-Za-z_.][\w.]*)\s*(?:<-|=)\s*function\s*\("), "def"),),
          import_patterns=((_c(r"\bsource\(\s*['\"](?P<mod>[^'\"]+)['\"]"), "path"),),
          script_toplevel=True))
_add(Spec("powershell", (".ps1", ".psm1"), "powershell", line_comments=("#",), block_comments=(("<#", "#>"),),
          def_patterns=((_c(r"\bfunction\s+(?P<name>[\w-]+)"), "def"),),
          import_patterns=((_c(r"^\s*\.\s+(?P<mod>\S+\.ps1)"), "path"),),
          script_toplevel=True, extra_calls=(_c(r"(?<![\w-])([A-Z][a-z]+-[A-Za-z]+)\b"),)))
_add(Spec("ruby", (".rb", ".rake"), "ruby", line_comments=("#",), block_comments=(("=begin", "=end"),),
          strings=('"', "'", "`"), body="end", flow="lines",
          def_patterns=((_c(r"^[ \t]*def\s+(?:self\.)?(?P<name>[A-Za-z_]\w*[?!=]?)"), "def"),),
          class_patterns=(_c(r"^[ \t]*(?:class|module)\s+(?P<name>[A-Z][\w:]*)"),),
          import_patterns=((_c(r"\brequire(?:_relative)?\s*\(?\s*['\"](?P<mod>[^'\"]+)['\"]"), "path"),),
          script_toplevel=True, ctor="initialize",
          extra_calls=(_c(r"^[ \t]*([a-z_]\w*[?!]?)(?=[ \t]+[\w'\":@\[]|[ \t]*$)"),)))
_add(Spec("lua", (".lua",), "lua", line_comments=("--",), block_comments=(("--[[", "]]"),),
          strings=('"', "'"), body="end", flow="lines",
          def_patterns=((_c(r"\bfunction\s+(?P<name>[\w.:]+)\s*\("), "def"),
                        (_c(r"\b(?:local\s+)?(?P<name>[\w.]+)\s*=\s*function\s*\("), "def")),
          import_patterns=((_c(r"\brequire\s*\(?\s*['\"](?P<mod>[^'\"]+)['\"]"), "dotted"),),
          script_toplevel=True))
_add(Spec("shell", (".sh", ".bash", ".zsh"), "shell", line_comments=("#",), block_comments=(),
          hash_needs_ws=True, flow="lines",
          def_patterns=((_c(r"^[ \t]*(?:function\s+)?(?P<name>[A-Za-z_][\w-]*)\s*\(\s*\)"), "def"),
                        (_c(r"^[ \t]*function\s+(?P<name>[A-Za-z_][\w-]*)\s*(?=\{)"), "def")),
          import_patterns=((_c(r"^[ \t]*(?:source|\.)\s+['\"]?(?P<mod>[^\s;'\"]+)"), "path"),),
          script_toplevel=True,
          extra_calls=(_c(r"(?:^|[;&|(`]|\$\(|\bthen\b|\bdo\b|\belse\b)[ \t]*([A-Za-z_][\w-]*)(?=[ \t]|$|;)"),)))
# 構文エラーで ast が使えない Python ファイル用の予備解析
_add(Spec("python", (".py", ".pyw"), "python", line_comments=("#",), block_comments=(),
          strings=('"""', "'''", '"', "'"), body="indent", flow="lines",
          def_patterns=((_c(r"^[ \t]*(?:async\s+)?def\s+(?P<name>\w+)"), "def"),),
          class_patterns=(_c(r"^[ \t]*class\s+(?P<name>\w+)"),),
          script_toplevel=True, ctor="__init__"))

EXT_MAP = {}
for _s in SPECS.values():
    for _e in _s.exts:
        EXT_MAP[_e] = _s.name
        EXT_MAP[_e.lower()] = _s.name

# Monaco でハイライトだけしたい (解析対象外) 拡張子
VIEW_ONLY = {
    ".json": "json", ".md": "markdown", ".yml": "yaml", ".yaml": "yaml", ".toml": "ini", ".ini": "ini",
    ".cfg": "ini", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".xml": "xml",
    ".sql": "sql", ".txt": "plaintext", ".bat": "bat", ".cmd": "bat", ".vue": "html", ".svelte": "html",
    ".dockerfile": "dockerfile", ".env": "plaintext", ".csv": "plaintext",
}
SPECIAL_NAMES = {"Dockerfile": "dockerfile", "Makefile": "plaintext", "requirements.txt": "plaintext"}


def monaco_lang(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in SPECIAL_NAMES:
        return SPECIAL_NAMES[name]
    dot = name.rfind(".")
    ext = name[dot:] if dot >= 0 else ""
    if ext in EXT_MAP:
        return SPECS[EXT_MAP[ext]].monaco
    return VIEW_ONLY.get(ext.lower(), "plaintext")


KEYWORDS = frozenset("""
if for while switch catch return sizeof typeof function elif else do try with assert not and or in is
lambda yield await new delete throw case foreach using lock fixed when match loop fn func def fun
synchronized until unless super this self alignof decltype noexcept static_assert require require_once
include include_once import export from as var let const elsif begin end then fi done esac local
defined instanceof void constructor catch_unwind guard repeat
""".split())

SHELL_WORDS = frozenset("""if then else elif fi for do done while until case esac in function return local
export declare readonly set shift exit break continue true false test select time""".split())

ACCESSORS = frozenset({"this", "self", "cls", "Self", "$this", "@", "super"})

# ---------------------------------------------------------------------------
# 前処理: コメント・文字列の空白化
# ---------------------------------------------------------------------------

_NONNL = re.compile(r"[^\n]")
_CHAR_RE = re.compile(r"'(?:\\.[^'\n]{0,8}|[^\\'\n])'")
_start_cache: dict[str, re.Pattern] = {}


def _blank(seg: str) -> str:
    return _NONNL.sub(" ", seg)


def strip_source(src: str, spec: Spec) -> tuple[str, str]:
    """(clean, nocomment) を返す。clean は文字列の中身も空白化。どちらも src と同じ長さ。"""
    pat = _start_cache.get(spec.name)
    if pat is None:
        toks = [a for a, _ in spec.block_comments] + list(spec.line_comments) + list(spec.strings)
        toks.sort(key=len, reverse=True)
        pat = re.compile("|".join(re.escape(t) for t in toks)) if toks else re.compile(r"(?!x)x")
        _start_cache[spec.name] = pat
    clean: list[str] = []
    nocom: list[str] = []
    i = 0
    n = len(src)
    block = dict(spec.block_comments)
    while i < n:
        m = pat.search(src, i)
        if not m:
            break
        p = m.start()
        t = m.group()
        if t in block:
            j = src.find(block[t], p + len(t))
            j = n if j < 0 else j + len(block[t])
            seg = src[i:p]
            clean.append(seg); nocom.append(seg)
            b = _blank(src[p:j])
            clean.append(b); nocom.append(b)
            i = j
            continue
        if t in spec.line_comments:
            if spec.hash_needs_ws and t == "#" and p > 0 and src[p - 1] not in " \t\n;":
                seg = src[i:p + 1]
                clean.append(seg); nocom.append(seg)
                i = p + 1
                continue
            j = src.find("\n", p)
            j = n if j < 0 else j
            seg = src[i:p]
            clean.append(seg); nocom.append(seg)
            b = _blank(src[p:j])
            clean.append(b); nocom.append(b)
            i = j
            continue
        # 文字列
        if t == "'" and spec.char_quote:
            cm = _CHAR_RE.match(src, p)
            if not cm:   # Rust のライフタイム 'a など
                seg = src[i:p + 1]
                clean.append(seg); nocom.append(seg)
                i = p + 1
                continue
            j = cm.end()
        else:
            j = p + len(t)
            multiline = len(t) == 3 or t == "`"
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if src.startswith(t, j):
                    j += len(t)
                    break
                if ch == "\n" and not multiline:
                    break
                j += 1
            j = min(j, n)
        seg = src[i:p]
        clean.append(seg); nocom.append(seg)
        body = src[p:j]
        q = 1 if (t == "'" and spec.char_quote) else len(t)
        closed = len(body) >= 2 * q and body.endswith(t if q == len(t) else "'")
        inner_end = len(body) - q if closed else len(body)
        clean.append(body[:q] + _blank(body[q:inner_end]) + body[inner_end:])
        nocom.append(body)
        i = j
    clean.append(src[i:]); nocom.append(src[i:])
    c = "".join(clean)
    nc = "".join(nocom)
    if len(c) != n:  # 念のため (想定外ケースで位置がずれないよう)
        c = c[:n].ljust(n)
    return c, nc


_BR = re.compile(r"[{}()\[\]]")
_PAIR = {"}": "{", ")": "(", "]": "["}


def build_match(s: str) -> dict[int, int]:
    """括弧の対応表 (開き位置 -> 閉じ位置、閉じ位置 -> 開き位置)。"""
    match: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    for m in _BR.finditer(s):
        ch = m.group()
        p = m.start()
        if ch in "{([":
            stack.append((ch, p))
        else:
            want = _PAIR[ch]
            # 対応の取れない閉じ括弧は読み飛ばす (壊れたコードへの耐性)
            k = len(stack) - 1
            while k >= 0 and stack[k][0] != want and len(stack) - k < 4:
                k -= 1
            if k >= 0 and stack[k][0] == want:
                op = stack[k][1]
                del stack[k:]
                match[op] = p
                match[p] = op
    n = len(s)
    for _, op in stack:
        match[op] = n - 1
    return match


class LineIndex:
    def __init__(self, s: str):
        self.starts = [0] + [m.end() for m in re.finditer("\n", s)]

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)

    def start_of(self, line: int) -> int:
        return self.starts[max(0, min(line - 1, len(self.starts) - 1))]


_WS = re.compile(r"\s*")
_BODY_SCAN = re.compile(r"=>|==|!=|<=|>=|[{;()\[\]=]")
_STMT_SCAN = re.compile(r"[;\n({\[)}\]]")
_CONT_END = set(",([{+-*/%=&|?:.<>!^~\\")


def skip_ws(s: str, i: int) -> int:
    return _WS.match(s, i).end()


def stmt_end(s: str, i: int, end: int, match: dict) -> int:
    """i から始まる文の終わり (; か、続きの無い改行か、外側の閉じ括弧) の位置。"""
    j = i
    while j < end:
        m = _STMT_SCAN.search(s, j, end)
        if not m:
            return end
        p = m.start()
        ch = m.group()
        if ch in "({[":
            j = match.get(p, end) + 1
            continue
        if ch in ")}]":
            return p
        if ch == ";":
            return p
        # 改行: 行末が演算子なら継続、次の行が . ? : で始まれば継続
        k = p - 1
        while k >= i and s[k] in " \t\r":
            k -= 1
        if k < i:
            j = p + 1
            i2 = skip_ws(s, p + 1)
            if i2 >= end:
                return end
            continue
        nxt = skip_ws(s, p + 1)
        if s[k] in _CONT_END or (nxt < end and s[nxt] in ".?:") or s.startswith("=>", nxt):
            j = p + 1
            continue
        return p
    return end


def find_body(s: str, pos: int, match: dict, spec: Spec, limit: int = 3000):
    """定義の後ろから本体 {…} を探す。(開始, 終了) か None(宣言のみ)。"""
    n = len(s)
    i = pos
    end = min(n, pos + limit)
    while i < end:
        m = _BODY_SCAN.search(s, i, end)
        if not m:
            return None
        t = m.group()
        p = m.start()
        if t in ("(", "["):
            i = match.get(p, n - 1) + 1
            continue
        if t == "{":
            return p, match.get(p, n - 1)
        if t == ";":
            return None
        if t in ("=>", "=") and (t == "=>" or spec.expr_eq):
            k = skip_ws(s, p + len(t))
            if k < n and s[k] == "{":
                return k, match.get(k, n - 1)
            return k, stmt_end(s, k, n, match)
        if t == "=" and spec.name in ("cpp", "c", "java", "csharp"):
            return None  # = default; = 0; など
        i = p + len(t)
    return None


def indent_body(src_lines: list[str], li: int, style: str) -> int:
    """インデント基準で本体の最終行 (0始まり) を返す。"""
    def ind(line):
        return len(line) - len(line.lstrip(" \t"))
    base = ind(src_lines[li])
    last = li
    for j in range(li + 1, len(src_lines)):
        line = src_lines[j]
        if not line.strip():
            continue
        if ind(line) <= base:
            if style == "end" and line.strip().startswith(("end", "}")):
                last = j
            break
        last = j
    return last


# ---------------------------------------------------------------------------
# 汎用解析
# ---------------------------------------------------------------------------

CALL_RE = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*[!?]?)\s*\(")
_CFUNC_RE = re.compile(r"(?<![\w$.])([A-Za-z_~$][\w$]*(?:::~?[A-Za-z_]\w*)*)\s*\(")
_CFUNC_BAD_PREFIX = re.compile(r"[=(.,!+\-/%|^?]|\b(?:return|new|else|throw|await|yield|case|delete|typeof|goto|in|of|echo|print)\b")
_JS_MODIFIERS = frozenset("async static get set public private protected override readonly abstract".split())
_CFUNC_TAIL = re.compile(r"^\s*(?:const|noexcept|override|final|volatile|&|&&|mutable|throws\s+[\w.,\s]+|->\s*[^{;]+|:\s*[^{;]+|\s)*$")


def _recv_before(s: str, p: int) -> str:
    j = p - 1
    while j >= 0 and s[j] in " \t":
        j -= 1
    if j < 0:
        return ""
    if s[j] == ".":
        j -= 1
        if j >= 0 and s[j] == "?":
            j -= 1
    elif s[j] in ">:" and j > 0 and s[j - 1] in "-:":
        j -= 2
    else:
        return ""
    while j >= 0 and s[j] in " \t\n":
        j -= 1
    if j < 0:
        return ""
    if s[j] in ")]":
        return "()"
    k = j
    while k >= 0 and (s[k].isalnum() or s[k] in "_$@"):
        k -= 1
    return s[k + 1:j + 1] or "()"


def _is_new(s: str, p: int) -> bool:
    k = p - 1
    while k >= 0 and s[k] in " \t":
        k -= 1
    return s[max(0, k - 2):k + 1] == "new" and (k < 3 or not (s[k - 3].isalnum() or s[k - 3] == "_"))


def _cfunc_defs(s: str, match: dict, spec: Spec):
    """「型 名前(引数) 修飾 {」を関数定義として推定する。"""
    out = []
    n = len(s)
    for m in _CFUNC_RE.finditer(s):
        name = m.group(1)
        base = name.rsplit("::", 1)[-1].lstrip("~")
        if base in KEYWORDS or base in ("if", "for", "while", "switch", "catch", "using", "lock", "fixed", "foreach", "function"):
            continue
        ls = max(s.rfind("\n", 0, m.start()), s.rfind(";", 0, m.start()), s.rfind("{", 0, m.start()),
                 s.rfind("}", 0, m.start()))
        prefix = s[ls + 1:m.start()]
        if _CFUNC_BAD_PREFIX.search(prefix):
            continue
        if spec.js_like:
            words = prefix.split()
            if any(w not in _JS_MODIFIERS for w in words):
                continue
        elif prefix.strip() and not re.fullmatch(r"[\w\s*&<>,\[\]:~?@]*", prefix):
            continue
        paren = m.end() - 1
        close = match.get(paren)
        if close is None or close <= paren:
            continue
        k = skip_ws(s, close + 1)
        brace = s.find("{", k, min(n, k + 400))
        if brace < 0:
            continue
        tail = s[close + 1:brace]
        if ";" in tail or "=" in tail.replace("=>", "") or not _CFUNC_TAIL.match(tail):
            if not (spec.js_like and tail.strip().startswith(":")):
                continue
        if not prefix.strip() and not spec.js_like and spec.name not in ("c", "cpp", "dart") and not base[:1].isupper():
            # Java/C# は戻り値型などの前置きが必須 (修飾子なしコンストラクタ=大文字始まりは許可)
            continue
        out.append((m.start(1), name, None, (brace, match.get(brace, n - 1))))
    return out


def analyze_generic(rel: str, src: str, spec: Spec) -> dict:
    clean, nocom = strip_source(src, spec)
    match = build_match(clean) if spec.body == "brace" else {}
    li = LineIndex(clean)
    src_lines = src.split("\n")
    n = len(clean)

    def body_range(pos_after: int, def_pos: int, kind: str):
        if spec.body == "brace":
            if kind == "arrow":
                k = skip_ws(clean, pos_after)
                if k < n and clean[k] == "{":
                    return k, match.get(k, n - 1)
                return k, stmt_end(clean, k, n, match)
            if spec.name == "shell":
                k = clean.find("{", pos_after, pos_after + 200)
                if k < 0:
                    k = clean.find("(", pos_after, pos_after + 200)
                return (k, match.get(k, n - 1)) if k >= 0 else None
            return find_body(clean, pos_after, match, spec)
        line0 = li.line(def_pos) - 1
        last = indent_body(src_lines, line0, spec.body)
        end_pos = li.start_of(last + 1) + len(src_lines[last])
        return li.start_of(line0 + 1) + len(src_lines[line0]), end_pos

    # --- クラス ---
    classes = []
    for pat in spec.class_patterns:
        for m in pat.finditer(clean):
            if spec.body == "brace":
                b = find_body(clean, m.end(), match, spec, limit=600)
                if not b or clean[b[0]] != "{":
                    continue
            else:
                b = body_range(m.end(), m.start(), "def")
            classes.append({"name": m.group("name"), "pos": m.start("name"), "body": b})
    classes.sort(key=lambda c: c["pos"])

    # --- 関数 ---
    raw = []
    seen = set()
    for pat, kind in spec.def_patterns:
        for m in pat.finditer(clean):
            p = m.start("name")
            if p in seen:
                continue
            b = body_range(m.end(), m.start(), kind)
            if not b:
                continue
            seen.add(p)
            recv = m.groupdict().get("recv")
            raw.append((p, m.group("name"), recv, b))
    if spec.cfunc:
        for d in _cfunc_defs(clean, match, spec):
            if d[0] not in seen:
                seen.add(d[0])
                raw.append(d)
    raw.sort()

    def owner_class(pos):
        best = None
        for c in classes:
            b = c["body"]
            if b and b[0] <= pos <= b[1]:
                if best is None or c["body"][0] >= best["body"][0]:
                    best = c
        return best

    symbols = []
    def_positions = set()
    for p, name, recv, b in raw:
        cls = None
        if recv:
            cls = recv.rsplit(".", 1)[-1]
        elif "::" in name:
            cls, name = name.rsplit("::", 1)
        elif spec.name == "lua" and ("." in name or ":" in name):
            cls, name = re.split(r"[.:]", name)[-2:]
        else:
            oc = owner_class(p)
            cls = oc["name"] if oc else None
        def_positions.add(p)
        qual = f"{cls}.{name}" if cls else name
        symbols.append({
            "name": name, "qual": qual, "kind": "method" if cls else "function", "cls": cls,
            "start": li.line(p), "end": li.line(b[1]), "b": [b[0], b[1]], "calls": [],
        })
    for c in classes:
        b = c["body"]
        symbols.append({
            "name": c["name"], "qual": c["name"], "kind": "class", "cls": None,
            "start": li.line(c["pos"]), "end": li.line(b[1]) if b else li.line(c["pos"]),
            "b": None, "calls": [],
        })

    funcs = sorted((s for s in symbols if s["b"]), key=lambda s: (s["b"][0], -s["b"][1]))
    toplevel = None
    if spec.script_toplevel:
        toplevel = {"name": "<top-level>", "qual": "<top-level>", "kind": "toplevel", "cls": None,
                    "start": 1, "end": len(src_lines), "b": None, "calls": []}
    class_ranges = [c["body"] for c in classes if c["body"]]

    # --- 呼び出し ---
    calls = []
    for m in CALL_RE.finditer(clean):
        name = m.group(1)
        p = m.start(1)
        if p in def_positions or (spec.name == "shell" and name in SHELL_WORDS):
            continue
        recv = _recv_before(clean, p)
        if name in KEYWORDS and not recv:   # Engine::new() / Foo.new は呼び出し
            continue
        calls.append((p, name, recv, _is_new(clean, p)))
    for pat in spec.extra_calls:
        for m in pat.finditer(clean):
            name = m.group(1)
            p = m.start(1)
            if name in KEYWORDS or name in SHELL_WORDS or p in def_positions:
                continue
            calls.append((p, name, "", False))
    calls.sort()

    # 一番内側の関数へ割り当て (区間は入れ子前提のスイープ)
    stack: list[dict] = []
    fi = 0
    for p, name, recv, is_new in calls:
        while fi < len(funcs) and funcs[fi]["b"][0] <= p:
            stack.append(funcs[fi])
            fi += 1
        while stack and stack[-1]["b"][1] < p:
            stack.pop()
        owner = None
        for f in reversed(stack):
            if f["b"][0] <= p <= f["b"][1]:
                owner = f
                break
        if owner is None:
            if toplevel is None or any(a <= p <= b for a, b in class_ranges):
                continue
            owner = toplevel
        owner["calls"].append([name, recv, li.line(p), 1 if is_new else 0])

    if toplevel is not None and toplevel["calls"]:
        symbols.append(toplevel)

    imports = parse_imports(nocom, spec, li)
    return {"lang": spec.name, "symbols": symbols, "imports": imports}


_JS_CLAUSE_NAMED = re.compile(r"\{([^}]*)\}")


def _js_clause(clause: str, mod: str, line: int) -> list[dict]:
    out = []
    clause = clause.strip()
    if not clause:
        return [{"mod": mod, "line": line, "kind": "module", "local": None, "orig": None}]
    named = _JS_CLAUSE_NAMED.search(clause)
    if named:
        for part in named.group(1).split(","):
            part = part.strip().replace("type ", "")
            if not part:
                continue
            bits = re.split(r"\s+as\s+|\s*:\s*", part)
            orig = bits[0].strip()
            local = bits[-1].strip()
            out.append({"mod": mod, "line": line, "kind": "from", "local": local, "orig": orig})
        clause = clause[:named.start()] + clause[named.end():]
    ns = re.search(r"\*\s*as\s+(" + ID + ")", clause)
    if ns:
        out.append({"mod": mod, "line": line, "kind": "module", "local": ns.group(1), "orig": None})
        clause = clause.replace(ns.group(0), "")
    dm = re.match(r"\s*(" + ID + r")\s*,?", clause)
    if dm and dm.group(1) not in ("type",):
        out.append({"mod": mod, "line": line, "kind": "default", "local": dm.group(1), "orig": dm.group(1)})
    if not out:
        out.append({"mod": mod, "line": line, "kind": "module", "local": None, "orig": None})
    return out


def parse_imports(text: str, spec: Spec, li: LineIndex) -> list[dict]:
    out = []
    for pat, mode in spec.import_patterns:
        for m in pat.finditer(text):
            line = li.line(m.start())
            gd = m.groupdict()
            if mode == "goblock":
                for gm in re.finditer(r"(?:(\w+)\s+)?\"([^\"]+)\"", gd["block"]):
                    out.append({"mod": gm.group(2), "line": line, "kind": "module", "local": gm.group(1), "orig": None, "mode": "gopkg"})
                continue
            mod = gd.get("mod")
            if not mod:
                continue
            if mode == "js":
                for item in _js_clause(gd.get("clause") or "", mod, line):
                    item["mode"] = "path"
                    out.append(item)
                continue
            out.append({"mod": mod, "line": line, "kind": "module", "local": None, "orig": None, "mode": mode})
    return out


# ---------------------------------------------------------------------------
# 関数内フロー (if/for/switch/try…) の抽出: 波括弧系言語
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z_]\w*")
_CASE_RE = re.compile(r"\b(?:case\b(?P<lab>.*?)(?:(?<!:):(?!:)|->|=>)|(?P<def>default)\s*(?::|->|=>))", re.S)
_SPACES = re.compile(r"\s+")


def _short(t: str, n: int = 90) -> str:
    t = _SPACES.sub(" ", t).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


class FlowParser:
    LOOPS = {"for", "while", "foreach", "loop", "until"}
    SWITCHES = {"switch", "match", "when", "select"}
    TERMINALS = {"return", "throw", "raise"}

    def __init__(self, clean: str, disp: str, match: dict, li: LineIndex):
        self.s = clean
        self.d = disp
        self.m = match
        self.li = li
        self.budget = 4000

    def word_at(self, i):
        w = _WORD.match(self.s, i)
        return w.group() if w else ""

    def calls_in(self, a, b):
        names = []
        for m in CALL_RE.finditer(self.s, a, b):
            nm = m.group(1)
            if nm not in KEYWORDS and nm not in names:
                names.append(nm)
        return names

    def block(self, i: int, end: int) -> list:
        items = []
        while i < end and self.budget > 0:
            i = skip_ws(self.s, i)
            if i >= end:
                break
            ch = self.s[i]
            if ch in ";,":
                i += 1
                continue
            if ch == "}":
                i += 1
                continue
            if ch == "{":
                close = self.m.get(i, end)
                items.extend(self.block(i + 1, min(close, end)))
                i = close + 1
                continue
            item, j = self.statement(i, end)
            if item:
                items.append(item)
            i = max(j, i + 1)
        return items

    def cond(self, i, end):
        k = skip_ws(self.s, i)
        if k < end and self.s[k] == "(":
            close = self.m.get(k, end)
            nk = skip_ws(self.s, close + 1)
            # 「if (a) && b {」のように括弧の後ろへ条件が続く場合だけ { まで読む
            if not (nk < end and self.s[nk] in "&|+-*/<>=!.?"):
                return _short(self.d[k + 1:close]), close + 1
        j = k
        while j < end:
            ch = self.s[j]
            if ch in "([":
                j = self.m.get(j, end) + 1
                continue
            if ch == "{" or ch == ";":
                break
            j += 1
        return _short(self.d[k:j]), j

    def body(self, j, end):
        k = skip_ws(self.s, j)
        if k < end and self.s[k] == "{":
            close = self.m.get(k, end)
            return self.block(k + 1, min(close, end)), close + 1
        if k < end and self.s[k] == ":":   # PHP の代替構文などは無視して1文
            k += 1
        it, nj = self.statement(k, end)
        return ([it] if it else []), nj

    def statement(self, i: int, end: int):
        self.budget -= 1
        s = self.s
        w = self.word_at(i)
        line = self.li.line(i)
        if w in ("if", "elseif", "elif", "unless", "guard"):
            c, j = self.cond(i + len(w), end)
            then, j = self.body(j, end)
            els = []
            k = skip_ws(s, j)
            w2 = self.word_at(k)
            if w2 == "else":
                k2 = skip_ws(s, k + 4)
                if self.word_at(k2) == "if":
                    it, j = self.statement(k2, end)
                    els = [it] if it else []
                else:
                    els, j = self.body(k2, end)
            elif w2 in ("elseif", "elif"):
                it, j = self.statement(k, end)
                els = [it] if it else []
            if w == "guard":
                return {"t": "if", "x": "guard " + c, "l": line, "c": self.calls_in(i, i + len(c) + 8), "a": [], "b": els}, j
            return {"t": "if", "x": c, "l": line, "c": [], "a": then, "b": els}, j
        if w in self.LOOPS:
            c, j = (("", i + 4) if w == "loop" else self.cond(i + len(w), end))
            body, j = self.body(j, end)
            return {"t": "loop", "x": f"{w} {c}".strip(), "l": line, "c": [], "a": body}, j
        if w == "do" or w == "repeat":
            body, j = self.body(i + len(w), end)
            k = skip_ws(s, j)
            w2 = self.word_at(k)
            if w2 in ("while", "until"):
                c, j = self.cond(k + len(w2), end)
                return {"t": "loop", "x": f"do … {w2} {c}", "l": line, "c": [], "a": body, "post": 1}, j
            if w2 == "catch":   # Swift do-catch
                return self._try_rest(body, line, k, end)
            return {"t": "group", "x": w, "l": line, "a": body}, j
        if w in self.SWITCHES:
            c, j = self.cond(i + len(w), end)
            k = skip_ws(s, j)
            if k < end and s[k] == "{":
                close = self.m.get(k, end)
                cases = self.cases(k + 1, min(close, end))
                return {"t": "switch", "x": f"{w} {c}".strip(), "l": line, "c": [], "cases": cases}, close + 1
            return {"t": "stmt", "x": f"{w} {c}", "l": line, "c": []}, j
        if w == "try":
            body, j = self.body(i + 3, end)
            return self._try_rest(body, line, j, end)
        if w in self.TERMINALS:
            e = stmt_end(s, i, end, self.m)
            return {"t": "return", "x": _short(self.d[i:e]), "l": line, "c": self.calls_in(i, e)}, e + 1
        if w in ("break", "continue"):
            e = stmt_end(s, i, end, self.m)
            return {"t": w, "x": w, "l": line}, e + 1
        if w in ("else", "case", "default"):
            return None, i + len(w)
        e = stmt_end(s, i, end, self.m)
        if e <= i:
            e = i + 1
        text = self.d[i:e]
        if w in ("function", "fun", "func", "fn", "def", "class", "struct", "impl"):
            return {"t": "stmt", "x": _short(text.split("{")[0], 60), "l": line, "c": [], "def": 1}, e + 1
        return {"t": "stmt", "x": _short(text), "l": line, "c": self.calls_in(i, e)}, e + 1

    def _try_rest(self, body, line, j, end):
        handlers = []
        fin = []
        while True:
            k = skip_ws(self.s, j)
            w = self.word_at(k)
            if w in ("catch", "except", "rescue"):
                c, jj = self.cond(k + len(w), end)
                hb, j = self.body(jj, end)
                handlers.append({"x": c or w, "a": hb})
            elif w in ("finally", "ensure"):
                fin, j = self.body(k + len(w), end)
            else:
                break
        return {"t": "try", "x": "try", "l": line, "c": [], "a": body, "h": handlers, "f": fin}, j

    def _top_labels(self, a, b):
        """[a,b) の中で入れ子括弧の外側にある case ラベルを列挙。"""
        labs = []
        i = a
        while i < b:
            nxt = _BR.search(self.s, i, b)
            seg_end = nxt.start() if nxt else b
            for m in _CASE_RE.finditer(self.s, i, seg_end):
                lab = "default" if m.group("def") else _short(self.d[m.start("lab"):m.end("lab")], 50)
                labs.append((m.start(), m.end(), lab))
            if not nxt:
                break
            p = nxt.start()
            i = (self.m.get(p, b) + 1) if self.s[p] in "{([" else p + 1
        return labs

    def cases(self, a, b):
        labs = self._top_labels(a, b)
        out = []
        if labs:
            for idx, (st, en, lab) in enumerate(labs):
                stop = labs[idx + 1][0] if idx + 1 < len(labs) else b
                out.append({"x": lab, "a": self.block(en, stop)})
            return out
        # Rust match / Kotlin when: 「パターン => 式」をトップレベルの , / 改行で区切る
        i = a
        while i < b and len(out) < 60:
            i = skip_ws(self.s, i)
            if i >= b:
                break
            e = stmt_end(self.s, i, b, self.m)
            seg = self.s[i:e]
            arrow = re.search(r"=>|->", seg)
            if arrow:
                lab = _short(self.d[i:i + arrow.start()], 50)
                bs = i + arrow.end()
                k = skip_ws(self.s, bs)
                if k < b and self.s[k] == "{":
                    close = self.m.get(k, b)
                    out.append({"x": lab, "a": self.block(k + 1, close)})
                    e = max(e, close)
                else:
                    out.append({"x": lab, "a": [{"t": "stmt", "x": _short(self.d[bs:e]), "l": self.li.line(bs), "c": self.calls_in(bs, e)}]})
            i = e + 1
        return out


_prep_cache: dict = {}


def _prepare(src: str, spec: Spec):
    """フロー抽出用の前処理結果 (同じ内容のファイルは使い回す)。"""
    key = (spec.name, hash(src), len(src))
    hit = _prep_cache.get(key)
    if hit:
        return hit
    clean, nocom = strip_source(src, spec)
    match = build_match(clean) if spec.body == "brace" else {}
    res = analyze_generic("", src, spec)
    val = (clean, nocom, match, LineIndex(clean), res)
    if len(_prep_cache) > 32:
        _prep_cache.clear()
    _prep_cache[key] = val
    return val


def generic_flow(src: str, spec: Spec, start_line: int) -> list:
    """start_line で定義される関数本体のフローを返す。"""
    clean, nocom, match, li, res = _prepare(src, spec)
    if spec.flow == "lines" or spec.body != "brace":
        sym = next((s for s in res["symbols"] if s["start"] == start_line and s["kind"] != "class"), None)
        if sym is None:
            sym = next((s for s in res["symbols"] if s["kind"] == "toplevel"), None)
        if sym is None:
            return []
        by_line = {}
        for name, _r, line, _n in sym["calls"]:
            by_line.setdefault(line, []).append(name)
        lines = src.split("\n")
        return [{"t": "stmt", "x": _short(lines[ln - 1]), "l": ln, "c": names} for ln, names in sorted(by_line.items())]
    sym = next((s for s in res["symbols"] if s["start"] == start_line and s["b"]), None)
    fp = FlowParser(clean, nocom, match, li)
    if sym is None:
        # トップレベル: 関数・クラス本体を除いた部分を順に
        tops = sorted([s["b"] for s in res["symbols"] if s["b"]])
        items = []
        i = 0
        for a, b in tops + [(len(clean), len(clean))]:
            # 定義の頭 (function foo(...) ) は行頭から
            ls = clean.rfind("\n", 0, a) + 1
            if ls > i:
                items.extend(it for it in fp.block(i, ls) if not it.get("def"))
            i = max(i, b + 1)
        return items
    a, b = sym["b"]
    if clean[a] == "{":
        return fp.block(a + 1, b)
    return [{"t": "return", "x": _short(nocom[a:b + 1]), "l": li.line(a), "c": fp.calls_in(a, b + 1)}]
