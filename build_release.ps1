$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $python -m PyInstaller --noconfirm --onedir --windowed --uac-admin --name GenshinCollectMarker --collect-all playwright --collect-data tzdata capture_app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
& $python package_assets.py
if ($LASTEXITCODE -ne 0) { throw 'Asset packaging failed' }
