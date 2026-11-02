#!/usr/bin/env python3
"""Hardening of the fork (findings 1–9 of the security review).

No real herdr, no real server/daemon, no real browser: everything goes through mocks / temp dirs.
Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "board") not in sys.path:
    sys.path.insert(0, str(ROOT / "board"))

sys.modules.setdefault("curses", mock.MagicMock())  # tui 只用纯函数 / 方法，不初始化终端

import collect  # noqa: E402
import i18n  # noqa: E402
import server  # noqa: E402
import tui  # noqa: E402

UPSTREAM = "loo" + "fare"  # 拆开写：本文件自己也在「不许出现」的扫描范围里


class ZhTestCase(unittest.TestCase):
    """断言用中文原文：固定 zh。"""

    def setUp(self) -> None:
        i18n.use("zh", {})

    def tearDown(self) -> None:
        i18n.reset()


# ---------- 1. 更新只来自本 fork ----------


class TestUpdateRepo(ZhTestCase):
    def test_default_repo_is_the_fork(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERDR_PORTAL_REPO", None)
            self.assertEqual(collect.version_repo(), "cinco/herdr-portal")

    def test_override_and_invalid_values(self):
        with mock.patch.dict(os.environ, {"HERDR_PORTAL_REPO": "someone/other-fork"}):
            self.assertEqual(collect.version_repo(), "someone/other-fork")
        for bad in ("evil", "a/b/c", "x/y;rm -rf ~", "../../etc", "a/..", " "):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"HERDR_PORTAL_REPO": bad}):
                self.assertEqual(collect.version_repo(), "cinco/herdr-portal")

    def test_plugin_install_fallback_uses_the_same_repo(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(collect, "PLUGIN_ROOT", Path(tmp)):
            os.environ.pop("HERDR_PORTAL_REPO", None)
            plan = collect.update_plan()
            self.assertEqual(plan["mode"], "plugin")
            self.assertEqual(plan["command"][-1], "github:cinco/herdr-portal")
            with mock.patch.dict(os.environ, {"HERDR_PORTAL_REPO": "me/portal"}):
                self.assertEqual(collect.update_plan()["command"][-1], "github:me/portal")

    def test_linked_clone_uses_git_pull_ff_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(collect, "PLUGIN_ROOT", Path(tmp)):
            (Path(tmp) / ".git").mkdir()
            plan = collect.update_plan()
        self.assertEqual(plan["mode"], "git")
        self.assertEqual(plan["command"][-2:], ["pull", "--ff-only"])

    def test_version_check_url_points_at_the_fork(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            raise OSError("offline")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HERDR_PORTAL_STATE_DIR": tmp}), \
                mock.patch.object(collect.urllib.request, "urlopen", fake_urlopen):
            os.environ.pop("HERDR_PORTAL_REPO", None)
            record = collect.fetch_latest_version()
        self.assertIn("/cinco/herdr-portal/main/herdr-plugin.toml", seen["url"])
        self.assertIn("error", record)

    def test_nobody_references_the_upstream_repo(self):
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        offenders = []
        for rel in tracked:
            path = ROOT / rel
            if rel.startswith(".pares/") or path.suffix == ".png" or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if UPSTREAM in text.lower():
                offenders.append(rel)
        self.assertEqual(offenders, [])


# ---------- 2. mo clean：没装就不出现；真清理要两步 ----------


def fake_board(mo: bool):
    board = types.SimpleNamespace()
    board.reclaim = {"armed": False, "note": "", "mo": mo, "mo_armed": False}
    board.launch_mo = mock.Mock(return_value=True)
    board.disarm_mo = lambda: tui.Board.disarm_mo(board)
    board.request_mo = lambda dry_run: tui.Board.request_mo(board, dry_run)
    return board


class TestMoConfirmation(ZhTestCase):
    def test_missing_mo_never_runs(self):
        board = fake_board(mo=False)
        self.assertFalse(board.request_mo(False))
        self.assertFalse(board.request_mo(False))
        self.assertFalse(board.request_mo(True))
        board.launch_mo.assert_not_called()
        self.assertIn("未找到", board.reclaim["note"])

    def test_dry_run_goes_straight_through(self):
        board = fake_board(mo=True)
        self.assertTrue(board.request_mo(True))
        board.launch_mo.assert_called_once_with(dry_run=True)

    def test_real_clean_needs_two_presses(self):
        board = fake_board(mo=True)
        self.assertFalse(board.request_mo(False))
        board.launch_mo.assert_not_called()
        self.assertTrue(board.reclaim["mo_armed"])
        self.assertTrue(board.reclaim["note"])
        self.assertTrue(board.request_mo(False))
        board.launch_mo.assert_called_once_with(dry_run=False)
        self.assertFalse(board.reclaim["mo_armed"])

    def test_any_other_key_disarms(self):
        board = fake_board(mo=True)
        board.request_mo(False)
        tui.Board.disarm_mo(board)
        self.assertFalse(board.reclaim["mo_armed"])
        self.assertEqual(board.reclaim["note"], "")
        board.request_mo(False)  # 重新上膛，不会直接执行
        board.launch_mo.assert_not_called()

    def test_key_handler_disarms_on_everything_but_D(self):
        for key, disarmed in (("j", True), ("\x1b", True), ("\n", True), ("d", True), ("D", False)):
            with self.subTest(key=key):
                board = mock.MagicMock()
                board.request_mo.return_value = False
                tui._handle_reclaim_key(board, key)
                self.assertEqual(board.disarm_mo.called, disarmed)

    def test_mo_clean_refuses_when_binary_missing(self):
        with mock.patch.object(collect, "MO_BIN", "definitely-not-a-real-mo-binary"), \
                mock.patch.object(collect, "launch_task") as launch:
            with self.assertRaises(RuntimeError):
                collect.mo_clean(dry_run=False)
        launch.assert_not_called()


# ---------- 4. 有子进程的 shell 不是闲置 ----------


class TestShellWithChildren(ZhTestCase):
    ITEM = {"pane_id": "w1:p2", "kind": "window", "focused": False}
    INFO = {"shell_pid": 100, "foreground_processes": [{"pid": 100, "argv0": "-zsh", "cmdline": "-zsh"}]}

    def test_idle_shell_without_children_is_a_candidate(self):
        table = {100: (1, "zsh"), 300: (1, "other")}
        verdict, risk, _reason, _label = collect._reclaim_verdict(self.ITEM, self.INFO, None, table)
        self.assertEqual((verdict, risk), ("candidate", "shell"))

    def test_background_job_protects_the_shell(self):
        table = {100: (1, "zsh"), 200: (100, "node"), 201: (100, "/usr/bin/python3")}
        verdict, _risk, reason, _label = collect._reclaim_verdict(self.ITEM, self.INFO, None, table)
        self.assertEqual(verdict, "protected")
        self.assertIn("node", reason)
        self.assertIn("python3", reason)

    def test_unreadable_process_table_protects(self):
        verdict, _risk, reason, _label = collect._reclaim_verdict(self.ITEM, self.INFO, None, {})
        self.assertEqual((verdict, reason), ("protected", "进程信息不可用"))

    def test_scan_passes_the_process_table(self):
        snapshot = {"items": [dict(self.ITEM, tab_id="t", workspace_id="w", quiet_seconds=9999)], "focused": {}}
        with mock.patch.object(collect, "collect", return_value=snapshot), \
                mock.patch.object(collect, "_process_info_map", return_value={"w1:p2": self.INFO}), \
                mock.patch.object(collect, "_process_table", return_value={100: (1, "zsh"), 200: (100, "sleep")}):
            scan = collect.cleanup_scan(0)
        self.assertEqual(scan["candidates"], [])
        self.assertEqual(scan["protected"][0]["pane_id"], "w1:p2")


# ---------- 5. XSS：planStatusText 与 innerHTML ----------


class TestWebEscaping(unittest.TestCase):
    APP = (ROOT / "board" / "web" / "app.js").read_text(encoding="utf-8")

    def test_plan_status_is_escaped(self):
        self.assertIn("${esc(planStatusText(w.status))}", self.APP)
        self.assertNotRegex(self.APP, r"\$\{planStatusText\(")

    def test_catalog_text_in_html_templates_is_escaped(self):
        # 模板字符串里直接插 t(...) 而不 esc：目录内容也当数据
        self.assertNotRegex(self.APP, r"\$\{t\(")


# ---------- 3. 网页「释放选中」两步确认 ----------


class TestWebCleanupArming(unittest.TestCase):
    APP = (ROOT / "board" / "web" / "app.js").read_text(encoding="utf-8")

    def test_apply_arms_before_posting(self):
        body = self.APP.split("async function applyCleanup()", 1)[1].split("\n}\n", 1)[0]
        arm = body.index("armCleanup(")
        post = body.index('apiFetch("/api/cleanup"')
        self.assertLess(arm, post)
        self.assertIn("if (!state.cleanup.armed)", body)

    def test_arming_times_out_and_is_cancelled(self):
        self.assertIn("setTimeout(disarmCleanup, CLEANUP_ARM_MS)", self.APP)
        for fn in ("function updateCleanupSelected()", "async function loadCleanupScan()", "function closeCleanup()"):
            body = self.APP.split(fn, 1)[1].split("\n}\n", 1)[0]
            self.assertIn("disarmCleanup()", body, fn)


# ---------- 6. 以 - 开头的文本 ----------


class TestDashText(ZhTestCase):
    @staticmethod
    def snapshot(status: str) -> dict:
        return {"ok": True, "items": [{"pane_id": "wD:p1", "kind": "agent", "status": status, "agent_label": "OMP", "title": "t"}]}

    def test_dash_text_to_settled_agent_is_typed_not_prompted(self):
        with mock.patch.object(collect, "collect", return_value=self.snapshot("idle")), \
                mock.patch.object(collect, "run_herdr") as runner:
            result = collect.send_text("wD:p1", "-x continue")
        self.assertEqual(result["path"], "keys")
        self.assertEqual(
            runner.call_args_list,
            [
                mock.call("pane", "send-text", "wD:p1", "-x continue", timeout=10, allow_empty=True),
                mock.call("pane", "send-keys", "wD:p1", "enter", timeout=10, allow_empty=True),
            ],
        )

    def test_send_text_never_inserts_double_dash(self):
        with mock.patch.object(collect, "collect", return_value=self.snapshot("working")), \
                mock.patch.object(collect, "run_herdr") as runner:
            collect.send_text("wD:p1", "--help me")
        # herdr 会把单独的 -- 当文本发进 pane：参数里绝不能出现它
        self.assertTrue(runner.call_args_list)
        for call in runner.call_args_list:
            self.assertNotIn("--", call.args)
        self.assertEqual(runner.call_args_list[0].args, ("pane", "send-text", "wD:p1", "--help me"))

    def test_plain_text_still_uses_agent_prompt(self):
        with mock.patch.object(collect, "collect", return_value=self.snapshot("blocked")), \
                mock.patch.object(collect, "run_herdr") as runner:
            self.assertEqual(collect.send_text("wD:p1", "yes")["path"], "prompt")
        runner.assert_called_once_with("agent", "prompt", "wD:p1", "yes", timeout=20)

    def test_rename_rejects_leading_dash(self):
        for label in ("-x", "--clear", " --label foo"):
            with self.subTest(label=label), mock.patch.object(collect, "run_herdr") as runner:
                with self.assertRaises(ValueError):
                    collect.rename_target("tab", "w1:t1", label)
                runner.assert_not_called()

    def test_spawn_tab_strips_leading_dash_from_internal_label(self):
        with mock.patch.object(collect, "run_herdr", return_value={"result": {"root_pane": {"pane_id": "w1:p9"}, "tab": {"tab_id": "w1:t9"}}}) as runner:
            collect.spawn_tab(label="--weird")
        args = runner.call_args.args
        self.assertEqual(args[args.index("--label") + 1], "weird")


# ---------- 7. CI ----------


class TestCiPinning(unittest.TestCase):
    CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_actions_pinned_by_full_sha(self):
        uses = re.findall(r"uses:\s*(\S+)", self.CI)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"@[0-9a-f]{40}$", ref)

    def test_read_only_permissions(self):
        self.assertRegex(self.CI, r"(?m)^permissions:\n\s+contents: read")


# ---------- 8. token ----------


class TestToken(unittest.TestCase):
    def test_new_token_on_every_start_with_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "rt" / "token"
            with mock.patch.object(server, "TOKEN_FILE", token_file):
                first = server.load_token()
                second = server.load_token()
                self.assertNotEqual(first, second)
                self.assertTrue(server.TOKEN_RE.match(second))
                self.assertEqual(token_file.read_text(encoding="utf-8").strip(), second)
                self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
                self.assertFalse(token_file.with_name("token.tmp").exists())

    def test_open_web_uses_fragment(self):
        script = (ROOT / "scripts" / "open-web.sh").read_text(encoding="utf-8")
        self.assertIn('open_url="${url}#token=${token}"', script)
        self.assertNotIn("?token=", script)

    def test_web_reads_fragment_and_cleans_it(self):
        app = (ROOT / "board" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('location.hash.replace(/^#/, "")', app)
        self.assertIn('window.addEventListener("hashchange"', app)


# ---------- 9. Linux：xdg-open，不碰 macOS 专属命令 ----------


class TestLinuxOpen(unittest.TestCase):
    def test_open_web_linux_branch(self):
        script = (ROOT / "scripts" / "open-web.sh").read_text(encoding="utf-8")
        self.assertIn('xdg-open "$open_url"', script)
        self.assertNotRegex(script, r"(?m)^\s*open \"\$url\"")
        self.assertIn('[ "$os" = "Darwin" ]', script.split("osascript", 1)[0])

    def test_bring_to_front_is_macos_only(self):
        with mock.patch.object(collect.sys, "platform", "linux"), mock.patch.dict(os.environ, {"HERDR_PORTAL_NO_FRONT": ""}), \
                mock.patch.object(collect.subprocess, "run") as run:
            self.assertFalse(collect.bring_herdr_to_front())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
