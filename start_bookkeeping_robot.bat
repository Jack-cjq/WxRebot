@echo off
setlocal
cd /d "%~dp0"

python examples\messaging\start_bookkeeping_robot.py

if errorlevel 1 (
  echo.
  echo 启动失败，请先检查 Python 环境和依赖是否安装完成。
  pause
)
