<#
.SYNOPSIS
    Run the OmniSight inference node on this PC's NVIDIA GPU (the "Local GPU" backend).

.DESCRIPTION
    Creates (once) a uv-managed Python 3.12 virtual environment in .venv-gpu, installs the
    CUDA 12.1 PyTorch stack and the shared contracts, then starts kaggle-server\launch.py
    with --no-tunnel so the node listens on http://127.0.0.1:<Port> only. Nothing is
    published to the gist. Pick "Local GPU" in the OmniSight tray menu (Backend) to use it.

    The first start downloads the model from Hugging Face into %USERPROFILE%\.cache\huggingface
    (Qwen2-VL-2B ~4.5 GB, Qwen2-VL-7B ~16 GB) plus Whisper-base (~0.3 GB).

.PARAMETER Model
    2b (default, ~2 GB VRAM, fits 6 GB+ GPUs) or 7b (~6 GB VRAM at load; needs ~8 GB free).

.PARAMETER Port
    Local port (default 8000, which the desktop client expects).

.PARAMETER Reinstall
    Force reinstalling the Python packages.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run-local-gpu.ps1
    powershell -ExecutionPolicy Bypass -File scripts\run-local-gpu.ps1 -Model 7b
#>
[CmdletBinding()]
param(
    [ValidateSet("2b", "7b")][string]$Model = "2b",
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv-gpu"
$Python = Join-Path $Venv "Scripts\python.exe"
$Requirements = Join-Path $Root "kaggle-server\requirements-local-gpu.txt"
$Stamp = Join-Path $Venv ".omnisight-requirements.sha256"

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

# --- GPU check -------------------------------------------------------------------
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    throw "nvidia-smi not found: an NVIDIA GPU and driver are required for the local backend."
}
$gpu = (& nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits | Select-Object -First 1).Split(",") | ForEach-Object { $_.Trim() }
$freeMiB = [int]$gpu[2]
Write-Step "GPU: $($gpu[0]), $($gpu[1]) MiB total, $freeMiB MiB free"
$needMiB = if ($Model -eq "7b") { 7000 } else { 3500 }
if ($freeMiB -lt $needMiB) {
    Write-Warning "Only $freeMiB MiB VRAM free; Qwen2-VL-$Model needs about $needMiB MiB. Close GPU-heavy apps or expect out-of-memory (HTTP 507)."
}

# --- Python 3.12 venv via uv ---------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not found. Install it with:  powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}
if (-not (Test-Path $Python)) {
    Write-Step "Creating .venv-gpu with a uv-managed Python 3.12"
    & uv venv $Venv --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
}

# .NET directly: Get-FileHash is missing when PowerShell's module path is incomplete.
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $hash = [System.BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($Requirements))).Replace("-", "")
} finally {
    $sha.Dispose()
}
$installed = if (Test-Path $Stamp) { Get-Content $Stamp -Raw } else { "" }
if ($Reinstall -or $installed.Trim() -ne $hash) {
    Write-Step "Installing CUDA PyTorch stack (first run downloads ~2.5 GB)"
    & uv pip install --python $Python --index-strategy unsafe-best-match -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
    & uv pip install --python $Python --no-deps -e $Root
    if ($LASTEXITCODE -ne 0) { throw "contracts install failed" }
    Set-Content -Path $Stamp -Value $hash -NoNewline
}

& $Python -c "import torch, sys; ok = torch.cuda.is_available(); print('torch', torch.__version__, 'CUDA', torch.version.cuda, 'available:', ok); sys.exit(0 if ok else 1)"
if ($LASTEXITCODE -ne 0) { throw "PyTorch cannot see the GPU (check the NVIDIA driver)." }

# --- Node configuration ----------------------------------------------------------------
if ($Model -eq "7b") {
    $env:OMNISIGHT_MODEL_ID = "Qwen/Qwen2-VL-7B-Instruct"
    $env:OMNISIGHT_QUANTIZE_LM_HEAD = "1"          # saves ~0.8 GB on 8 GB laptop GPUs
    $env:OMNISIGHT_MAX_PIXELS = "$(1024 * 576)"    # fewer vision tokens, lower peak VRAM
    $env:OMNISIGHT_BASELINE_BUDGET_GB = "5.8"
    $env:OMNISIGHT_VRAM_CEILING_GB = "7.5"
} else {
    $env:OMNISIGHT_MODEL_ID = "Qwen/Qwen2-VL-2B-Instruct"
    $env:OMNISIGHT_BASELINE_BUDGET_GB = "3.0"
    $env:OMNISIGHT_VRAM_CEILING_GB = "6.0"
}
$env:OMNISIGHT_PORT = "$Port"
$env:OMNISIGHT_ASR_PRELOAD = "1"
$env:OMNISIGHT_KEEPALIVE_INTERVAL_S = "3600"       # anti-idle is a Kaggle concern
$env:PYTHONUTF8 = "1"

Write-Step "Starting $($env:OMNISIGHT_MODEL_ID) on http://127.0.0.1:$Port (Ctrl+C to stop)"
Push-Location $Root
try {
    & $Python -u (Join-Path $Root "kaggle-server\launch.py") --no-tunnel
} finally {
    Pop-Location
}
