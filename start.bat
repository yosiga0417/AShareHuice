@echo off
rem ════════════════════════════════════════════════════════════
rem  A股自设指数净值回测工具 —— Windows 一键启动
rem  双击本文件即可：安装依赖（如缺失）→ 启动后端 → 打开浏览器
rem  停止：关闭本窗口，或按 Ctrl+C
rem ════════════════════════════════════════════════════════════
setlocal
cd /d "%~dp0"
set "PORT=%BACKTEST_PORT%"
if "%PORT%"=="" set "PORT=8000"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python，请先安装 Python 3.9+ 并勾选 "Add to PATH"。
  pause
  exit /b 1
)

python -c "import fastapi, uvicorn, pandas, numpy, akshare, pyarrow, openpyxl, xlrd, multipart, pydantic" >nul 2>nul
if errorlevel 1 (
  echo [提示] 检测到依赖缺失，正在安装（首次可能较慢）...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请手动执行: python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

echo ──────────────────────────────────────────────
echo  A股自设指数净值回测工具
echo  服务地址: http://127.0.0.1:%PORT%/
echo  Ctrl+C 或关闭本窗口即可停止服务。
echo ──────────────────────────────────────────────

start /b cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:%PORT%/"
python -m uvicorn backend:app --host 127.0.0.1 --port %PORT% --log-level info

echo.
echo 服务已停止。
pause
