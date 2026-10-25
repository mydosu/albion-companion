param(
    # Checkout that holds the private assets. Read-only: this script never writes here.
    [string]$SourceRoot = 'D:\AI\Wife',
    # Checkout or worktree that needs the assets. Defaults to the repository this script lives in.
    [string]$TargetRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'

# NOTICE: shell/.gitignore ignores these paths on purpose, so temporary worktrees start without
# them. Without author-models, vue-tsc fails on the two JSON imports in spine38/Model.vue. Without
# the model zips, electron-vite fails on the spine import and warns on the live2d URL. Copy the
# files instead of linking them, so deleting a worktree cannot delete the source files. Existing
# files are kept.
$directories = @(
    'shell\packages\stage-ui-spine\src\assets\author-models'
)
$files = @(
    'shell\packages\stage-ui\src\assets\live2d\models\albion.zip'
    'shell\packages\stage-ui\src\assets\live2d\models\albion-old-aerbien3.zip'
    'shell\packages\stage-ui\src\assets\live2d\models\hiyori_free_zh.zip'
    'shell\packages\stage-ui\src\assets\live2d\models\hiyori_pro_zh.zip'
    'shell\packages\stage-ui\src\assets\spine\models\albion-spine-38.zip'
    'shell\packages\stage-ui\src\assets\vrm\models\AvatarSample-A\AvatarSample_A.vrm'
    'shell\packages\stage-ui\src\assets\vrm\models\AvatarSample-B\AvatarSample_B.vrm'
)

$copied = 0
$skipped = 0

function Copy-IfMissing([string]$source, [string]$dest, [string]$label) {
    if (Test-Path -LiteralPath $dest) {
        $script:skipped++
        return
    }
    if ($WhatIf) {
        Write-Output "would copy $label"
        $script:copied++
        return
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
    Copy-Item -LiteralPath $source -Destination $dest
    $script:copied++
}

foreach ($relative in $directories) {
    $sourceDir = Join-Path $SourceRoot $relative
    $targetDir = Join-Path $TargetRoot $relative
    if (-not (Test-Path -LiteralPath $sourceDir -PathType Container)) {
        throw "Private asset directory not found in source checkout: $sourceDir"
    }
    foreach ($file in Get-ChildItem -LiteralPath $sourceDir -Recurse -File) {
        $rel = $file.FullName.Substring($sourceDir.Length).TrimStart('\')
        Copy-IfMissing $file.FullName (Join-Path $targetDir $rel) $rel
    }
    Write-Output "directory: $relative -> $targetDir"
}

foreach ($relative in $files) {
    $sourceFile = Join-Path $SourceRoot $relative
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
        throw "Private asset file not found in source checkout: $sourceFile"
    }
    Copy-IfMissing $sourceFile (Join-Path $TargetRoot $relative) $relative
}

Write-Output "private-assets: copied=$copied skipped(existing)=$skipped target=$TargetRoot"
