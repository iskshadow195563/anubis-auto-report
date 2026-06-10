$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    try {
        py -3 -m venv .venv
    } catch {
        python -m venv .venv
    }
}

& ".venv\Scripts\Activate.ps1"
python -m pip install --upgrade "pip>=26.1.2" "setuptools>=78.1.1"
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt

$env:PYTHONPATH = Join-Path $Root "src"
pyinstaller --clean --noconfirm --onefile --windowed --name "Anubis Auto Report" --paths $env:PYTHONPATH app.py

Write-Host ""
Write-Host "Windows exe created:"
Write-Host (Join-Path $Root "dist\Anubis Auto Report.exe")
Write-Host ""
Write-Host "Put .env next to the exe before running it."
