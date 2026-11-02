<#
.SYNOPSIS
    建耳朵（ear/whisper_serve.py）的 Python 环境：ear/ 自己的 uv 项目。

.DESCRIPTION
    步 4 实测（2026-11-01，_tmp/ear-env/REPORT.md）：paraformer 三件套在
    Python 3.10 + torch 2.14.1+cpu + funasr 1.4.19 + numpy 2.2.6 + kaldi-native-fbank
    下跑得起来，同一句中文的转写与旧 tts/GPT-sovits/runtime（Python 3.9.13）逐字一致，
    热转写 0.30s（旧 0.40s），`.venv` 1.24 GB（旧 runtime 7.46 GB）。

    所以耳朵不再往 tts/GPT-sovits/runtime 里装东西——那个 runtime 只留给 GSV/Denia。
    依赖版本的唯一真相是 ear/pyproject.toml + ear/uv.lock；config/rebuild/stt-environment.json
    只保留旧 runtime 的清单作为记录，本脚本不读它。

      1. 找 uv（没有就报错，不在这里装工具链）；
      2. uv sync --directory <repo>\ear（默认 --locked：锁文件对不上就失败，别静默换版本）；
      3. 用 ear\.venv 的解释器 import 一遍耳朵用到的模块，报失败项；
      4. 顺带确认 ear\models\modelscope\iic 的三件套在（不在只警告，模型是大文件，不进库）。

.PARAMETER Root
    耳朵目录，默认 <repo>\ear

.PARAMETER Check
    只看现有 .venv 能不能 import，不同步。

.PARAMETER Force
    删掉 ear\.venv 重装（依赖装坏了、或想从别的机器换过来时用）。默认不动已有的 .venv。

.PARAMETER Unlocked
    不用 --locked（允许 uv 按 pyproject.toml 重新解析；只在改完依赖时用，之后提交新的 uv.lock）。

.PARAMETER DryRun
    只打印计划，不下载、不安装、不删任何东西。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1 -DryRun

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/setup_stt_runtime.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$Check,
    [switch]$Force,
    [switch]$Unlocked,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
trap {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
if (-not $Root) { $Root = Join-Path $RepoRoot 'ear' }
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$Lock = Join-Path $Root 'uv.lock'
$Iic = Join-Path $Root 'models\modelscope\iic'

# 耳朵 import 的东西（whisper_serve.py 的服务层 + 两个引擎）
$Modules = [ordered]@{
    'torch'              = 'torch'
    'numpy'              = 'numpy'
    'funasr'             = 'funasr'
    'kaldi_native_fbank' = 'kaldi_native_fbank'
    'modelscope'         = 'modelscope'
    'faster_whisper'     = 'faster_whisper'
    'ctranslate2'        = 'ctranslate2'
    'fastapi'            = 'fastapi'
    'uvicorn'            = 'uvicorn'
    'python-multipart'   = 'multipart'
}

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('==> ' + $Text) -ForegroundColor Cyan
}

function Write-Info([string]$Text) { Write-Host ('    ' + $Text) }
function Write-Dry([string]$Text) { Write-Host ('    [dry] ' + $Text) -ForegroundColor DarkGray }

function Invoke-Capture([string]$Exe, [string[]]$Arguments) {
    # 静默跑一个命令，收 stdout/stderr 与退出码（不把 stderr 变成 PowerShell 异常）
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Exe
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $startInfo.Arguments = (@($Arguments | ForEach-Object {
        if ($_ -eq '') { '""' } elseif ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' ')
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    return [pscustomobject]@{ Code = $process.ExitCode; Output = [string]$stdout.Result; Error = [string]$stderr.Result }
}

function Test-Modules {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }
    $code = (($Modules.Values | ForEach-Object { 'import ' + $_ + ';' }) -join '')
    $probe = Invoke-Capture -Exe $VenvPython -Arguments @('-c', $code)
    if ($probe.Code -ne 0) {
        $tail = @(($probe.Error -split "`r?`n") | Where-Object { $_ -ne '' })
        if ($tail.Count) { Write-Info $tail[-1] }
        return $false
    }
    return $true
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
$syncArgs = @('sync', '--directory', $Root)
if (-not $Unlocked) { $syncArgs += '--locked' }

if ($DryRun) {
    Write-Step 'dry run：不下载、不安装、不删任何东西'
    Write-Info ('repo    : ' + $RepoRoot)
    Write-Info ('ear dir : ' + $Root)
    Write-Info ('uv      : ' + ($(if ($uv) { $uv.Source } else { '<缺失，正式跑会报错>' })))
    Write-Info ('command : uv ' + ($syncArgs -join ' '))
    Write-Info ('lock    : ' + (Test-Path -LiteralPath $Lock) + '  ' + $Lock)
    Write-Info ('venv    : ' + (Test-Path -LiteralPath $VenvPython) + '  ' + $VenvPython)
    Write-Info ('force   : ' + [bool]$Force)
    Write-Info ('models  : ' + (Test-Path -LiteralPath $Iic) + '  ' + $Iic)
    exit 0
}

if (-not $uv) { throw 'uv 不在 PATH 上：先装 uv（https://docs.astral.sh/uv/getting-started/installation/），再跑这个脚本' }
if (-not (Test-Path -LiteralPath $Lock)) { throw ('缺 ' + $Lock + '：uv.lock 是版本真相，必须在仓库里') }

if ($Check) {
    Write-Step '只检查：能不能 import 耳朵的依赖'
    if (-not (Test-Path -LiteralPath $VenvPython)) { throw ('没有 venv：' + $VenvPython) }
    if (Test-Modules) { Write-Info 'ear imports: all ok'; exit 0 }
    Write-Info 'ear imports: 有失败项'
    exit 1
}

if ($Force -and (Test-Path -LiteralPath (Join-Path $Root '.venv'))) {
    Write-Step ('-Force：删除 ' + (Join-Path $Root '.venv'))
    Remove-Item -LiteralPath (Join-Path $Root '.venv') -Recurse -Force
}

Write-Step ('uv ' + ($syncArgs -join ' '))
Push-Location $RepoRoot
try {
    & $uv.Source @syncArgs | Out-Host
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($code -ne 0) { throw ('uv sync 退出码 ' + $code + '（看上面输出；改完依赖要提交新的 ear/uv.lock）') }

Write-Step '验证耳朵的 import'
$failures = New-Object System.Collections.Generic.List[string]
foreach ($key in $Modules.Keys) {
    $module = $Modules[$key]
    $probe = Invoke-Capture -Exe $VenvPython -Arguments @('-c', ('import ' + $module))
    if ($probe.Code -ne 0) {
        $failures.Add($module)
        Write-Info ('FAIL ' + $module)
    } else {
        Write-Info ('ok   ' + $module)
    }
}

Write-Step '模型目录'
if (Test-Path -LiteralPath $Iic) {
    Write-Info ('ok   ' + $Iic)
} else {
    Write-Host ('    ! 缺 ' + $Iic + '（paraformer-zh / fsmn-vad / ct-punc 三件套，约 1.1 GB，不进库）') -ForegroundColor Yellow
    Write-Host '      下载方法见 docs/ASR路径清单.md' -ForegroundColor Yellow
}

Write-Step 'summary'
Write-Info ('venv   : ' + $VenvPython)
if ($failures.Count) {
    Write-Host ('import 失败 (' + $failures.Count + ')：' + ($failures -join ', ')) -ForegroundColor Yellow
    exit 1
}
Write-Info 'ear imports: all ok'
exit 0
