@echo off
rem gm —— Galgame_monster 的 CLI 入口（把仓库目录加进 PATH 后可以直接敲 gm）
rem 自定位，项目目录换了也能用；优先用项目自带的 venv。
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%gm.py" %*
endlocal
