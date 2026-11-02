#!/usr/bin/env python3
"""Runtime i18n for herdr-portal.

Keys are the original Chinese source strings (see .pares/cartoes/convencao-i18n.md):
`zh` needs no catalog, every other language looks the key up in
`board/i18n/<lang>.json` and falls back to the key itself when it is missing.

    from i18n import t, N_
    t("已释放 {n} 个 pane", n=3)

`N_()` only marks a string for extraction (module-level constants); translate it
with `t()` where it is displayed.

Also usable from shell scripts:

    python3 board/i18n/__init__.py "日志: {runtime}/server.log" runtime="$runtime"
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

CATALOG_DIR = Path(__file__).resolve().parent
SUPPORTED = ("en", "pt_BR", "zh")
DEFAULT = "en"
SOURCE = "zh"  # 源文本语言：不需要目录
_ENV_ORDER = ("HERDR_PORTAL_LANG", "LC_ALL", "LC_MESSAGES", "LANG")

_lock = threading.Lock()
_lang: str | None = None
_catalog: dict[str, str] | None = None


def _note(context: str, exc: BaseException | None = None) -> None:
    """Debug-only trace through logutil; i18n must work even when logutil is not importable."""
    try:
        import logutil  # noqa: PLC0415 - board/ 在 sys.path 上时才有
    except Exception:  # noqa: BLE001
        return
    logutil.note(context, exc)


def normalize(value: str | None) -> str | None:
    """Map a locale-ish value to a supported language; None for empty values.

    `pt`, `pt_BR`, `pt-BR.UTF-8` -> pt_BR; `en*` -> en; `zh*` -> zh; anything else
    (including `C` / `POSIX`) -> en.
    """
    text = (value or "").strip()
    if not text:
        return None
    base = text.split(".", 1)[0].split("@", 1)[0].replace("-", "_").lower()
    if base.startswith("pt"):
        return "pt_BR"
    if base.startswith("zh"):
        return "zh"
    return DEFAULT


CONFIG_FILE = "lang"  # 插件配置目录里的一行文本：en / pt_BR / zh


def config_dir(environ: Any = None) -> Path | None:
    """Plugin config dir: $HERDR_PLUGIN_CONFIG_DIR (set by herdr), else the default herdr path.

    With an explicit `environ` (tests) only that mapping is consulted, never the real home.
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("HERDR_PLUGIN_CONFIG_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    base = (env.get("XDG_CONFIG_HOME") or "").strip()
    if not base:
        home = (env.get("HOME") or "").strip() or (str(Path.home()) if environ is None else "")
        if not home:
            return None
        base = str(Path(home) / ".config")
    return Path(base) / "herdr" / "plugins" / "config" / "herdr-portal"


def configured_lang(environ: Any = None) -> str | None:
    """Language written in `<config dir>/lang` (first non-empty, non-comment line), if any."""
    directory = config_dir(environ)
    if directory is None:
        return None
    path = directory / CONFIG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return normalize(line)
    return None


def resolve_lang(environ: Any = None) -> str:
    """HERDR_PORTAL_LANG > `<plugin config dir>/lang` > LC_ALL > LC_MESSAGES > LANG."""
    env = os.environ if environ is None else environ
    forced = normalize(env.get(_ENV_ORDER[0]))
    if forced:
        return forced
    from_file = configured_lang(env)
    if from_file:
        return from_file
    for name in _ENV_ORDER[1:]:
        lang_ = normalize(env.get(name))
        if lang_:
            return lang_
    return DEFAULT


def load_catalog(lang_: str, directory: Path | None = None) -> dict[str, str]:
    """Read `<lang>.json`; missing / invalid file -> {} (callers then show the zh key)."""
    if lang_ == SOURCE:
        return {}
    path = (directory or CATALOG_DIR) / f"{lang_}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        _note(f"i18n: 没有找到目录 {path}，回落到中文原文", exc)
        return {}
    except (OSError, ValueError) as exc:
        _note(f"i18n: 目录读取失败 {path}，回落到中文原文", exc)
        return {}
    if not isinstance(raw, dict):
        _note(f"i18n: 目录不是 JSON 对象 {path}，回落到中文原文")
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if key != "_notas" and isinstance(value, str) and value
    }


def _ensure() -> tuple[str, dict[str, str]]:
    global _lang, _catalog
    if _lang is None or _catalog is None:
        with _lock:
            if _lang is None or _catalog is None:
                lang_ = resolve_lang()
                _catalog = load_catalog(lang_)
                _lang = lang_
    return _lang, _catalog


def reset() -> None:
    """Forget the resolved language / catalog (tests, or after changing the env)."""
    global _lang, _catalog
    with _lock:
        _lang = None
        _catalog = None


def use(lang_: str, catalog_: dict[str, str]) -> None:
    """Force a language + catalog (tests / tooling); `reset()` goes back to the environment."""
    global _lang, _catalog
    with _lock:
        _lang = lang_
        _catalog = {k: v for k, v in catalog_.items() if k != "_notas" and isinstance(v, str) and v}


def lang() -> str:
    return _ensure()[0]


def html_lang() -> str:
    """Value for `<html lang>`."""
    return {"pt_BR": "pt-BR", "zh": "zh-CN"}.get(lang(), lang())


def catalog() -> dict[str, str]:
    """The active catalog (empty for zh); a copy, safe to serialize."""
    return dict(_ensure()[1])


def translate(key: str, catalog_: dict[str, str], **kw: Any) -> str:
    text = catalog_.get(key) or key
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError) as exc:
        # 译文里的占位符写错了：退回原文再格式化，原文也不行就原样返回
        _note(f"i18n: 占位符格式化失败 {key!r}", exc)
        try:
            return key.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text


def t(key: str, **kw: Any) -> str:
    """Translate `key` (the zh source string); never raises."""
    return translate(key, _ensure()[1], **kw)


def N_(key: str) -> str:  # noqa: N802 - gettext 惯例
    """Mark a string for extraction without translating it (translate at display time)."""
    return key


def _main(argv: list[str]) -> int:
    """CLI for shell scripts: `__init__.py KEY [name=value ...]`; `--lang` prints the language."""
    if not argv:
        print("usage: i18n KEY [name=value ...] | --lang", file=sys.stderr)
        return 2
    if argv[0] == "--lang":
        print(lang())
        return 0
    kw = dict(arg.split("=", 1) for arg in argv[1:] if "=" in arg)
    print(t(argv[0], **kw))
    return 0


if __name__ == "__main__":
    board = str(CATALOG_DIR.parent)
    if board not in sys.path:
        sys.path.insert(1, board)  # 让 _note 能找到 logutil
    raise SystemExit(_main(sys.argv[1:]))
