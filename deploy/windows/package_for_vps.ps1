param(
    [string]$OutputPath = "",
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
} else {
    $ProjectRoot = Resolve-Path $ProjectRoot
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $runtimeDir = Join-Path $ProjectRoot "data\runtime"
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $OutputPath = Join-Path $runtimeDir "meeting-digest-bot-release.zip"
}

$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("meeting-digest-bot-package-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null

try {
    $include = @(
        "meeting_digest_bot",
        "deploy",
        "requirements.txt",
        ".env.example",
        "MeetingDigestBot.readme"
    )

    foreach ($item in $include) {
        $source = Join-Path $ProjectRoot $item
        if (-not (Test-Path $source)) {
            throw "Missing package item: $source"
        }
        $destination = Join-Path $stageRoot $item
        if ((Get-Item $source).PSIsContainer) {
            Copy-Item -Path $source -Destination $destination -Recurse -Force
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path $destination -Parent) | Out-Null
            Copy-Item -Path $source -Destination $destination -Force
        }
    }

    Get-ChildItem -Path $stageRoot -Recurse -Force -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    Get-ChildItem -Path $stageRoot -Recurse -Force -Include "*.pyc",".DS_Store" | Remove-Item -Force

    if (Test-Path $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $OutputPath -Parent) | Out-Null

    $env:PACKAGE_STAGE_ROOT = $stageRoot
    $env:PACKAGE_OUTPUT_PATH = $OutputPath
    @'
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import os

stage = Path(os.environ["PACKAGE_STAGE_ROOT"])
output = Path(os.environ["PACKAGE_OUTPUT_PATH"])

with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(stage).as_posix())
'@ | python -

    $archive = Get-Item $OutputPath
    Write-Host "Package created: $($archive.FullName)"
    Write-Host "Size: $([Math]::Round($archive.Length / 1KB, 2)) KB"
} finally {
    if (Test-Path $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}
