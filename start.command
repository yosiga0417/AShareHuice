#!/usr/bin/env bash
# macOS 双击入口：在 Finder 里双击本文件即可启动（需要先在终端执行过一次
# chmod +x start.command start.sh，或由 git 保留可执行位）。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$ROOT/start.sh" "$@"
status=$?
if [ "$status" -ne 0 ]; then
  echo
  echo "启动失败（退出码 $status），按回车键关闭窗口。"
  read -r _
fi
