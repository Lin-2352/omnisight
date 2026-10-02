<#
.SYNOPSIS
    Run the OmniSight inference node on this PC: on the NVIDIA GPU, or on the CPU when there is none.

.DESCRIPTION
    Creates (once) a uv-managed Python 3.12 virtual environment in .venv-gpu, installs the
    CUDA 12.1 PyTorch stack (it also runs on CPU-only machines) and the shared contracts,
    then starts kaggle-server\launch.py with --no-tunnel so the node listens on
    http://127.0.0.1:<Port> only. Nothing is published to the gist. Pick "Local node" in the
    OmniSight tray menu (Backend) to use it.

    The first start downloads the model from Hugging Face into %USERPROFILE%\.cache\huggingface
    (Qwen2-VL-2B ~4.5 GB, Qwen2-VL-7B ~16 GB) plus Whisper-base (~0.3 GB).

.PARAMETER Device
    auto (default: asks scripts\capability_report.py - GPU when it has enough free VRAM, else
    the CPU when there is enough free RAM), cuda, or cpu.

.PARAMETER Model
    2b (default; ~3.2 GB VRAM peak on a GPU) or 7b (GPU only; needs ~7.5 GB free VRAM).

.PARAMETER CpuDtype
    CPU weights: float32 (best answers, ~10.5 GB RAM), int8 (~7.5 GB RAM, faster but weaker answers)
    or bfloat16 (only fast on CPUs with native bf16 math). Default: what scripts\capability_report.py
    recommends for the free RAM.

.PARAMETER Port
    Local port (default 8000, which the desktop client expects).

.PARAMETER Reinstall
    Force reinstalling the Python packages.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run-local-gpu.ps1
    powershell -ExecutionPolicy Bypass -File scripts\run-local-gpu.ps1 -Model 7b
    powershell -ExecutionPolicy Bypass -File scripts\run-local-gpu.ps1 -Device cpu
#>
[CmdletBinding()]
param(
    [ValidateSet("auto", "cuda", "cpu")][string]$Device = "auto",
    [ValidateSet("2b", "7b")][string]$Model = "2b",
    [ValidateSet("bfloat16", "float32", "int8")][string]$CpuDtype = "float32",
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

# --- Python 3.12 venv via uv ---------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not found. Install it with:  powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}
if (-not (Test-Path $Python)) {
    Write-Step "Creating .venv-gpu with a uv-managed Python 3.12"
    & uv venv $Venv --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
}

# --- Pick the device (same rules as the desktop client's capability report) ---------------
if ($Device -eq "auto") {
    # Not assigned straight to $Device: that parameter is validated ("auto", "cuda", "cpu"), so storing "none" in it would
    # fail with a cryptic validation error instead of the explanation below.
    $picked = (& $Python (Join-Path $Root "scripts\capability_report.py") --device).Trim()
    if ($picked -eq "none") {
        & $Python (Join-Path $Root "scripts\capability_report.py")
        throw "Not enough free GPU memory or RAM for a local model. Close other programs or use the Kaggle backend."
    }
    $Device = $picked
    Write-Step "Auto-selected device: $Device"
}
if ($Device -eq "cpu" -and $Model -eq "7b") {
    throw "Qwen2-VL-7B is GPU-only here (about 30 GB of RAM unquantized); use -Model 2b on the CPU."
}

if ($Device -eq "cuda") {
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        throw "nvidia-smi not found: an NVIDIA GPU and driver are required for -Device cuda (use -Device cpu instead)."
    }
    $gpu = (& nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits | Select-Object -First 1).Split(",") | ForEach-Object { $_.Trim() }
    $freeMiB = [int]$gpu[2]
    Write-Step "GPU: $($gpu[0]), $($gpu[1]) MiB total, $freeMiB MiB free"
    $needMiB = if ($Model -eq "7b") { 7000 } else { 3500 }
    if ($freeMiB -lt $needMiB) {
        Write-Warning "Only $freeMiB MiB VRAM free; Qwen2-VL-$Model needs about $needMiB MiB. Close GPU-heavy apps or expect out-of-memory (HTTP 507)."
    }
} else {
    if (-not $PSBoundParameters.ContainsKey("CpuDtype")) {
        $CpuDtype = (& $Python (Join-Path $Root "scripts\capability_report.py") --cpu-dtype).Trim()
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $freeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
    Write-Step "CPU mode ($CpuDtype): $((Get-CimInstance Win32_Processor | Select-Object -First 1).Name), $freeGB GB RAM free"
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
    Write-Step "Installing the PyTorch stack (first run downloads ~2.5 GB)"
    & uv pip install --python $Python --index-strategy unsafe-best-match -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
    & uv pip install --python $Python --no-deps -e $Root
    if ($LASTEXITCODE -ne 0) { throw "contracts install failed" }
    Set-Content -Path $Stamp -Value $hash -NoNewline
}

if ($Device -eq "cuda") {
    & $Python -c "import torch, sys; ok = torch.cuda.is_available(); print('torch', torch.__version__, 'CUDA', torch.version.cuda, 'available:', ok); sys.exit(0 if ok else 1)"
    if ($LASTEXITCODE -ne 0) { throw "PyTorch cannot see the GPU (check the NVIDIA driver, or use -Device cpu)." }
}

# --- Node configuration ----------------------------------------------------------------
$env:OMNISIGHT_DEVICE = $Device
if ($Device -eq "cpu") {
    $env:OMNISIGHT_MODEL_ID = "Qwen/Qwen2-VL-2B-Instruct"
    $env:OMNISIGHT_CPU_DTYPE = $CpuDtype
    $env:OMNISIGHT_MAX_PIXELS = "$(896 * 504)"     # fewer vision tokens: prefill dominates on the CPU
    $env:OMNISIGHT_CPU_MAX_NEW_TOKENS = "256"
    $env:OMNISIGHT_GENERATION_TIMEOUT_S = "240"
    $env:OMNISIGHT_QUEUE_TIMEOUT_S = "300"
} elseif ($Model -eq "7b") {
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

Write-Step "Starting $($env:OMNISIGHT_MODEL_ID) on $Device at http://127.0.0.1:$Port (Ctrl+C to stop)"
Push-Location $Root
try {
    & $Python -u (Join-Path $Root "kaggle-server\launch.py") --no-tunnel
} finally {
    Pop-Location
}
