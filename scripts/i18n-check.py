#!/usr/bin/env python3
"""Reconcile i18n keys used in the code with the catalogs in board/i18n/.

For each catalog present it prints:
  - keys used in the code with no translation (missing)
  - catalog keys the code never uses (unused)
  - translations whose {placeholders} differ from the key's
It also lists Chinese literals the code shows without going through t() (untagged).

Keys come from:
  - Python: t("…") / N_("…") calls with a literal first argument (AST)
  - JS:     t("…") / N_("…") calls with a string / plain template literal
  - HTML:   data-i18n / data-i18n-title / data-i18n-placeholder / data-i18n-aria-label
  - shell:  i18n "…" calls (function defined in the scripts)

Usage: scripts/i18n-check.py [--json] [--strict]
  --strict  exit 1 if any catalog is missing keys or has placeholder mismatches
Exit code is 0 otherwise: this is a report for the translators, not a gate.
"""

from __future__ import annotations

import ast
import html.parser
import json
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "board" / "i18n"
PY_FILES = ("board/tui.py", "board/collect.py", "board/server.py", "board/daemon.py", "board/logutil.py")
HTML_FILES = ("board/web/index.html",)
JS_FILES = ("board/web/app.js",)
SH_GLOB = "scripts/*.sh"
CJK_RE = re.compile(r"[　-〿一-鿿＀-￯]")
IGNORE_MARK = "i18n: internal"  # 行内注释：这一行的中文是内部值 / 只匹配数据，不翻译
TRANSLATE_FUNCS = {"t", "N_"}
LOG_FUNCS = {"note", "_optional"}  # 只在 debug 模式写日志：按约定不翻译
HTML_ATTRS = ("data-i18n", "data-i18n-title", "data-i18n-placeholder", "data-i18n-aria-label")
VISIBLE_ATTRS = ("title", "placeholder", "aria-label")


# ---------- Python ----------


