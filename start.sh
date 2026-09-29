#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  A股自设指数净值回测工具 —— 一键启动脚本
#
#  用法：
#    ./start.sh               启动后端并自动打开浏览器（http://127.0.0.1:8000/）
#    ./start.sh --file        启动后端并打开本地 index.html（保留 file:// 的既有存档）
#    ./start.sh --port 8080   指定端口
#    ./start.sh --no-open     只启动服务，不打开浏览器
#    ./start.sh --help        查看帮助
#
#  停止：在本窗口按 Ctrl+C，或直接关闭终端窗口。
# ════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

PORT="${BACKTEST_PORT:-8000}"
MODE="serve"     # serve = 通过 http 打开页面；file = 打开本地 index.html
OPEN_BROWSER=1
REQ_FILE="$ROOT/requirements.txt"

usage() {
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --file)     MODE="file" ;;
    --serve)    MODE="serve" ;;
    --no-open)  OPEN_BROWSER=0 ;;
    --port)     PORT="${2:-}"; shift ;;
    --port=*)   PORT="${1#*=}" ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
  esac
  shift
done

case "$PORT" in
  ''|*[!0-9]*) echo "端口无效：$PORT" >&2; exit 2 ;;
esac

URL="http://127.0.0.1:${PORT}"
PAGE_URL="$URL/"
[ "$MODE" = "file" ] && PAGE_URL="$ROOT/index.html"

# ── 1. 找一个可用的 Python ──────────────────────────────────────
find_python() {
  local cand
  for cand in "$ROOT/.venv/bin/python" "${VIRTUAL_ENV:-}/bin/python" python3 python; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1; then printf '%s' "$cand"; return 0; fi
  done
  return 1
}

if ! PY="$(find_python)"; then
  echo "✗ 未找到 Python，请先安装 Python 3.9+：https://www.python.org/downloads/" >&2
  exit 1
fi

# ── 2. 检查依赖，缺失时自动安装 ─────────────────────────────────
deps_ok() {
  "$PY" - >/dev/null 2>&1 <<'PYEOF'
import importlib
for name in ("fastapi", "uvicorn", "pandas", "numpy", "akshare",
             "pyarrow", "openpyxl", "xlrd", "multipart", "pydantic"):
    importlib.import_module(name)
PYEOF
}

if ! deps_ok; then
  echo "→ 检测到依赖缺失，正在安装（首次安装可能需要几分钟）..."
  if ! "$PY" -m pip install -r "$REQ_FILE"; then
    echo "→ 直接安装失败，改为创建虚拟环境 .venv 后安装 ..." >&2
    if [ ! -x "$ROOT/.venv/bin/python" ]; then
      "$PY" -m venv "$ROOT/.venv" || { echo "✗ 创建虚拟环境失败" >&2; exit 1; }
    fi
    PY="$ROOT/.venv/bin/python"
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r "$REQ_FILE" || { echo "✗ 依赖安装失败，请手动执行：$PY -m pip install -r requirements.txt" >&2; exit 1; }
  fi
  deps_ok || { echo "✗ 依赖仍不完整，请检查上面的安装日志。" >&2; exit 1; }
fi

# ── 3. 小工具：健康检查 / 端口占用 / 打开浏览器 ─────────────────
health_ok() {
  # 显式禁用系统代理：macOS 的系统代理（如 Clash/Surge）会让 127.0.0.1 的请求
  # 也走代理，导致健康检查误判为"服务未启动"。浏览器访问 localhost 默认直连。
  "$PY" - "$1" >/dev/null 2>&1 <<'PYEOF'
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(sys.argv[1], timeout=2) as response:
    data = json.load(response)
sys.exit(0 if isinstance(data, dict) and data.get("status") == "ok"
         and data.get("data_source") == "AKShare"
         and data.get("frontend_served") is True else 1)
PYEOF
}

port_busy() {
  "$PY" - "$1" >/dev/null 2>&1 <<'PYEOF'
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYEOF
}

open_page() {
  local target="$1"
  if [ "$OPEN_BROWSER" != "1" ]; then
    echo "→ 请手动在浏览器打开：$target"
    return 0
  fi
  case "$(uname -s)" in
    Darwin)
      open "$target" || echo "→ 请手动在浏览器打开：$target"
      ;;
    Linux)
      if command -v xdg-open >/dev/null 2>&1; then xdg-open "$target" >/dev/null 2>&1 || true
      else echo "→ 请手动在浏览器打开：$target"; fi
      ;;
    *)
      echo "→ 请手动在浏览器打开：$target"
      ;;
  esac
}

# ── 4. 已经在运行就直接开页面 ───────────────────────────────────
if health_ok "$URL/api/health"; then
  echo "✓ 服务已经在运行：$URL"
  open_page "$PAGE_URL"
  exit 0
fi

if port_busy "$PORT"; then
  echo "✗ 端口 $PORT 已被其他程序占用，请用 --port 换一个端口，例如：./start.sh --port 8010" >&2
  exit 1
fi

# ── 5. 后台等待服务就绪并打开浏览器，前台运行服务 ───────────────
# 下面的 exec 会让服务进程沿用当前 shell 的 PID，等待循环据此判断服务是否还活着，
# 避免服务已经退出后这里还残留 120 秒的等待。
MAIN_PID=$$
(
  i=0
  while [ "$i" -lt 240 ]; do
    kill -0 "$MAIN_PID" 2>/dev/null || exit 0
    if health_ok "$URL/api/health"; then
      open_page "$PAGE_URL"
      exit 0
    fi
    sleep 0.5
    i=$((i + 1))
  done
  echo "⚠️  等待后端启动超时（120 秒），请查看上面的日志。"
  echo "    也可手动访问：$PAGE_URL"
) &

echo "──────────────────────────────────────────────"
echo " A股自设指数净值回测工具"
echo " 服务地址：$URL"
if [ "$MODE" = "file" ]; then
  echo " 打开页面：$ROOT/index.html"
  echo " 提醒：本地页面不会自动跟随端口，若页面里的「后端地址」不是 ${URL}，"
  echo "       请在「高级设置 → 后端地址」中改成 ${URL}。"
fi
echo " 首次回测需拉取历史行情，耗时较长；Ctrl+C 或关闭本窗口即可停止服务。"
echo "──────────────────────────────────────────────"

exec "$PY" -m uvicorn backend:app --host 127.0.0.1 --port "$PORT" --log-level info
