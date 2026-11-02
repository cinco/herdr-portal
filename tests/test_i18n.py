#!/usr/bin/env python3
"""board/i18n 的单元测试 + 代码里用到的键与目录的对账（只报告，不因缺译文失败）。

跑法：python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "board") not in sys.path:
    sys.path.insert(0, str(ROOT / "board"))

import collect  # noqa: E402
import i18n  # noqa: E402


def load_checker():
    """scripts/i18n-check.py（文件名带连字符，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("i18n_check", ROOT / "scripts" / "i18n-check.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class I18nTestCase(unittest.TestCase):
    def setUp(self) -> None:
        i18n.reset()

    def tearDown(self) -> None:
        i18n.reset()


class TestResolveLang(I18nTestCase):
    def test_portal_lang_wins_over_locale(self):
        env = {"HERDR_PORTAL_LANG": "pt-BR", "LC_ALL": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}
        self.assertEqual(i18n.resolve_lang(env), "pt_BR")

    def test_precedence_lc_all_then_lc_messages_then_lang(self):
        self.assertEqual(i18n.resolve_lang({"LC_ALL": "pt_BR.UTF-8", "LC_MESSAGES": "zh_CN", "LANG": "en_US"}), "pt_BR")
        self.assertEqual(i18n.resolve_lang({"LC_MESSAGES": "zh_CN.UTF-8", "LANG": "pt_BR.UTF-8"}), "zh")
        self.assertEqual(i18n.resolve_lang({"LANG": "pt_PT.UTF-8"}), "pt_BR")

    def test_empty_values_are_skipped(self):
        self.assertEqual(i18n.resolve_lang({"HERDR_PORTAL_LANG": "", "LC_ALL": "  ", "LANG": "zh_TW.UTF-8"}), "zh")

    def test_aliases(self):
        for value, expected in {
            "pt": "pt_BR",
            "pt_BR": "pt_BR",
            "pt-BR": "pt_BR",
            "PT_br.utf8": "pt_BR",
            "en": "en",
            "en_GB.UTF-8": "en",
            "zh": "zh",
            "zh-Hans": "zh",
            "zh_CN.UTF-8@stroke": "zh",
        }.items():
            with self.subTest(value=value):
                self.assertEqual(i18n.resolve_lang({"HERDR_PORTAL_LANG": value}), expected)

    def test_unknown_and_posix_fall_back_to_english(self):
        for value in ("fr_FR.UTF-8", "C", "POSIX", "C.UTF-8", "de"):
            with self.subTest(value=value):
                self.assertEqual(i18n.resolve_lang({"LANG": value}), "en")
        self.assertEqual(i18n.resolve_lang({}), "en")

    def test_config_file_sits_between_portal_lang_and_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "lang").write_text("# idioma\n\npt-BR\n", encoding="utf-8")
            env = {"HERDR_PLUGIN_CONFIG_DIR": tmp, "LANG": "en_US.UTF-8"}
            self.assertEqual(i18n.resolve_lang(env), "pt_BR")
            self.assertEqual(i18n.resolve_lang({**env, "HERDR_PORTAL_LANG": "zh"}), "zh")
            self.assertEqual(i18n.resolve_lang({**env, "LC_ALL": "zh_CN.UTF-8"}), "pt_BR")

    def test_config_file_default_path_and_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp, "herdr", "plugins", "config", "herdr-portal")
            cfg.mkdir(parents=True)
            self.assertEqual(i18n.resolve_lang({"XDG_CONFIG_HOME": tmp, "LANG": "zh_CN"}), "zh")
            (cfg / "lang").write_text("pt_BR", encoding="utf-8")
            self.assertEqual(i18n.resolve_lang({"XDG_CONFIG_HOME": tmp, "LANG": "zh_CN"}), "pt_BR")
        # explicit environ without any config hint never reads the real home
        self.assertIsNone(i18n.config_dir({"LANG": "en_US"}))

    def test_lang_reads_process_environment(self):
        with mock.patch.dict(os.environ, {"HERDR_PORTAL_LANG": "pt"}):
            i18n.reset()
            self.assertEqual(i18n.lang(), "pt_BR")
            self.assertEqual(i18n.html_lang(), "pt-BR")


