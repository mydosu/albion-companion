<#
.SYNOPSIS
    Rebuild tts/GPT-sovits/runtime: the Python 3.9 interpreter used by the ear
    (pet/whisper_serve.py) and by the eye launcher on Windows.

.DESCRIPTION
    The reference deployment has an embeddable CPython 3.9.13 at
    tts/GPT-sovits/runtime/python.exe, but nothing in the repository recreated
    it. This script does, using config/rebuild/stt-environment.json:

      1. refuses to touch an existing runtime unless -Force is given;
      2. obtains CPython 3.9.13 (official embeddable zip by default, or a venv
         made from a locally installed 3.9 with -Source venv);
      3. bootstraps pip;
      4. installs the minimal import set of the ear + eyes. The eyes
         (pet/tools/eyes/*.py) use only the standard library and .NET. The ear
         imports uvicorn, fastapi (+starlette), funasr, faster_whisper,
         ctranslate2 and python-multipart; torch/torchaudio/numpy are pinned
         because funasr needs them at runtime. Packages are installed one at a
         time; a package without a wheel for this platform is reported and
         skipped instead of aborting the whole install;
      5. with -Full, replays the whole 252-entry snapshot the same way.

    Nothing is started. The ear/eye launchers keep using the runtime path, so
    run this while the ear and eye capture agent are stopped (they are stopped
    by default only when you choose to restart them, see docs/REBUILD.md).

.PARAMETER Root
    Destination directory. Default: <repo>/tts/GPT-sovits/runtime

.PARAMETER Source
    embed (default), venv, or auto. venv needs a locally installed 3.9
    (py -3.9 or uv python find 3.9).

.PARAMETER PythonVersion
    Default: the "python" value in the manifest (3.9.13).

.PARAMETER Manifest
    Default: <repo>/config/rebuild/stt-environment.json

.PARAMETER Force
    Move an existing runtime aside to runtime.bak-<timestamp> and rebuild.
    Without this switch the script only inspects and exits.

.PARAMETER Full
    Install all 252 packages from the snapshot instead of the minimal set.
    Failures are still reported per package and do not abort the loop.

.PARAMETER TorchWheel
    Local torch wheel (e.g. a cu128 wheel downloaded elsewhere). Otherwise the
    pinned torch==2.7.0+cu128 is taken from -TorchIndex.

.PARAMETER TorchIndex
    Extra index used for torch-family packages.
    Default: https://download.pytorch.org/whl/cu128

.PARAMETER PipIndex
    Optional --index-url override (mirror).

.PARAMETER SkipPackages
    Only create the interpreter and pip, install nothing (useful for testing
    the bootstrap).

.PARAMETER DryRun
    Print the plan without downloading, moving, writing or installing anything.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1 -DryRun

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1 -Force -Full
#>
[CmdletBinding()]
param(
    [string]$Root,
    [ValidateSet('embed', 'venv', 'auto')][string]$Source = 'embed',
    [string]$PythonVersion,
    [string]$Manifest,
    [switch]$Force,
    [switch]$Full,
    [string]$TorchWheel,
    [string]$TorchIndex = 'https://download.pytorch.org/whl/cu128',
    [string]$PipIndex,
    [switch]$SkipPackages,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
trap {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
if (-not $Root) { $Root = Join-Path $RepoRoot 'tts\GPT-sovits\runtime' }
if (-not $Manifest) { $Manifest = Join-Path $RepoRoot 'config\rebuild\stt-environment.json' }
if (-not (Test-Path -LiteralPath $Manifest)) { throw ('manifest not found: ' + $Manifest) }

$snapshot = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$pins = @{}
foreach ($prop in $snapshot.packages.PSObject.Properties) { $pins[$prop.Name] = [string]$prop.Value }
if (-not $PythonVersion) {
    $PythonVersion = [string]$snapshot.python
    if (-not $PythonVersion) { $PythonVersion = '3.9.13' }
}
$ConstraintFile = Join-Path ([IO.Path]::GetTempPath()) ('albion-stt-constraints-{0}.txt' -f $PID)

# Minimal import set, taken by grepping the files below (2026-11-02):
#   pet/whisper_serve.py : uvicorn, fastapi (+starlette), funasr, faster_whisper,
#                          ctranslate2, UploadFile/Form => python-multipart
#   pet/tools/eyes/*.py  : standard library only (http.server, subprocess, ...)
# torch/torchaudio/numpy are runtime dependencies of funasr and are pinned in
# the manifest; without them "from funasr import AutoModel" fails at import.
$CorePackages = @(
    'torch',
    'torchaudio',
    'numpy',
    'funasr',
    'faster-whisper',
    'ctranslate2',
    'fastapi',
    'uvicorn',
    'python-multipart'
)

# import name = distribution name (python-multipart 0.0.9 exposes "multipart")
$CoreImports = [ordered]@{
    'torch'          = 'torch'
    'torchaudio'     = 'torchaudio'
    'funasr'         = 'funasr'
    'faster_whisper' = 'faster_whisper'
    'ctranslate2'    = 'ctranslate2'
    'fastapi'        = 'fastapi'
    'uvicorn'        = 'uvicorn'
    'python-multipart' = 'multipart'
}

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('==> ' + $Text) -ForegroundColor Cyan
}

function Write-Info([string]$Text) {
    Write-Host ('    ' + $Text)
}

function ConvertTo-Argument([string]$Value) {
    # Windows command-line quoting for ProcessStartInfo.Arguments (same rules as
    # the C runtime): wrap in quotes, escape backslashes before a quote.
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') { $backslashes++; continue }
        if ($ch -eq '"') {
            [void]$builder.Append(('\' * (2 * $backslashes + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($ch)
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * (2 * $backslashes))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-External([string]$Exe, [string[]]$Arguments) {
    Write-Info ($Exe + ' ' + ($Arguments -join ' '))
    # No redirection: native stderr goes straight to the console and does not
    # become a terminating PowerShell error under $ErrorActionPreference=Stop
    # (Windows PowerShell 5.1 turns redirected native stderr into one).
    # Out-Host keeps stdout out of the return value; callers expect one exit code.
    & $Exe @Arguments | Out-Host
    return $LASTEXITCODE
}

function Invoke-Capture([string]$Exe, [string[]]$Arguments) {
    # Quiet helper for probes: captures stdout/stderr without touching the
    # PowerShell error stream.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Exe
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $startInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-Argument $_ }) -join ' ')
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    return [pscustomobject]@{
        Code   = $process.ExitCode
        Output = @((([string]$stdout.Result) -split "`r?`n") | Where-Object { $_ -ne '' })
        Error  = [string]$stderr.Result
    }
}

function Get-PipSpec([string]$Name) {
    if ($pins.ContainsKey($Name)) { return ('{0}=={1}' -f $Name, $pins[$Name]) }
    return $Name
}

function Invoke-PipInstall([string]$Spec, [bool]$UseConstraints, [switch]$NoDeps) {
    $arguments = @($Spec)
    if ($UseConstraints -and (Test-Path -LiteralPath $ConstraintFile)) {
        $arguments += @('-c', $ConstraintFile)
    }
    if ($NoDeps) { $arguments += '--no-deps' }
    if ($Spec -match '(?i)^torch' -and $TorchIndex) {
        $arguments += @('--extra-index-url', $TorchIndex)
    }
    if ($PipIndex) { $arguments += @('--index-url', $PipIndex) }
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--progress-bar', 'off') + $arguments
    return Invoke-External -Exe $script:PythonExe -Arguments $pipArgs
}

function Find-BasePython([string]$Version) {
    $majorMinor = (($Version -split '\.')[0..1]) -join '.'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $probe = Invoke-Capture -Exe 'py' -Arguments @(('-' + $majorMinor), '-c', 'import sys; print(sys.executable)')
        $lines = @($probe.Output | Where-Object { $_ -and $_.Trim() })
        if ($probe.Code -eq 0 -and $lines.Count) {
            $path = [string]$lines[-1].Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    }
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $probe = Invoke-Capture -Exe 'uv' -Arguments @('python', 'find', $majorMinor)
        $lines = @($probe.Output | Where-Object { $_ -and $_.Trim() })
        if ($probe.Code -eq 0 -and $lines.Count) {
            $path = [string]$lines[-1].Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    }
    return $null
}

function Install-VenvRuntime([string]$BasePython) {
    Write-Step ('creating a venv at ' + $Root + ' from ' + $BasePython)
    $code = Invoke-External -Exe $BasePython -Arguments @('-m', 'venv', '--copies', $Root)
    if ($code -ne 0) { throw ('venv creation failed with exit code ' + $code) }
    # A venv keeps its launchers under Scripts\, but the launchers in this repo
    # call runtime\python.exe. Copying them next to pyvenv.cfg keeps that path.
    foreach ($name in @('python.exe', 'pythonw.exe')) {
        $src = Join-Path $Root ('Scripts\' + $name)
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $Root $name) -Force
        }
    }
}

function Install-EmbeddableRuntime([string]$Version) {
    $zipName = 'python-' + $Version + '-embed-amd64.zip'
    $url = 'https://www.python.org/ftp/python/' + $Version + '/' + $zipName
    $cache = Join-Path ([IO.Path]::GetTempPath()) $zipName
    Write-Step ('downloading and extracting ' + $url)
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = 'SilentlyContinue'
    if (-not (Test-Path -LiteralPath $cache)) {
        Invoke-WebRequest -Uri $url -OutFile $cache -UseBasicParsing
    }
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    Expand-Archive -LiteralPath $cache -DestinationPath $Root -Force
    # The embeddable distribution ships with "import site" commented out and no
    # pip; without this the packages installed below would be invisible.
    $parts = $Version -split '\.'
    $pth = Join-Path $Root ('python{0}{1}._pth' -f $parts[0], $parts[1])
    if (-not (Test-Path -LiteralPath $pth)) { throw ('unexpected embeddable layout, missing ' + $pth) }
    $lines = New-Object System.Collections.Generic.List[string]
    $siteOn = $false
    foreach ($line in (Get-Content -LiteralPath $pth)) {
        if ($line -match '^\s*#\s*import site\s*$') {
            $lines.Add('import site')
            $siteOn = $true
        } elseif ($line.Trim() -eq 'import site') {
            $lines.Add($line)
            $siteOn = $true
        } else {
            $lines.Add($line)
        }
    }
    if (-not $siteOn) { $lines.Add('import site') }
    Set-Content -LiteralPath $pth -Value $lines -Encoding ASCII
}

function Install-PipBootstrap([string]$Version) {
    $probe = Invoke-Capture -Exe $script:PythonExe -Arguments @('-m', 'pip', '--version')
    if ($probe.Code -eq 0) { return }
    Write-Step 'bootstrapping pip with get-pip.py'
    $parts = $Version -split '\.'
    $majorMinor = $parts[0] + '.' + $parts[1]
    $urls = @(
        ('https://bootstrap.pypa.io/pip/' + $majorMinor + '/get-pip.py'),
        'https://bootstrap.pypa.io/get-pip.py'
    )
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $lastError = ''
    $ok = $false
    foreach ($url in $urls) {
        $getPip = Join-Path ([IO.Path]::GetTempPath()) ('get-pip-' + $majorMinor + '.py')
        if (-not (Test-Path -LiteralPath $getPip)) {
            Write-Info ('downloading ' + $url)
            try {
                Invoke-WebRequest -Uri $url -OutFile $getPip -UseBasicParsing
            } catch {
                $lastError = $_.Exception.Message
                continue
            }
        }
        $probe = Invoke-Capture -Exe $script:PythonExe -Arguments @($getPip, '--no-warn-script-location')
        if ($probe.Code -eq 0) {
            $ok = $true
            break
        }
        $lastError = (($probe.Error + ' ' + ($probe.Output -join ' ')).Trim())
    }
    if (-not $ok) { throw ('get-pip.py failed: ' + $lastError) }
    $probe = Invoke-Capture -Exe $script:PythonExe -Arguments @('-m', 'pip', '--version')
    if ($probe.Code -ne 0) { throw 'pip is still unavailable after get-pip.py' }
    Write-Info ($probe.Output[-1])
}

$coreSpecs = @()
foreach ($name in $CorePackages) {
    if ($name -eq 'torch' -and $TorchWheel) {
        $coreSpecs += $TorchWheel
    } else {
        $coreSpecs += (Get-PipSpec $name)
    }
}

if ($DryRun) {
    Write-Step 'dry run: nothing will be downloaded, moved, written or installed'
    Write-Info ('repo root : ' + $RepoRoot)
    Write-Info ('runtime   : ' + $Root)
    Write-Info ('manifest  : ' + $Manifest)
    Write-Info ('python    : ' + $PythonVersion + ' (source: ' + $Source + ')')
    Write-Info ('pins      : ' + $pins.Count)
    Write-Info ('core specs: ' + ($coreSpecs -join ' '))
    if ($Full) { Write-Info ('full mode : all ' + $pins.Count + ' snapshot packages, one by one') }
    Write-Info ('torch idx : ' + $TorchIndex)
    Write-Info ('existing  : ' + (Test-Path -LiteralPath (Join-Path $Root 'python.exe')))
    Write-Info ('backup    : ' + ($Root + '.bak-<timestamp>') + ' (only with -Force)')
    exit 0
}

$existingPython = Join-Path $Root 'python.exe'
if (Test-Path -LiteralPath $existingPython) {
    if (-not $Force) {
        Write-Step ('runtime already exists: ' + $Root)
        $probe = Invoke-Capture -Exe $existingPython -Arguments @('-c', "import sys; print('%d.%d.%d' % sys.version_info[:3])")
        if ($probe.Code -ne 0 -or -not $probe.Output.Count) {
            throw ('the existing interpreter does not run; pass -Force to move it aside and rebuild: ' + $existingPython)
        }
        $version = [string]$probe.Output[-1].Trim()
        Write-Info ('python ' + $version + ' left untouched; pass -Force to rebuild')
        if ($version -ne $PythonVersion) {
            Write-Warning ('manifest pins Python ' + $PythonVersion + '; the existing runtime differs')
        }
        exit 0
    }
    $backup = $Root + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Write-Step ('moving the existing runtime to ' + $backup)
    Move-Item -LiteralPath $Root -Destination $backup
} elseif (Test-Path -LiteralPath $Root) {
    if (-not $Force) {
        throw ('directory exists but has no python.exe: ' + $Root + ' (pass -Force to move it aside)')
    }
    $backup = $Root + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Write-Step ('moving the incomplete runtime directory to ' + $backup)
    Move-Item -LiteralPath $Root -Destination $backup
}

$parent = Split-Path -Parent $Root
if ($parent -and -not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

$basePython = Find-BasePython $PythonVersion
if ($Source -eq 'auto') {
    if ($basePython) { $Source = 'venv' } else { $Source = 'embed' }
}
if ($Source -eq 'venv') {
    if (-not $basePython) {
        throw ('no local Python ' + $PythonVersion + ' found for -Source venv (py -3.9 / uv python find 3.9); use -Source embed')
    }
    Install-VenvRuntime $basePython
} else {
    Install-EmbeddableRuntime $PythonVersion
}

$script:PythonExe = Join-Path $Root 'python.exe'
if (-not (Test-Path -LiteralPath $script:PythonExe)) { throw ('interpreter was not created: ' + $script:PythonExe) }
$probe = Invoke-Capture -Exe $script:PythonExe -Arguments @('-c', "import sys; print('%d.%d.%d' % sys.version_info[:3])")
if ($probe.Code -ne 0 -or -not $probe.Output.Count) { throw ('the new interpreter does not run: ' + $script:PythonExe) }
$actualVersion = [string]$probe.Output[-1].Trim()
if ($actualVersion -ne $PythonVersion) {
    Write-Warning ('interpreter is Python ' + $actualVersion + ', manifest pins ' + $PythonVersion)
}

Install-PipBootstrap $actualVersion

if ($SkipPackages) {
    Write-Step 'interpreter and pip are ready; package installation skipped (-SkipPackages)'
    Write-Info ('runtime: ' + $script:PythonExe + ' (Python ' + $actualVersion + ')')
    exit 0
}

$constraintLines = foreach ($key in ($pins.Keys | Sort-Object)) { '{0}=={1}' -f $key, $pins[$key] }
Set-Content -LiteralPath $ConstraintFile -Value @($constraintLines) -Encoding ASCII

$coreFailures = New-Object System.Collections.Generic.List[string]
foreach ($spec in $coreSpecs) {
    Write-Step ('installing ' + $spec)
    $code = Invoke-PipInstall -Spec $spec -UseConstraints $true
    if ($code -ne 0) {
        Write-Warning ('pinned install failed, retrying without the constraints file: ' + $spec)
        $code = Invoke-PipInstall -Spec $spec -UseConstraints $false
    }
    if ($code -ne 0) {
        Write-Warning ('could not install ' + $spec + '; continuing with the remaining packages')
        $coreFailures.Add($spec)
    }
}

$fullFailures = New-Object System.Collections.Generic.List[string]
if ($Full) {
    Write-Step ('replaying the snapshot: ' + $pins.Count + ' packages, one by one, --no-deps')
    foreach ($key in ($pins.Keys | Sort-Object)) {
        $spec = '{0}=={1}' -f $key, $pins[$key]
        $code = Invoke-PipInstall -Spec $spec -UseConstraints $false -NoDeps
        if ($code -ne 0) {
            Write-Warning ('snapshot package skipped (no compatible wheel): ' + $spec)
            $fullFailures.Add($spec)
        }
    }
}

$importFailures = New-Object System.Collections.Generic.List[string]
Write-Step 'verifying the modules the ear imports'
foreach ($key in $CoreImports.Keys) {
    $module = $CoreImports[$key]
    $probe = Invoke-Capture -Exe $script:PythonExe -Arguments @('-c', ('import ' + $module))
    if ($probe.Code -ne 0) {
        $importFailures.Add($module)
        Write-Warning ('import failed: ' + $module)
    } else {
        Write-Info ('ok ' + $module)
    }
}

Write-Step 'summary'
Write-Info ('runtime : ' + $script:PythonExe + ' (Python ' + $actualVersion + ')')
if ($coreFailures.Count) {
    Write-Host ('core packages that failed (' + $coreFailures.Count + '):') -ForegroundColor Yellow
    foreach ($item in $coreFailures) { Write-Info $item }
} else {
    Write-Info 'core packages: all installed'
}
if ($Full) {
    if ($fullFailures.Count) {
        Write-Host ('snapshot packages skipped (' + $fullFailures.Count + ' of ' + $pins.Count + '):') -ForegroundColor Yellow
        foreach ($item in $fullFailures) { Write-Info $item }
    } else {
        Write-Info ('snapshot packages: all ' + $pins.Count + ' installed')
    }
}
if ($importFailures.Count) {
    Write-Host ('modules that do not import (' + $importFailures.Count + '):') -ForegroundColor Yellow
    foreach ($item in $importFailures) { Write-Info $item }
} else {
    Write-Info 'ear imports: all ok'
}

if ($coreFailures.Count -or $importFailures.Count) { exit 1 }
exit 0