def _func_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def python_keys(path: Path) -> tuple[list[tuple[str, int]], list[tuple[int, str]]]:
    """(keys used, untagged CJK literals) for one Python file."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    keys: list[tuple[str, int]] = []
    skip: set[int] = set()  # ids of nodes that are fine (translated, docstring, log, f-string parts)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Call):
            name = _func_name(node.func)
            if name in TRANSLATE_FUNCS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.append((first.value, first.lineno))
                    skip.add(id(first))
            if name in LOG_FUNCS:
                for arg in node.args:
                    for sub in ast.walk(arg):
                        skip.add(id(sub))
        if isinstance(node, ast.JoinedStr):
            for sub in ast.walk(node):
                if sub is not node and isinstance(sub, ast.Constant):
                    skip.add(id(sub))

    untagged: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skip or not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            continue
        segment = ast.get_source_segment(source, node) or ""
        if not CJK_RE.search(segment):
            continue
        line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        if IGNORE_MARK in line:
            continue
        untagged.append((node.lineno, segment[:90]))
    return keys, sorted(untagged)


# ---------- JS ----------


def _js_tokens(source: str):
    """Yield (kind, raw, value, start, end, extra) for strings / templates / comments / punctuation.

    For templates, extra = (static text parts, [(expr_start, expr_end), ...]) so callers can
    scan the `${...}` expressions as code (they often contain t("…") calls themselves).

    Small tokenizer — enough to tell string contents from code and comments.
    Regex literals are not modelled (app.js has none with quotes or CJK).
    """
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            yield ("comment", source[i:end], None, i, end, None)
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            yield ("comment", source[i:end], None, i, end, None)
            i = end
        elif ch in "\"'":
            j = i + 1
            while j < n and source[j] != ch:
                j += 2 if source[j] == "\\" else 1
            raw = source[i : j + 1]
            yield ("string", raw, _js_unquote(raw), i, j + 1, None)
            i = j + 1
        elif ch == "`":
            j, depth, parts, buf, has_expr, exprs, expr_start = i + 1, 0, [], [], False, [], 0
            while j < n:
                c = source[j]
                if depth == 0:
                    if c == "\\":
                        buf.append(source[j : j + 2])
                        j += 2
                        continue
                    if c == "`":
                        break
                    if source.startswith("${", j):
                        has_expr = True
                        parts.append("".join(buf))
                        buf = []
                        depth = 1
                        j += 2
                        expr_start = j
                        continue
                    buf.append(c)
                else:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            exprs.append((expr_start, j))
                    elif c in "\"'`":
                        # 跳过表达式里的字符串 / 嵌套模板（不追踪其中的 ${}）
                        quote = c
                        j += 1
                        nested = 0
                        while j < n:
                            if source[j] == "\\":
                                j += 2
                                continue
                            if quote == "`" and source.startswith("${", j):
                                nested += 1
                            elif quote == "`" and source[j] == "}" and nested:
                                nested -= 1
                            elif source[j] == quote and not nested:
                                break
                            j += 1
                j += 1
            parts.append("".join(buf))
            raw = source[i : j + 1]
            value = None if has_expr else _js_unquote_template("".join(parts))
            yield ("template", raw, value, i, j + 1, (parts, exprs))
            i = j + 1
        else:
            yield ("char", ch, None, i, i + 1, None)
            i += 1


def _js_unquote(raw: str) -> str:
    body = raw[1:-1]
    if raw[0] == "'":
        body = body.replace('\\"', '"').replace('"', '\\"').replace("\\'", "'")
    try:
        return json.loads(f'"{body}"')
    except ValueError:
        return body


def _js_unquote_template(text: str) -> str:
    return text.replace("\\`", "`").replace("\\$", "$").replace("\\\\", "\\")


def js_keys(path: Path) -> tuple[list[tuple[str, int]], list[tuple[int, str]]]:
    source = path.read_text(encoding="utf-8")
    keys: list[tuple[str, int]] = []
    untagged: list[tuple[int, str]] = []
    _js_scan(source, 0, len(source), source, keys, untagged)
    return keys, untagged


def _js_scan(text: str, base: int, _stop: int, source: str, keys: list, untagged: list) -> None:
    """Scan JS code `text` (which starts at offset `base` of `source`)."""
    lines = source.splitlines()
    tokens = [tok for tok in _js_tokens(text)]
    code = [tok for tok in tokens if tok[0] != "comment" and not (tok[0] == "char" and tok[1].isspace())]
    for index, (kind, raw, value, start, _end, extra) in enumerate(code):
        if kind not in {"string", "template"}:
            continue
        lineno = source.count("\n", 0, base + start) + 1
        line = lines[lineno - 1]
        if kind == "template" and extra and extra[1]:
            for expr_start, expr_end in extra[1]:
                _js_scan(text[expr_start:expr_end], base + expr_start, 0, source, keys, untagged)
        # t( / N_( 紧挨在前面？往回拼出标识符
        is_call = False
        if index >= 1 and code[index - 1][1] == "(":
            k = index - 2
            ident = ""
            while k >= 0 and code[k][0] == "char" and (code[k][1].isalnum() or code[k][1] in "_$"):
                ident = code[k][1] + ident
                k -= 1
            prev = code[k][1] if k >= 0 else ""
            is_call = ident in TRANSLATE_FUNCS and prev != "."
        if is_call and value is not None:
            keys.append((value, lineno))
            continue
        static = "".join(extra[0]) if kind == "template" and extra else raw
        if CJK_RE.search(static) and IGNORE_MARK not in line:
            untagged.append((lineno, raw[:90]))


# ---------- HTML ----------


class _HTMLScan(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.keys: list[tuple[str, int]] = []
        self.untagged: list[tuple[int, str]] = []
        self.stack: list[bool] = []  # 元素是否带 data-i18n（文本归它管）

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        line = self.getpos()[0]
        for name in HTML_ATTRS:
            if attrs.get(name):
                self.keys.append((attrs[name], line))
        for name in VISIBLE_ATTRS:
            value = attrs.get(name) or ""
            if CJK_RE.search(value) and not attrs.get(f"data-i18n-{name}"):
                self.untagged.append((line, f'{name}="{value}"'))
        if tag not in {"meta", "link", "br", "img", "input", "hr"}:
            self.stack.append(bool(attrs.get("data-i18n")))

    def handle_endtag(self, tag):
        if self.stack and tag not in {"meta", "link", "br", "img", "input", "hr"}:
            self.stack.pop()

    def handle_data(self, data):
        if CJK_RE.search(data) and not (self.stack and self.stack[-1]):
            self.untagged.append((self.getpos()[0], data.strip()[:90]))


def html_keys(path: Path) -> tuple[list[tuple[str, int]], list[tuple[int, str]]]:
    scan = _HTMLScan()
    scan.feed(path.read_text(encoding="utf-8"))
    return scan.keys, scan.untagged


# ---------- shell ----------

SH_CALL_RE = re.compile(r'''\bi18n\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')''')


def sh_keys(path: Path) -> tuple[list[tuple[str, int]], list[tuple[int, str]]]:
    keys: list[tuple[str, int]] = []
    untagged: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        found = False
        for match in SH_CALL_RE.finditer(line):
            found = True
            value = match.group(1) if match.group(1) is not None else match.group(2)
            if match.group(1) is not None:
                value = re.sub(r'\\(["\\$`])', r"\1", value)
            keys.append((value, lineno))
        if not found and CJK_RE.search(line) and IGNORE_MARK not in line:
            untagged.append((lineno, stripped[:90]))
    return keys, untagged


# ---------- 汇总 ----------


def collect_keys(root: Path = ROOT) -> tuple[dict[str, list[str]], dict[str, list[tuple[int, str]]]]:
    """({key: [file:line, ...]} in first-use order, {file: untagged literals})."""
    used: dict[str, list[str]] = {}
    untagged: dict[str, list[tuple[int, str]]] = {}
    sources: list[tuple[str, object]] = []
    sources += [(rel, python_keys) for rel in PY_FILES]
    sources += [(rel, html_keys) for rel in HTML_FILES]
    sources += [(rel, js_keys) for rel in JS_FILES]
    sources += [(str(p.relative_to(root)), sh_keys) for p in sorted(root.glob(SH_GLOB))]
    for rel, scanner in sources:
        path = root / rel
        if not path.exists():
            continue
        keys, loose = scanner(path)  # type: ignore[operator]
        for key, line in keys:
            used.setdefault(key, []).append(f"{rel}:{line}")
        if loose:
            untagged[rel] = loose
    return used, untagged


def placeholders(text: str) -> set[str]:
    names: set[str] = set()
    try:
        for _literal, field, _spec, _conv in string.Formatter().parse(text):
            if field is not None:
                names.add(field)
    except ValueError:
        names.add("<invalid format>")
    return names


def load_catalogs(directory: Path = CATALOG_DIR) -> dict[str, dict | str]:
    """{lang: catalog} for every board/i18n/*.json; a str value is a load error."""
    catalogs: dict[str, dict | str] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            catalogs[path.stem] = f"invalid JSON: {exc}"
            continue
        if not isinstance(data, dict):
            catalogs[path.stem] = "not a JSON object"
            continue
        catalogs[path.stem] = {k: v for k, v in data.items() if k != "_notas"}
    return catalogs


def reconcile(used: dict[str, list[str]], catalog: dict) -> dict[str, list]:
    missing = [key for key in used if not isinstance(catalog.get(key), str) or not catalog.get(key)]
    unused = [key for key in catalog if key not in used]
    mismatched = [
        key
        for key in used
        if isinstance(catalog.get(key), str) and catalog[key] and placeholders(catalog[key]) != placeholders(key)
    ]
    return {"missing": missing, "unused": unused, "placeholder_mismatch": mismatched}


def report(root: Path = ROOT) -> dict:
    used, untagged = collect_keys(root)
    catalogs = load_catalogs(root / "board" / "i18n")
    result: dict = {
        "used": len(used),
        "untagged": {rel: [f"{line}: {text}" for line, text in items] for rel, items in untagged.items()},
        "catalogs": {},
    }
    for lang, catalog in catalogs.items():
        if isinstance(catalog, str):
            result["catalogs"][lang] = {"error": catalog}
            continue
        result["catalogs"][lang] = {"size": len(catalog), **reconcile(used, catalog)}
    result["_where"] = used
    return result


def main(argv: list[str]) -> int:
    data = report()
    used = data.pop("_where")
    if "--json" in argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"keys used in code: {data['used']}")
        if not data["catalogs"]:
            print("no catalogs in board/i18n/ yet")
        for lang, info in data["catalogs"].items():
            print(f"\n== {lang} ==")
            if "error" in info:
                print(f"  ERROR: {info['error']}")
                continue
            print(
                f"  catalog keys: {info['size']} · missing: {len(info['missing'])} · "
                f"unused: {len(info['unused'])} · placeholder mismatch: {len(info['placeholder_mismatch'])}"
            )
            for title, field in (
                ("used without translation", "missing"),
                ("in catalog but unused", "unused"),
                ("placeholders differ from key", "placeholder_mismatch"),
            ):
                if info[field]:
                    print(f"  -- {title}:")
                    for key in info[field]:
                        where = f"  ({used[key][0]})" if key in used else ""
                        print(f"     {key!r}{where}")
        if data["untagged"]:
            print("\n== Chinese literals shown without t() (mark intentional ones with '# i18n: internal') ==")
            for rel, items in data["untagged"].items():
                for item in items:
                    print(f"  {rel}:{item}")
    if "--strict" in argv:
        bad = any(
            "error" in info or info["missing"] or info["placeholder_mismatch"] for info in data["catalogs"].values()
        )
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
