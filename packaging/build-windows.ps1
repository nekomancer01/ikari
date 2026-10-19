# Builds the Windows app on a Windows machine:
#   dist\SubredditSentiment\SubredditSentiment.exe       (the app folder)
#   dist\SubredditSentiment-windows-portable.zip          (that folder, zipped)
#   dist\SubredditSentiment-Setup-<version>.exe          (installer, if Inno Setup 6 is installed)
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
    & $py -m venv .venv-build
}
$vpy = ".venv-build\Scripts\python.exe"
& $vpy -m pip install --upgrade pip
& $vpy -m pip install . pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $vpy -m PyInstaller packaging\subsentiment.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Compress-Archive -Path dist\SubredditSentiment -DestinationPath dist\SubredditSentiment-windows-portable.zip -Force

$iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    & $iscc packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
} else {
    Write-Host "Inno Setup 6 not found; skipping the installer (https://jrsoftware.org/isinfo.php)."
}
Write-Host "`nDone. Run dist\SubredditSentiment\SubredditSentiment.exe"
