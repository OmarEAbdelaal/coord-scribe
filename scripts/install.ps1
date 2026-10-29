<#
.SYNOPSIS
  Installs Coord on Windows 11 with NVIDIA GPU support.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoDiarize
#>
param(
    [switch]$NoDiarize,          # skip speaker detection (no PyTorch, ~3 GB smaller)
    [string]$Python = "3.12"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Run($exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $exe $($arguments -join ' ')" }
}

Step "Checking uv (Python package manager)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv from astral.sh ..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Run uv @("--version")

Step "Checking NVIDIA driver"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
} else {
    Write-Warning "nvidia-smi not found. Install the latest NVIDIA Studio/Game Ready driver for the RTX 5080."
}

Step "Creating virtual environment (.venv, Python $Python)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { Run uv @("venv", "--python", $Python, ".venv") }
$py = (Resolve-Path ".venv\Scripts\python.exe").Path

$needGpuExtra = $true
if (-not $NoDiarize) {
    Step "Installing PyTorch with CUDA (RTX 50-series needs CUDA 12.8 or newer)"
    # A CUDA 12 torch build also provides the cuBLAS/cuDNN DLLs that faster-whisper needs.
    $installed = $false
    foreach ($cuda in @("cu128", "cu129", "cu130")) {
        Write-Host "Trying https://download.pytorch.org/whl/$cuda ..."
        & uv pip install --python $py torch torchaudio --index-url "https://download.pytorch.org/whl/$cuda"
        if ($LASTEXITCODE -eq 0) {
            $installed = $true
            $needGpuExtra = -not $cuda.StartsWith("cu12")
            break
        }
    }
    if (-not $installed) { throw "Could not install CUDA PyTorch. Check your internet connection and try again." }
}

Step "Installing Coord"
$extras = @()
if (-not $NoDiarize) { $extras += "diarize" }
if ($needGpuExtra) { $extras += "gpu" }
$target = if ($extras.Count) { ".[" + ($extras -join ",") + "]" } else { "." }
Run uv @("pip", "install", "--python", $py, "-e", $target)

Step "Creating your Coordy folder"
Run $py @("-m", "coordy", "init")

Step "Health check"
& $py -m coordy doctor

Write-Host "`nInstalled. Next steps:" -ForegroundColor Green
Write-Host "  1. Install Ollama (https://ollama.com/download) if you have not."
Write-Host "  2. Accept the speaker model terms: https://huggingface.co/pyannote/speaker-diarization-community-1"
Write-Host "     and put a read token in $env:USERPROFILE\Coordy\config.toml -> [diarization] hf_token"
Write-Host "  3. .venv\Scripts\coordy setup-models"
Write-Host "  4. Edit $env:USERPROFILE\Coordy\aliases.toml (names to hide)"
Write-Host "  5. .venv\Scripts\coordy claude-config --write   then fully restart Claude Desktop"
