<#
Pull the latest code, build the Windows app and start it, for testing.
Run from the repo root via dev-windows.bat, or: powershell -File packaging\dev-windows.ps1

  -Branch main   branch to pull (default: the one that's checked out)
  -NoPull        build what's on disk, skip git
  -Test          run the test suite before building
  -NoLaunch      don't start the app; open dist\ in Explorer instead
#>
param(
    [string]$Branch,
    [switch]$NoPull,
    [switch]$Test,
    [switch]$NoLaunch
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

if (-not $NoPull) {
    Step "Pulling the latest code"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git is not installed: https://git-scm.com/download/win" }
    if ($Branch) { git checkout $Branch; Check "git checkout" }
    git pull --ff-only; Check "git pull"
}

Step "Setting up the build environment (.venv-build)"
$py = ".venv-build\Scripts\python.exe"
if (-not (Test-Path $py)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv-build } else { python -m venv .venv-build }
    if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is needed: https://www.python.org/downloads/ (tick 'Add python.exe to PATH')" }
}
& $py -m pip install --upgrade pip --quiet; Check "pip upgrade"
& $py -m pip install -e ".[dev]" pyinstaller --quiet; Check "pip install"

if ($Test) {
    Step "Running tests"
    & $py -m pytest -q; Check "tests"
}

Step "Building"
& $py packaging\build.py; Check "build"

$exe = "dist\ikari\ikari.exe"
if (-not (Test-Path $exe)) { throw "$exe was not produced" }
Write-Host "`nBuilt: $((Resolve-Path $exe).Path)" -ForegroundColor Green
Get-ChildItem dist -Filter "ikari-*" -File | ForEach-Object { Write-Host "       $($_.FullName)" }

if ($NoLaunch) { if ($Host.Name -eq "ConsoleHost" -and -not $env:CI) { Invoke-Item dist } }
else { Start-Process $exe }
