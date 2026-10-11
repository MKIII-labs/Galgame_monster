@echo off
rem Galgame_monster launcher - starts the app without a console window.
rem Self-locating (%~dp0), so it keeps working if the project folder is moved.
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=pythonw.exe"
start "" /D "%ROOT%" "%PY%" "%ROOT%main.py"
endlocal