class TestTranslate(I18nTestCase):
    def test_missing_key_returns_key(self):
        i18n.use("en", {})
        self.assertEqual(i18n.t("当前没有项目"), "当前没有项目")

    def test_missing_key_still_formats_placeholders(self):
        i18n.use("en", {})
        self.assertEqual(i18n.t("已释放 {panes} 个 pane", panes=3), "已释放 3 个 pane")

    def test_translation_with_placeholders(self):
        i18n.use("en", {"发现新版 v{latest}（当前 v{current}）": "v{current} → v{latest} available"})
        self.assertEqual(i18n.t("发现新版 v{latest}（当前 v{current}）", latest="1.4.0", current="1.3.0"), "v1.3.0 → v1.4.0 available")

    def test_no_kwargs_leaves_braces_alone(self):
        i18n.use("en", {"等待 \\1": "Waiting for \\1 {x}"})
        self.assertEqual(i18n.t("等待 \\1"), "Waiting for \\1 {x}")

    def test_broken_placeholder_in_translation_falls_back_to_key(self):
        i18n.use("en", {"静默 {quiet}": "quiet {quite}"})
        self.assertEqual(i18n.t("静默 {quiet}", quiet="5m"), "静默 5m")

    def test_zh_never_uses_a_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "zh.json").write_text(json.dumps({"窗口": "WRONG"}), encoding="utf-8")
            self.assertEqual(i18n.load_catalog("zh", Path(tmp)), {})

    def test_n_marker_is_identity(self):
        self.assertEqual(i18n.N_("待确认"), "待确认")


