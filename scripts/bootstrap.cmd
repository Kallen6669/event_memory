@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" goto ready
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if errorlevel 1 (
  echo 请先安装 Python 3.10 或以上版本，并勾选加入 PATH。
  exit /b 1
)
python -m venv .venv
if errorlevel 1 exit /b 1
:ready
".venv\Scripts\python.exe" -c "import jwt, cryptography" >nul 2>&1
if not errorlevel 1 goto launch
echo 首次安装运行依赖，需要联网；不会调用大模型。
".venv\Scripts\python.exe" -m pip install -e ".[chatgpt]"
if errorlevel 1 exit /b 1
:launch
".venv\Scripts\python.exe" -B -X utf8 scripts\handoff.py %*
exit /b %errorlevel%
