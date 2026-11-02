#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
host="${HERDR_PORTAL_HOST:-127.0.0.1}"
port="${HERDR_PORTAL_PORT:-8787}"
url="http://${host}:${port}/"
runtime="${HERDR_PORTAL_RUNTIME:-$HOME/.local/state/herdr-portal}"

# 提示文字走 board/i18n（键是中文原文，name=value 填占位符）；Python 不可用时原样输出键
i18n() {
  python3 "$root/board/i18n/__init__.py" "$@" 2>/dev/null || printf '%s\n' "$1"
}

if ! python3 "$root/board/daemon.py" start; then
  i18n "看板服务启动失败。"
  i18n "日志: {runtime}/server.log" runtime="$runtime"
  tail -n 20 "$runtime/server.log" 2>/dev/null || true
  exit 1
fi

# /api/* 需要 token：放进 URL 的 #token= 片段（片段不发给服务器、不进访问日志），
# 前端读一次就存 sessionStorage、把地址栏里的抹掉。服务每次启动都会换新 token。
token=""
if [ -r "$runtime/token" ]; then
  IFS= read -r token < "$runtime/token" || token=""
fi
# 只认 token_urlsafe 的字符，别把奇怪内容拼进 URL
case "$token" in
  *[!A-Za-z0-9_-]*) token="" ;;
esac
open_url="$url"
if [ -n "$token" ]; then
  open_url="${url}#token=${token}"
fi

os="$(uname -s 2>/dev/null || echo unknown)"
if [ "$os" = "Darwin" ] && [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  # 优先复用已打开的门户标签页，避免每次重复开新标签（减少内存占用）。
  # URL 一律通过 argv 传进去：拼进 AppleScript 源码的话，HERDR_PORTAL_HOST / PORT
  # 里带引号的内容会被当成 AppleScript 执行（do shell script 之类）。
  if osascript \
    -e 'on run argv' \
    -e 'set baseUrl to item 1 of argv' \
    -e 'set openUrl to item 2 of argv' \
    -e 'tell application "Google Chrome"' \
    -e 'set found to false' \
    -e 'repeat with w in windows' \
    -e 'repeat with t in tabs of w' \
    -e 'if URL of t starts with baseUrl then' \
    -e 'set URL of t to openUrl' \
    -e 'set active tab index of w to index of t' \
    -e 'set index of w to 1' \
    -e 'set found to true' \
    -e 'exit repeat' \
    -e 'end if' \
    -e 'end repeat' \
    -e 'if found then exit repeat' \
    -e 'end repeat' \
    -e 'if not found then' \
    -e 'open location openUrl' \
    -e 'end if' \
    -e 'activate' \
    -e 'end tell' \
    -e 'end run' -- "$url" "$open_url" >/dev/null 2>&1; then
    :
  else
    open "$open_url"
  fi
elif [ "$os" = "Darwin" ]; then
  open "$open_url"
elif command -v xdg-open >/dev/null 2>&1; then
  # Linux 等：只用 xdg-open（这里的 open 可能是 openvt），不碰 osascript / open -a
  xdg-open "$open_url" >/dev/null 2>&1 || true
fi

i18n "HERDR 全局看板已打开"
printf '\n  %s\n\n' "$url"
i18n "服务在后台运行，刷新间隔 1.5s。"
if [ -t 0 ]; then
  i18n "按任意键关闭这个提示窗（看板网页会继续开着）。"
  read -rsn1 || true
fi
