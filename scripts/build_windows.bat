@echo off
setlocal

cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade "pip>=26.1.2" "setuptools>=78.1.1"
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt

set PYTHONPATH=%CD%\src
pyinstaller --clean --noconfirm --onefile --windowed --name "Anubis Auto Report" --paths "%CD%\src" app.py

echo.
echo Windows exe created:
echo %CD%\dist\Anubis Auto Report.exe
echo.
echo Put .env next to the exe before running it.
pause
