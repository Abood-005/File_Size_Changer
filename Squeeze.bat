@echo off
rem Double-click to launch Squeeze without a console window.
rem Uses the project venv (.venv, Python 3.12) when present: the Tk bundled
rem with Python 3.15 hangs on import on this machine, so a global pythonw
rem would start and then sit invisibly with no window.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" app.py
) else (
    echo .venv not found. Create it with:
    echo     uv venv --python 3.12 .venv
    echo     uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    pause
)
