<#
.SYNOPSIS
    一键安装总入口：把耳朵 runtime、TTS 环境、WSL 脑和模板按阶段串起来。

.DESCRIPTION
    只做仓库内的环境准备，不安装 Git/Node/WSL/显卡驱动这类系统级软件，
    不构建桌面壳，也不启动任何服务（安装完的服务仍按一键启动脚本拉起）。

    阶段（按顺序）：
      preflight  检查 git、node/pnpm、uv、nvidia-smi、wsl 和 WSL 发行版；
                 缺的列出来并给官方下载地址。uv / WSL 发行版是所选阶段的硬前提。
      stt        调 tools/setup/setup_stt_runtime.ps1 建耳朵/眼睛的 Python 3.9。
      tts        调 tools/setup/setup_tts_env.ps1 建 tts/irodori/.venv。
      brain      在 WSL 里跑 brain/install.sh 和 deploy.sh。
                 install.sh 需要 root；默认 WSL 用户不是 root 时加 -WslUser root。
      templates  把 config/local.env.example 复制成 config/local.env（已存在不覆盖）；
                 脑的 agent/.env.example、SOUL.example.md 由 deploy.sh 在目标缺失时放。

    每个阶段幂等：已经装好的打印 [skip]，不重复动。任一阶段失败会打印阶段名并停下，
    不会继续后面的阶段。

.PARAMETER DryRun
    只打印/演练，不写文件、不装包、不执行 WSL 安装。
    有 -DryRun 的子脚本（setup_stt_runtime.ps1、setup_tts_env.ps1）会真的以 -DryRun 调起，
    以便看到它们的计划；deploy.sh 即使 DRY_RUN=1 也会 mkdir 目标目录，所以这里只打印不执行。

.PARAMETER Only
    只跑这些阶段。逗号分隔，例如 -Only stt,tts；也可 -Only templates。
    带空格的列表要整体加引号（-Only "stt tts"）。可用：preflight, stt, tts, brain, templates。

.PARAMETER Skip
    跳过这些阶段，写法同 -Only。和 -Only 同时给时，先取 Only 再减 Skip。

.PARAMETER Distro
    WSL 发行版名，默认 Ubuntu。

.PARAMETER WslUser
    WSL 里以哪个用户跑 install.sh/deploy.sh，例如 root。默认用 WSL 默认用户。
    install.sh 要写 /usr/local，普通用户会失败，所以默认用户不是 root 时请加 -WslUser root。

.PARAMETER BrainHome
    WSL 里的脑目录，默认 /root/albion-pi；作为第一个参数传给 deploy.sh。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/install_all.ps1 -DryRun

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/install_all.ps1 -Only stt,tts -DryRun

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/install_all.ps1 -Skip brain

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup/install_all.ps1 -WslUser root
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string[]]$Only = @(),
    [string[]]$Skip = @(),
    [string]$Distro = 'Ubuntu',
    [string]$WslUser = '',
    [string]$BrainHome = '/root/albion-pi'
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSCommandPath))
$AllStages = @('preflight', 'stt', 'tts', 'brain', 'templates')
$StageTitles = @{
    preflight = '前置检查'
    stt       = '耳朵 Python runtime'
    tts       = 'TTS 环境'
    brain     = 'WSL 脑（install.sh + deploy.sh）'
    templates = '本地模板'
}
$script:Results = [ordered]@{}
$script:Selected = @()
$script:CurrentStage = ''

function Write-Section([string]$Text) {
    Write-Host ''
    Write-Host ('=== ' + $Text + ' ===') -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host ('[OK]   ' + $Text) -ForegroundColor Green
}

function Write-Skip([string]$Text) {
    Write-Host ('[skip] ' + $Text) -ForegroundColor DarkGray
}

function Write-Run([string]$Text) {
    Write-Host ('[run]  ' + $Text) -ForegroundColor Yellow
}

function Write-Dry([string]$Text) {
    Write-Host ('[dry]  ' + $Text) -ForegroundColor Yellow
}

function Write-WarnLine([string]$Text) {
    Write-Host ('[warn] ' + $Text) -ForegroundColor DarkYellow
}

function Write-InfoLine([string]$Text) {
    Write-Host ('       ' + $Text)
}

