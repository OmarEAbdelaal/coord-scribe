$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $exe" }
}
if (-not (Test-Path .venv\Scripts\python.exe)) { Run uv @("venv", "--python", "3.12") }
Run uv @("pip", "install", "--python", ".venv\Scripts\python.exe", "-e", ".[desktop,package,dev]")
Run .venv\Scripts\python.exe @("-m", "pytest", "-v", "-o", "faulthandler_timeout=120")
Run .venv\Scripts\pyinstaller.exe @("packaging/windows/coord.spec", "--noconfirm", "--clean")
$compiler = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $compiler)) { throw "Install Inno Setup 6 from https://jrsoftware.org/isinfo.php, then rerun. Portable app is in dist/Coord." }
Run $compiler @("packaging/windows/installer.iss")
Write-Host "Installer is ready in dist/installer. Weights download separately through the application."
