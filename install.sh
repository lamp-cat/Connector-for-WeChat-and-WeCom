#!/bin/bash
# 消息工作台 · 从源码安装（开发者用）。普通用户请直接下载 .dmg，双击打开即可，首次启动会自动配置。
# 只做可自动化的部分；密钥抓取若需要，会在 App 窗口内引导。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
echo "==> 环境检查"
[[ "$(uname)" == "Darwin" ]] || { echo "只支持 macOS"; exit 1; }
command -v swiftc >/dev/null || { echo "缺 swiftc，请先装 Xcode Command Line Tools: xcode-select --install"; exit 1; }
command -v python3 >/dev/null || { echo "缺 python3"; exit 1; }
echo "==> 编译 App（内嵌读取器与网关）"
bash "$REPO/app/build.sh"
echo "==> 指向本仓库（可选；不设的话 App 会把运行时装到 ~/.local/share/wechatdesk/runtime）"
defaults write com.gly.wechatdesk PlatformRoot "$REPO" || true
echo
echo "完成。打开：open \"$REPO/app/dist/微信与企业微信聊天伴侣.app\""
echo "首次启动准备公共网关；在设置页切换个人微信或企业微信并连接。"
echo "AI 设置共用；企业微信 MCP 和各账号消息归档默认关闭，需要分别开启。"
