# Package astrbot_plugin_moe_waifu into an AstrBot-installable zip.
#
# Design notes (shared with sibling plugins, keep them):
#   1) EXPLICIT include list. New top-level .py modules MUST be added here too,
#      and the self-check below fails the build when one is missed.
#   2) Refuse to overwrite an existing zip of the same version: bump the version
#      in metadata.yaml (and add a CHANGELOG entry) before repacking. Historical
#      zips in dist/ are kept forever so users can roll back.
#   3) Archive layout: wrapped in a top-level folder named after the plugin, with
#      forward slashes. Do NOT use Compress-Archive.
#   4) dist/ is git-ignored: the zip is a local install artifact, not source.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_zip.ps1
#
# NOTE: keep this file ASCII-only. PS 5.1 on a non-UTF8 code page can mis-parse
#       UTF-8 no-BOM files that contain CJK comments.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

if ($PSScriptRoot) {
    $root = $PSScriptRoot
} elseif ($MyInvocation.MyCommand.Path) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $root = (Get-Location).Path
}

$pluginName = "astrbot_plugin_moe_waifu"
$distDir = Join-Path $root "dist"

# --- version from metadata.yaml (UTF8) ---
$metaPath = Join-Path $root "metadata.yaml"
if (-not (Test-Path $metaPath)) {
    Write-Host "ERROR: metadata.yaml not found" -ForegroundColor Red
    exit 1
}
$metaRaw = Get-Content -Raw -Encoding UTF8 $metaPath
$version = ""
if ($metaRaw -match '(?m)^\s*version:\s*v?([0-9][^\s#]*)') {
    $version = $Matches[1].Trim()
}
if (-not $version) {
    Write-Host "ERROR: cannot read version from metadata.yaml" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $distDir)) {
    New-Item -ItemType Directory -Path $distDir | Out-Null
}
$zipName = "${pluginName}_v${version}.zip"
$zipPath = Join-Path $distDir $zipName

if (Test-Path $zipPath) {
    Write-Host "ERROR: $zipName already exists. Bump 'version' in metadata.yaml and add a CHANGELOG entry before repacking." -ForegroundColor Red
    exit 1
}

# --- files that go into the release ---
# Keep this in sync with main.py's reload list (store / card / pool / draw).
$includeList = @(
    "main.py",
    "pool.py",
    "draw.py",
    "card.py",
    "store.py",
    "seed_pool.json",
    "_conf_schema.json",
    "metadata.yaml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE"
)

# --- self-check: every top-level .py in the repo MUST be listed above ---
$topPy = @(Get-ChildItem $root -Filter *.py -File | Select-Object -ExpandProperty Name)
$notListed = @($topPy | Where-Object { $includeList -notcontains $_ })
if ($notListed.Count -gt 0) {
    Write-Host ("ERROR: top-level module(s) missing from includeList: " + ($notListed -join ", ")) -ForegroundColor Red
    Write-Host "       Add them to the list above before packaging." -ForegroundColor Red
    exit 1
}
Write-Host ("includeList check OK: all " + $topPy.Count + " top-level .py files are listed")

function Add-ItemToZip($zip, $fsPath, $entryPath) {
    if (Test-Path -PathType Container $fsPath) {
        # directory entry must end with '/'
        $null = $zip.CreateEntry($entryPath + "/")
        Get-ChildItem $fsPath |
            Where-Object { $_.Name -ne "__pycache__" -and $_.Extension -notin @(".pyc", ".pyo") } |
            ForEach-Object {
                Add-ItemToZip $zip $_.FullName ($entryPath + "/" + $_.Name)
            }
    } else {
        # entries are named with forward slashes so Linux-side zipfile sees a real tree
        $entry = $zip.CreateEntry($entryPath, [System.IO.Compression.CompressionLevel]::Optimal)
        $stream = $entry.Open()
        try {
            $bytes = [System.IO.File]::ReadAllBytes($fsPath)
            $stream.Write($bytes, 0, $bytes.Length)
        } finally {
            $stream.Dispose()
        }
    }
}

$fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create, $false)
try {
    foreach ($name in $includeList) {
        $fsPath = Join-Path $root $name
        if (-not (Test-Path $fsPath)) {
            Write-Host "ERROR: missing required item: $name" -ForegroundColor Red
            $zip.Dispose(); $fs.Dispose()
            Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
            exit 1
        }
        Add-ItemToZip $zip $fsPath ($pluginName + "/" + $name)
    }
} finally {
    $zip.Dispose()
    $fs.Dispose()
}

# Use the full path to System32 tar.exe: when launched from Git Bash, MSYS tar
# shadows it on PATH and fails with "Cannot connect to X:" on drive-letter paths.
$tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
$entries = @(& $tarExe -tf $zipPath)
$sizeKb = [math]::Round((Get-Item $zipPath).Length / 1024, 1)
Write-Host "Packaged: $zipPath" -ForegroundColor Green
Write-Host ("Entries : {0}   Size: {1} KB" -f $entries.Count, $sizeKb)

# --- verify the archive layout (top level must be exactly the plugin folder) ---
$top = @($entries | ForEach-Object { ($_ -split '/')[0] } | Sort-Object -Unique)
Write-Host ("Top-level: " + ($top -join ","))
if ($top.Count -eq 1 -and $top[0] -eq $pluginName) {
    Write-Host "OK: wrapped layout as expected." -ForegroundColor Green
} else {
    Write-Host "WARN: unexpected archive layout, check packaging!" -ForegroundColor Yellow
}
