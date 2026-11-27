<#
  check_agy_host —— 把仓库里跑自检要用的那几样拷进 WSL 的临时目录再跑（2026-11-27，B 块）。

  WSL 看不到 D 盘（automount 关了，也不许改 wsl.conf），所以只能走 \\wsl$\Ubuntu\tmp\。
  只拷 brain/agent、brain/bin/agy_host.ts 和这支自检本身，不动 /root/albion-pi。
  用：pwsh -File tests/pi-brain/check_agy_host.ps1 [-Keep]
#>
param([switch]$Keep)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$stage = '\\wsl$\Ubuntu\tmp\agy-host-dev\repo'

# 上一轮的假 node_modules 是软链，Windows 这边删不掉：清目录/建目录都交给 WSL
wsl -d Ubuntu -- bash -c 'rm -rf /tmp/agy-host-dev/repo && mkdir -p /tmp/agy-host-dev/repo/brain/bin /tmp/agy-host-dev/repo/tests/pi-brain'
if ($LASTEXITCODE -ne 0) { throw 'WSL 里建临时目录失败' }

Copy-Item (Join-Path $repo 'brain\agent') (Join-Path $stage 'brain\agent') -Recurse -Force
Copy-Item (Join-Path $repo 'brain\bin\agy_host.ts') (Join-Path $stage 'brain\bin\agy_host.ts') -Force
Copy-Item (Join-Path $repo 'tests\pi-brain\check_agy_host.ts') (Join-Path $stage 'tests\pi-brain\check_agy_host.ts') -Force

$extra = if ($Keep) { ' --keep' } else { '' }
wsl -d Ubuntu -- bash -c "cd /tmp/agy-host-dev/repo && node tests/pi-brain/check_agy_host.ts$extra"
exit $LASTEXITCODE
