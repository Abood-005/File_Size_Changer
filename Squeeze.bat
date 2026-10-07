@echo off
rem Double-click to launch Squeeze without a console window.
cd /d "%~dp0"
start "" pythonw.exe app.py