class TestLoadCatalog(I18nTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def test_missing_file_is_empty(self):
        self.assertEqual(i18n.load_catalog("en", self.dir), {})

    def test_invalid_json_is_empty(self):
        (self.dir / "en.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(i18n.load_catalog("en", self.dir), {})

    def test_non_object_is_empty(self):
        (self.dir / "en.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(i18n.load_catalog("en", self.dir), {})

    def test_notes_and_non_strings_are_ignored(self):
        (self.dir / "pt_BR.json").write_text(
            json.dumps({"_notas": {"x": "y"}, "窗口": "Janela", "空": "", "数": 3}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertEqual(i18n.load_catalog("pt_BR", self.dir), {"窗口": "Janela"})

    def test_missing_catalog_degrades_to_source_text(self):
        with mock.patch.object(i18n, "CATALOG_DIR", self.dir), mock.patch.dict(os.environ, {"HERDR_PORTAL_LANG": "en"}):
            i18n.reset()
            self.assertEqual(i18n.lang(), "en")
            self.assertEqual(i18n.catalog(), {})
            self.assertEqual(i18n.t("窗口"), "窗口")


class TestInternalValuesVsLabels(I18nTestCase):
    """中文原文当内部值的地方：换成译文后逻辑不能变。"""

    CATALOG = {
        "刚刚": "just now",
        "{text} 前": "{text} ago",
        "周": "week",
        "月": "month",
        "7天": "7d",
        "等待 \\1 完成": "Waiting for \\1 to finish",
        "读取 \\1": "Lendo \\1",
    }

    def test_human_age_and_ago_are_structural(self):
        i18n.use("en", self.CATALOG)
        sys.modules.setdefault("curses", mock.MagicMock())  # 只用到 tui 的纯函数，不初始化终端
        import tui  # noqa: PLC0415

        self.assertEqual(collect.human_age(100.0, now=103.0), "just now")
        self.assertEqual(tui._ago(collect.human_age(100.0, now=103.0)), "just now")
        self.assertEqual(tui._ago(collect.human_age(100.0, now=112.0)), "12s ago")
        self.assertEqual(tui._ago("3h"), "3h ago")
        self.assertEqual(tui._ago(""), "")

    def test_help_line_keeps_the_last_segment(self):
        sys.modules.setdefault("curses", mock.MagicMock())
        import tui  # noqa: PLC0415

        text = "↑↓ Move  Space Toggle  A All  N None  T Threshold  Enter Reclaim  Esc Back"
        self.assertEqual(tui._help_line(text, 200), text)
        short = tui._help_line(text, 30)
        self.assertTrue(short.endswith("Esc Back"), short)
        self.assertLessEqual(tui._width(short), 30)
        self.assertTrue(short.startswith("↑↓ Move"), short)

    def test_humanize_uses_translated_replacement(self):
        i18n.use("en", self.CATALOG)
        self.assertEqual(collect._humanize_activity_title("Waiting for the build to finish"), "Waiting for build to finish")
        self.assertEqual(collect._humanize_activity_title("Read main.py"), "Lendo main.py")
        i18n.use("zh", {})
        self.assertEqual(collect._humanize_activity_title("Read main.py"), "读取 main.py")

    def test_plan_windows_sorted_by_internal_value_then_translated(self):
        i18n.use("en", self.CATALOG)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "agent.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE usage_history (provider, account_key, label, window_label, used_fraction, status, resets_at, recorded_at)"
            )
            for window in ("monthly", "weekly", "rolling-5h"):
                con.execute("INSERT INTO usage_history VALUES ('anthropic', 'k', 'l', ?, 0.5, 'ok', 0, 1)", (window,))
            con.commit()
            con.close()
            with mock.patch.object(collect, "_OMP_DB", db):
                collect._plan_memo = {}
                collect._plan_memo_at = 0.0
                summary = collect.plan_summary()
        collect._plan_memo = {}
        labels = [w["window_label"] for w in summary["providers"][0]["windows"]]
        self.assertEqual(labels, ["5h", "week", "month"])


class TestKeyCoverage(unittest.TestCase):
    """代码里用到的键 vs board/i18n/*.json。缺译文只打印（翻译是并行进行的），不让测试失败。"""

    @classmethod
    def setUpClass(cls):
        cls.checker = load_checker()
        cls.used, cls.untagged = cls.checker.collect_keys(ROOT)

    def test_extraction_sees_every_source_kind(self):
        where = {loc.split(":")[0] for locs in self.used.values() for loc in locs}
        for rel in ("board/tui.py", "board/collect.py", "board/server.py", "board/daemon.py",
                    "board/web/app.js", "board/web/index.html", "scripts/open-web.sh"):
            self.assertIn(rel, where)
        # 每种来源各抽一个已知键
        for key in ("{label} 已改为「{new_label}」", "未授权：本机看板需要一个有效 token", "关闭 (Esc)", "日志: {runtime}/server.log"):
            self.assertIn(key, self.used)

    def test_no_chinese_shown_without_t(self):
        """界面上的中文都要走 t()；故意保留的内部值在行尾标 `i18n: internal`。"""
        self.assertEqual(self.untagged, {}, json.dumps(self.untagged, ensure_ascii=False, indent=1))

    def test_report_missing_keys_per_catalog(self):
        catalogs = self.checker.load_catalogs(ROOT / "board" / "i18n")
        if not catalogs:
            print("\n[i18n] board/i18n/ 里还没有目录；代码用到", len(self.used), "个键", file=sys.stderr)
            return
        for lang, catalog in catalogs.items():
            if isinstance(catalog, str):
                print(f"\n[i18n] {lang}: {catalog}", file=sys.stderr)
                continue
            result = self.checker.reconcile(self.used, catalog)
            print(
                f"\n[i18n] {lang}: {len(catalog)} keys · missing {len(result['missing'])} · "
                f"unused {len(result['unused'])} · placeholder mismatch {len(result['placeholder_mismatch'])}",
                file=sys.stderr,
            )
            for key in result["missing"]:
                print(f"[i18n] {lang} missing: {key!r}", file=sys.stderr)
            for key in result["placeholder_mismatch"]:
                print(f"[i18n] {lang} placeholder mismatch: {key!r}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