function Get-FirstLine($Lines) {
    foreach ($line in @($Lines)) {
        if ($null -ne $line) {
            $text = ([string]$line).Trim()
            if ($text) { return $text }
        }
    }
    return ''
}

function Get-CommandSource([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Invoke-Probe([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>$null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{
        Code   = $code
        Output = @($output | ForEach-Object { [string]$_ })
    }
}

function Invoke-ProbeSafe([string]$Exe, [string[]]$Arguments) {
    try {
        return (Invoke-Probe -Exe $Exe -Arguments $Arguments)
    } catch {
        return [pscustomobject]@{ Code = 1; Output = @() }
    }
}

function Format-NativeCommand([string]$Exe, [string[]]$Arguments) {
    $parts = @($Exe) + @($Arguments)
    $quoted = foreach ($part in $parts) {
        if ($part -match '[\s"]') { '"' + ($part -replace '"', '\"') + '"' } else { $part }
    }
    return ($quoted -join ' ')
}

function Split-StageNames([string[]]$Values) {
    $result = @()
    foreach ($value in @($Values)) {
        if ($null -eq $value) { continue }
        foreach ($part in ($value -split '[,; ]+')) {
            $name = $part.Trim().ToLowerInvariant()
            if ($name) { $result += $name }
        }
    }
    return @($result)
}

function ConvertTo-WslPath([string]$WindowsPath) {
    $full = [IO.Path]::GetFullPath($WindowsPath)
    if ($full -match '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\(.*)$') {
        $rest = ($Matches[2] -replace '\\', '/')
        return [pscustomobject]@{ Path = ('/' + $rest); Distro = $Matches[1] }
    }
    if ($full -match '^([A-Za-z]):\\(.*)$') {
        $drive = $Matches[1].ToLowerInvariant()
        $rest = ($Matches[2] -replace '\\', '/')
        return [pscustomobject]@{ Path = ('/mnt/' + $drive + '/' + $rest); Distro = $null }
    }
    throw ('无法把路径转成 WSL 路径：' + $full + '（支持 X:\... 和 \\wsl.localhost\<发行版>\...）')
}

function Get-WslBaseArgs {
    $result = @('-d', $Distro)
    if ($WslUser) { $result += @('-u', $WslUser) }
    return $result
}

function Test-WslDistro([string]$Name) {
    if (-not (Get-Command 'wsl.exe' -ErrorAction SilentlyContinue)) { return $false }
    $probe = Invoke-ProbeSafe 'wsl.exe' @('-d', $Name, '--', 'true')
    return ($probe.Code -eq 0)
}

function Set-StageResult([string]$Stage, [string]$Status) {
    $script:Results[$Stage] = $Status
}

function Invoke-ChildScript([string]$FileName, [string[]]$Arguments) {
    $path = Join-Path $PSScriptRoot $FileName
    if (-not (Test-Path -LiteralPath $path)) { throw ('缺少子脚本：' + $path) }
    $powershell = Get-CommandSource 'powershell.exe'
    if (-not $powershell) { $powershell = Get-CommandSource 'pwsh.exe' }
    if (-not $powershell) { throw '找不到 powershell.exe / pwsh.exe' }
    $commandArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $path) + @($Arguments)
    Write-InfoLine (Format-NativeCommand $powershell $commandArgs)
    & $powershell @commandArgs | Out-Host
    return $LASTEXITCODE
}

function Test-PythonModules([string]$Python, [string[]]$Modules) {
    if (-not (Test-Path -LiteralPath $Python)) { return $false }
    $probe = Invoke-ProbeSafe $Python @('-c', ('import ' + ($Modules -join ', ')))
    return ($probe.Code -eq 0)
}

# ---------------------------------------------------------------------------
# 阶段实现
# ---------------------------------------------------------------------------

function Invoke-PreflightStage {
    $fatal = New-Object System.Collections.Generic.List[string]
    $warnings = New-Object System.Collections.Generic.List[string]
    $needTts = ($script:Selected -contains 'tts')
    $needBrain = ($script:Selected -contains 'brain')

    Write-InfoLine '只检查，不自动安装系统级软件；缺的按下面的官方地址自己装。'

    $git = Get-CommandSource 'git'
    if ($git) {
        $probe = Invoke-ProbeSafe $git @('--version')
        if ($probe.Code -eq 0) { Write-Ok (Get-FirstLine $probe.Output) }
        else { Write-WarnLine ('git 在，但取不到版本：' + $git); $warnings.Add('git') }
    } else {
        Write-WarnLine 'git 未找到：https://git-scm.com/download/win'
        $warnings.Add('git')
    }

    $node = Get-CommandSource 'node'
    if ($node) {
        $probe = Invoke-ProbeSafe $node @('--version')
        if ($probe.Code -eq 0) { Write-Ok ('node ' + (Get-FirstLine $probe.Output)) }
        else { Write-WarnLine ('node 在，但取不到版本：' + $node); $warnings.Add('node') }
    } else {
        Write-WarnLine 'node 未找到：https://nodejs.org/en/download'
        $warnings.Add('node')
    }

    $pnpm = Get-CommandSource 'pnpm'
    if ($pnpm) {
        $probe = Invoke-ProbeSafe $pnpm @('--version')
        if ($probe.Code -eq 0) { Write-Ok ('pnpm ' + (Get-FirstLine $probe.Output)) }
        else { Write-Ok ('pnpm ' + $pnpm + '（版本没取到，壳构建前自行确认）') }
    } else {
        Write-WarnLine 'pnpm 未找到：https://pnpm.io/installation'
        $warnings.Add('pnpm')
    }

    $uv = Get-CommandSource 'uv'
    if ($uv) {
        $probe = Invoke-ProbeSafe $uv @('--version')
        if ($probe.Code -eq 0) { Write-Ok (Get-FirstLine $probe.Output) }
        else { Write-WarnLine ('uv 在，但取不到版本：' + $uv); $warnings.Add('uv') }
    } else {
        Write-WarnLine 'uv 未找到：https://docs.astral.sh/uv/getting-started/installation/'
        if ($needTts) { $fatal.Add('uv（TTS 阶段要用）：https://docs.astral.sh/uv/getting-started/installation/') }
        else { Write-InfoLine '（本次没选 TTS 阶段，uv 不阻塞）' }
    }

    $smi = Get-CommandSource 'nvidia-smi'
    if ($smi) {
        $probe = Invoke-ProbeSafe $smi @('--query-gpu=name,driver_version', '--format=csv,noheader')
        if ($probe.Code -eq 0 -and (Get-FirstLine $probe.Output)) {
            Write-Ok ('nvidia-smi: ' + (Get-FirstLine $probe.Output))
        } else {
            Write-WarnLine 'nvidia-smi 在，但没读到 GPU；TTS 仍可建 CPU 环境，CUDA 不可用'
            $warnings.Add('gpu')
        }
    } else {
        Write-WarnLine 'nvidia-smi 未找到：https://www.nvidia.com/Download/index.aspx'
        $warnings.Add('nvidia-smi')
    }

    $wsl = Get-CommandSource 'wsl.exe'
    $distroOk = $false
    if ($wsl) {
        Write-Ok ('wsl ' + $wsl)
        $listProbe = Invoke-ProbeSafe $wsl @('-l', '-q')
        $names = @($listProbe.Output | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
        if ($names.Count) { Write-InfoLine ('WSL 发行版：' + ($names -join ', ')) }
        $distroOk = Test-WslDistro $Distro
        if ($distroOk) {
            Write-Ok ('WSL 发行版 ' + $Distro + ' 可用')
        } else {
            Write-WarnLine ('WSL 里没有发行版 ' + $Distro + '：https://learn.microsoft.com/windows/wsl/install')
            if ($needBrain) { $fatal.Add('WSL 发行版 ' + $Distro + '（脑阶段要用）：https://learn.microsoft.com/windows/wsl/install') }
        }
    } else {
        Write-WarnLine 'wsl.exe 未找到：https://learn.microsoft.com/windows/wsl/install'
        if ($needBrain) { $fatal.Add('wsl.exe（脑阶段要用）：https://learn.microsoft.com/windows/wsl/install') }
    }

    if ($warnings.Count) {
        Write-WarnLine ('非阻塞项：' + ($warnings -join ', ') + '（不挡环境阶段，但壳/视觉等步骤要用）')
    }
    if ($fatal.Count) {
        throw ('前置检查未通过：' + [Environment]::NewLine + '  - ' + ($fatal -join ([Environment]::NewLine + '  - ')))
    }
    Write-Ok '前置检查通过'
    Set-StageResult 'preflight' 'ok'
}

function Invoke-SttStage {
    $runtime = Join-Path $RepoRoot 'tts\GPT-sovits\runtime\python.exe'
    $runtimeOk = $false
    if (Test-Path -LiteralPath $runtime) {
        $probe = Invoke-ProbeSafe $runtime @('-c', 'import sys; assert sys.version_info[:2] == (3, 9)')
        $runtimeOk = ($probe.Code -eq 0)
    }
    if ($runtimeOk) {
        Write-Skip ('耳朵 runtime 已存在且能跑（Python 3.9）：' + $runtime)
        Set-StageResult 'stt' 'skip'
        return
    }
    if (Test-Path -LiteralPath $runtime) {
        Write-WarnLine ('runtime 存在但跑不起来，交给 setup_stt_runtime.ps1（可能需要 -Force）：' + $runtime)
    }

    $arguments = @()
    if ($DryRun) { $arguments += '-DryRun' }
    Write-Run ('setup_stt_runtime.ps1 ' + ($arguments -join ' '))
    $code = Invoke-ChildScript 'setup_stt_runtime.ps1' $arguments
    if ($code -ne 0) { throw ('setup_stt_runtime.ps1 退出码 ' + $code + '；看上面输出，修好后重跑') }
    if ($DryRun) {
        Write-Dry '耳朵 runtime：上面是子脚本的 -DryRun 计划，没有下载/安装'
        Set-StageResult 'stt' 'dry'
    } else {
        Write-Ok '耳朵 runtime 完成'
        Set-StageResult 'stt' 'ok'
    }
}

function Invoke-TtsStage {
    $venvPython = Join-Path $RepoRoot 'tts\irodori\.venv\Scripts\python.exe'
    $modules = @('torch', 'fastapi', 'uvicorn', 'httpx', 'websockets')
    if (Test-PythonModules $venvPython $modules) {
        Write-Skip ('TTS venv 已存在且依赖可 import：' + $venvPython)
        Set-StageResult 'tts' 'skip'
        return
    }
    if (Test-Path -LiteralPath $venvPython) {
        Write-WarnLine ('TTS venv 存在但依赖不齐，交给 setup_tts_env.ps1 补齐：' + $venvPython)
    }

    $arguments = @()
    if ($DryRun) { $arguments += '-DryRun' }
    Write-Run ('setup_tts_env.ps1 ' + ($arguments -join ' '))
    $code = Invoke-ChildScript 'setup_tts_env.ps1' $arguments
    if ($code -ne 0) { throw ('setup_tts_env.ps1 退出码 ' + $code + '；看上面输出，修好后重跑') }
    if ($DryRun) {
        Write-Dry 'TTS 环境：上面是子脚本的 -DryRun 计划，没有执行 uv sync'
        Set-StageResult 'tts' 'dry'
    } else {
        Write-Ok 'TTS 环境完成'
        Set-StageResult 'tts' 'ok'
    }
}

function Invoke-BrainStage {
    if (-not $Distro) { throw 'WSL 发行版名为空；用 -Distro <名字> 指定' }
    if (-not $BrainHome.StartsWith('/')) { throw ('-BrainHome 必须是 WSL 绝对路径（以 / 开头）：' + $BrainHome) }
    if (-not (Test-WslDistro $Distro)) {
        throw ('WSL 里没有发行版 ' + $Distro + '；先装/注册，或 -Distro 换一个名字')
    }

    $wslRepo = ConvertTo-WslPath $RepoRoot
    if ($wslRepo.Distro -and $wslRepo.Distro -ne $Distro) {
        Write-WarnLine ('仓库在 WSL 发行版 ' + $wslRepo.Distro + ' 里，但 -Distro 是 ' + $Distro + '；路径可能对不上')
    }
    $repoPath = $wslRepo.Path.TrimEnd('/')
    if (-not $repoPath) { $repoPath = '/' }
    $installScript = $repoPath + '/brain/install.sh'
    $deployScript = $repoPath + '/brain/deploy.sh'

    $wslBase = @(Get-WslBaseArgs)
    $installArgs = $wslBase + @('--', 'bash', $installScript)
    $deployArgs = $wslBase + @('--', 'bash', $deployScript, $BrainHome)

    $visible = Invoke-ProbeSafe 'wsl.exe' ($wslBase + @('--', 'test', '-f', $installScript))
    if ($visible.Code -ne 0) {
        $message = 'WSL 里看不到仓库脚本 ' + $installScript + '；WSL 的 /etc/wsl.conf 关了 automount（/mnt/c、/mnt/d 不是 Windows 盘）时会这样，生产仓库 D:\AI\Wife 正常应映射成 /mnt/d/AI/Wife。请先启用自动挂载，或把仓库放到 WSL 能访问的路径再跑。'
        if ($DryRun) { Write-WarnLine $message } else { throw $message }
    }

    if (-not $WslUser) {
        $idProbe = Invoke-ProbeSafe 'wsl.exe' ($wslBase + @('--', 'id', '-u'))
        $uid = Get-FirstLine $idProbe.Output
        if ($idProbe.Code -eq 0 -and $uid -and $uid -ne '0') {
            if ($DryRun) {
                Write-WarnLine ('WSL 默认用户 uid=' + $uid + '；真跑 install.sh 会因非 root 失败，加 -WslUser root')
            } else {
                throw ('WSL 默认用户 uid=' + $uid + '，install.sh 要 root；加 -WslUser root 或用 root 默认用户')
            }
        }
    }

    Write-InfoLine ('install.sh 需要 root（写 /usr/local）；deploy.sh 目标：' + $BrainHome)
    if ($DryRun) {
        Write-Dry (Format-NativeCommand 'wsl.exe' $installArgs)
        Write-Dry (Format-NativeCommand 'wsl.exe' $deployArgs)
        Write-InfoLine 'deploy.sh 即使 DRY_RUN=1 也会 mkdir 目标目录，所以 -DryRun 下只打印、不执行 deploy.sh'
        Write-Dry '脑：以上命令没有执行'
        Set-StageResult 'brain' 'dry'
        return
    }

    Write-Run (Format-NativeCommand 'wsl.exe' $installArgs)
    & wsl.exe @installArgs | Out-Host
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw ('install.sh 退出码 ' + $code + '（看上面 WSL 输出；常见：非 root、网络、node/pi 版本）') }

    Write-Run (Format-NativeCommand 'wsl.exe' $deployArgs)
    & wsl.exe @deployArgs | Out-Host
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw ('deploy.sh 退出码 ' + $code + '（看上面输出；常见：check_pi_brain_sync 报差异、目标不可写）') }

    Write-Ok ('脑部署完成：' + $BrainHome + '（模板只在目标缺失时放，已有文件不覆盖）')
    Set-StageResult 'brain' 'ok'
}

function Invoke-TemplatesStage {
    $source = Join-Path $RepoRoot 'config\local.env.example'
    $target = Join-Path $RepoRoot 'config\local.env'
    if (-not (Test-Path -LiteralPath $source)) { throw ('模板不存在：' + $source) }

    if (Test-Path -LiteralPath $target) {
        Write-Skip ('本地环境模板已存在，不覆盖：' + $target)
    } elseif ($DryRun) {
        Write-Dry ('复制 ' + $source + ' -> ' + $target)
    } else {
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -LiteralPath $source -Destination $target
        Write-Ok ('本机环境模板已复制：' + $target + '（填好后启动器会自动加载，见 launch/load_env.bat；重启对应服务生效）')
    }

    Write-InfoLine ('脑模板由 deploy.sh 在目标缺失时放：' + $BrainHome + '/agent/.env.example -> .env，SOUL.example.md -> SOUL.md')
    if ($DryRun) {
        Write-Dry '模板：上面是计划，没有写文件'
        Set-StageResult 'templates' 'dry'
    } else {
        Set-StageResult 'templates' 'ok'
    }
}

function Write-ManualChecklist {
    Write-Section '还要手动做（脚本不碰）'
    Write-InfoLine '1. 壳：照 docs/SHELL_FIRST_RUN.md 填 shell/apps/stage-tamagotchi/.env.local（VITE_ALBION_LLM_KEY = 脑的 API_SERVER_KEY），放两个角色 zip，再 pnpm install --frozen-lockfile && pnpm -F @proj-airi/stage-tamagotchi build:unpack。'
    Write-InfoLine '2. 脑：在 WSL 里把 agent/.env.example 复制成 agent/.env（至少 API_SERVER_KEY，和壳一致）、SOUL.example.md 复制成 SOUL.md 并写人设；deploy.sh 只放模板，不覆盖已有文件。'
    Write-InfoLine '3. 环境变量：config/local.env 是 Windows 侧唯一配置处（模板 local.env.example，不入库），launch/load_env.bat 会自动加载，真环境变量优先。'
    Write-InfoLine '4. OpenViking：照 memory/templates/ov.conf.example 和 memory/templates/ov.conf.notes.md 建自己的 ov.conf（不入库），再按教程阶段 7 起服务。'
    Write-InfoLine '5. 视觉机：照 deploy/vision/README.md 装驱动/容器、复制 .env.example 为 .env、下模型、开防火墙。'
    Write-InfoLine '6. 权重：照 docs/REBUILD.md「权重与 Windows 环境」；自训权重在私有仓库 Releases，基础模型/角色包/参考音频按该节恢复，别用别处的下载地址替换。'
    Write-InfoLine '7. 验收：python tools/check_repository.py、python tools/check_pi_brain_sync.py，然后按 docs/REBUILD.md「启动与验收」用一键启动脚本拉服务并做端到端确认。'
}

# ---------------------------------------------------------------------------
# 阶段选择与执行
# ---------------------------------------------------------------------------

$onlyList = @(Split-StageNames $Only)
$skipList = @(Split-StageNames $Skip)
foreach ($name in ($onlyList + $skipList)) {
    if ($AllStages -notcontains $name) {
        throw ('未知阶段 "' + $name + '"；可用：' + ($AllStages -join ', '))
    }
}

$selected = @()
if ($onlyList.Count) {
    $selected = @($AllStages | Where-Object { $onlyList -contains $_ })
} else {
    $selected = @($AllStages)
}
if ($skipList.Count) {
    $selected = @($selected | Where-Object { $skipList -notcontains $_ })
}
if (-not $selected.Count) { throw '没有要跑的阶段（检查 -Only / -Skip）' }
$script:Selected = @($selected)

Write-Host ''
Write-Host '阿尔比恩 Windows 一键安装' -ForegroundColor Cyan
Write-Host ('仓库：' + $RepoRoot)
Write-Host ('阶段：' + ($selected -join ' -> '))
Write-Host ('模式：' + ($(if ($DryRun) { 'DryRun（只演练）' } else { '真装' })))
Write-Host ('WSL：' + $Distro + '，脑目录 ' + $BrainHome)

$index = 0
try {
    foreach ($stage in $selected) {
        $index++
        $script:CurrentStage = $stage
        $title = $StageTitles[$stage]
        Write-Section ('[' + $index + '/' + $selected.Count + '] ' + $title + '（' + $stage + '）')
        switch ($stage) {
            'preflight' { Invoke-PreflightStage }
            'stt'       { Invoke-SttStage }
            'tts'       { Invoke-TtsStage }
            'brain'     { Invoke-BrainStage }
            'templates' { Invoke-TemplatesStage }
            default     { throw ('内部错误：未知阶段 ' + $stage) }
        }
    }
} catch {
    $failedStage = $script:CurrentStage
    if ($failedStage) { Set-StageResult $failedStage 'FAIL' }
    Write-Host ''
    Write-Host ('[FAIL] 阶段 ' + $failedStage + '（' + $StageTitles[$failedStage] + '）：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '[STOP] 后面的阶段没有执行；修好后重跑（可先用 -DryRun 看计划）。' -ForegroundColor Red
    exit 1
}

Write-Section '阶段结果'
foreach ($stage in $selected) {
    $status = $script:Results[$stage]
    if (-not $status) { $status = 'unknown' }
    $label = ('{0,-9} {1}' -f $stage, $status)
    switch ($status) {
        'ok'   { Write-Host ('  ' + $label) -ForegroundColor Green }
        'skip' { Write-Host ('  ' + $label) -ForegroundColor DarkGray }
        'dry'  { Write-Host ('  ' + $label) -ForegroundColor Yellow }
        default { Write-Host ('  ' + $label) -ForegroundColor Red }
    }
}

Write-ManualChecklist

if ($DryRun) {
    Write-Host ''
    Write-Ok 'DryRun 完成：没有下载包、没有写仓库文件、没有执行 WSL 安装。'
} else {
    Write-Host ''
    Write-Ok '安装阶段结束。服务仍按一键启动脚本拉起。'
}
exit 0
