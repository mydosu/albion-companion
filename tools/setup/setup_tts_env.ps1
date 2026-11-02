<#
.SYNOPSIS
    Build the Irodori TTS environment (tts/irodori/.venv) on Windows.

.DESCRIPTION
    Wraps the commands in docs/REBUILD.md ("weights and Windows environment"):

        uv sync --frozen --extra cu128 --python 3.10
        uv pip install fastapi==... uvicorn==... httpx==... websockets==...

    Preflight:
      - uv must be on PATH;
      - nvidia-smi is checked and the GPUs are printed;
      - a non-RTX-50 card prints a warning: the lockfile/extra cu128 is pinned
        for the reference 50-series deployment, so confirm the wheel's CUDA
        version fits the installed driver before touching uv.lock.

    The service-layer versions come from config/rebuild/tts-environment.json
    when present (the snapshot of the reference machine), otherwise from
    docs/REBUILD.md.

    This script only creates/updates the environment. It never starts,
    restarts or stops a service. Use -DryRun to print the commands instead of
    running them; do not run it against production while the TTS backend is
    serving unless you intend to update that backend.

.PARAMETER ProjectDir
    Default: <repo>/tts/irodori

.PARAMETER Extra
    uv extra. Default: cu128 (reference deployment).

.PARAMETER Python
    Default: .python-version in the project (3.10), else 3.10.

.PARAMETER Manifest
    Default: <repo>/config/rebuild/tts-environment.json

.PARAMETER SkipServiceDeps
    Only run uv sync; skip the fastapi/uvicorn/httpx/websockets install.

.PARAMETER DryRun
    Print the commands without running them.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_tts_env.ps1 -DryRun

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_tts_env.ps1
#>
[CmdletBinding()]
param(
    [string]$ProjectDir,
    [string]$Extra = 'cu128',
    [string]$Python,
    [string]$Manifest,
    [switch]$SkipServiceDeps,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
trap {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
if (-not $ProjectDir) { $ProjectDir = Join-Path $RepoRoot 'tts\irodori' }
if (-not $Manifest) { $Manifest = Join-Path $RepoRoot 'config\rebuild\tts-environment.json' }

if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'pyproject.toml'))) {
    throw ('not an Irodori project directory (pyproject.toml missing): ' + $ProjectDir)
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'uv.lock'))) {
    throw ('uv.lock missing; do not sync a project without its lockfile: ' + $ProjectDir)
}

if (-not $Python) {
    $versionFile = Join-Path $ProjectDir '.python-version'
    if (Test-Path -LiteralPath $versionFile) {
        $Python = (Get-Content -LiteralPath $versionFile -TotalCount 1).Trim()
    }
    if (-not $Python) { $Python = '3.10' }
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    throw 'uv not found on PATH. Install it first (docs/REBUILD.md, e.g. "pip install uv" or winget), then rerun.'
}
Write-Host ('uv: ' + (& $uv --version))

$gpuRows = @()
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi) {
    Write-Warning 'nvidia-smi not found. The TTS backend needs an NVIDIA GPU with a CUDA 12.8 capable driver; install the driver before serving TTS.'
} else {
    $gpuRows = @(& $smi.Source --query-gpu=name,driver_version --format=csv,noheader)
    if ($LASTEXITCODE -ne 0 -or -not $gpuRows) {
        Write-Warning 'nvidia-smi did not report a GPU. The environment can still be built, but CUDA will not be usable.'
    } else {
        Write-Host 'GPU:'
        foreach ($row in $gpuRows) { Write-Host ('  ' + $row) }
        $series50 = @($gpuRows | Where-Object { $_ -match '(?i)RTX\s*50\d0' })
        if (-not $series50.Count) {
            Write-Warning 'No RTX 50-series GPU detected. uv.lock + --extra cu128 target the reference 50-series cards; on other cards confirm the wheel CUDA build matches your driver (nvidia-smi prints the max CUDA version) before syncing. Do not rewrite uv.lock in the production checkout.'
        } else {
            Write-Host 'RTX 50-series detected: cu128 matches the reference deployment.'
        }
    }
}

$pins = $null
if (Test-Path -LiteralPath $Manifest) {
    $snapshot = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $pins = @{}
    foreach ($prop in $snapshot.packages.PSObject.Properties) { $pins[$prop.Name] = [string]$prop.Value }
} else {
    Write-Warning ('manifest not found, using the versions from docs/REBUILD.md: ' + $Manifest)
}

function Get-Pin([string]$Name, [string]$Fallback) {
    if ($pins -and $pins.ContainsKey($Name)) { return ('{0}=={1}' -f $Name, $pins[$Name]) }
    return $Fallback
}

$serviceSpecs = @(
    (Get-Pin 'fastapi' 'fastapi==0.129.2'),
    (Get-Pin 'uvicorn' 'uvicorn==0.41.0'),
    (Get-Pin 'httpx' 'httpx'),
    (Get-Pin 'websockets' 'websockets==16.1.1')
)

$syncArgs = @('sync', '--frozen', '--extra', $Extra, '--python', $Python)
$venvPython = Join-Path $ProjectDir '.venv\Scripts\python.exe'
# uv-created venvs do not contain pip; use "uv pip install --python ..." for the
# service layer instead of the pip command written in docs/REBUILD.md.
$serviceArgs = @('pip', 'install', '--python', $venvPython) + $serviceSpecs

Write-Host ''
Write-Host ('project : ' + $ProjectDir)
Write-Host ('python  : ' + $Python + ' (extra: ' + $Extra + ')')
Write-Host ('services: ' + ($(if ($SkipServiceDeps) { 'skipped' } else { $serviceSpecs -join ' ' })))

if ($DryRun) {
    Write-Host ''
    Write-Host ('DRY-RUN: cd "' + $ProjectDir + '"')
    Write-Host ('DRY-RUN: "' + $uv + '" ' + ($syncArgs -join ' '))
    if (-not $SkipServiceDeps) {
        Write-Host ('DRY-RUN: "' + $uv + '" ' + ($serviceArgs -join ' '))
    }
    Write-Host 'DRY-RUN: nothing was run or changed.'
    exit 0
}

Push-Location -LiteralPath $ProjectDir
try {
    Write-Host ''
    Write-Host ('==> uv ' + ($syncArgs -join ' ')) -ForegroundColor Cyan
    & $uv @syncArgs
    if ($LASTEXITCODE -ne 0) { throw ('uv sync failed with exit code ' + $LASTEXITCODE) }

    if (-not $SkipServiceDeps) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw ('venv python not found after uv sync: ' + $venvPython)
        }
        Write-Host ''
        Write-Host ('==> uv ' + ($serviceArgs -join ' ')) -ForegroundColor Cyan
        & $uv @serviceArgs
        if ($LASTEXITCODE -ne 0) { throw ('uv pip install failed with exit code ' + $LASTEXITCODE) }
    }
} finally {
    Pop-Location
}

if (Test-Path -LiteralPath $venvPython) {
    Write-Host ''
    & $venvPython -c "import sys, torch; print('venv: python ' + sys.version.split()[0] + ' torch ' + torch.__version__ + ' cuda=' + str(torch.cuda.is_available()))"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'environment was built but "import torch" failed; check the CUDA wheel and VC runtime.'
    }
}
