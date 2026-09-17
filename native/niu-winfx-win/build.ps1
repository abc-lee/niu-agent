# build.ps1 — publish the niu-winfx-win native component (route A: zero-content WinUI 3
# material window, separate process) and drop the distributable output into
# ui/main/native/winfx/. Fails hard (exit 1) when the output is incomplete —
# never ship a partial package.
$ErrorActionPreference = "Stop"

$projDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $projDir)
$dst      = Join-Path $repoRoot "ui\main\native\winfx"
$out      = Join-Path $projDir "publish"

Write-Host "[build] project: $projDir"

# dotnet publish -c Release -r win-x64 (project already pins Platforms=x64, RID, UseWinUI,
# WindowsPackageType=None, Microsoft.WindowsAppSDK 2.4.0, TFM net8.0-windows10.0.19041.0)
& dotnet publish $projDir -c Release -r win-x64 -o $out --nologo
if ($LASTEXITCODE -ne 0) {
    Write-Error "[build] dotnet publish failed (exit $LASTEXITCODE)"
    exit 1
}

# Completeness guard on the publish output before touching the drop target.
$required = @("WinFx.exe", "WinFx.dll", "Microsoft.WinUI.dll", "Microsoft.WindowsAppRuntime.Bootstrap.dll")
$missing = @()
foreach ($f in $required) {
    if (-not (Test-Path (Join-Path $out $f))) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Error ("[build] publish output incomplete, missing: " + ($missing -join ", "))
    exit 1
}

# Fresh drop: remove stale target, copy the full publish output.
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
New-Item -ItemType Directory -Path $dst -Force | Out-Null
Copy-Item (Join-Path $out "*") $dst -Recurse -Force

# Verify the drop before declaring success; on failure remove the partial target.
$dropped = @()
foreach ($f in $required) {
    if (-not (Test-Path (Join-Path $dst $f))) { $dropped += $f }
}
if ($dropped.Count -gt 0) {
    Write-Error ("[build] drop verification failed, missing: " + ($dropped -join ", "))
    Remove-Item $dst -Recurse -Force
    exit 1
}

$files = Get-ChildItem $dst -Recurse -File
$exe   = Get-Item (Join-Path $dst "WinFx.exe")
Write-Host ("[build] OK: " + $files.Count + " files (" + [math]::Round(($files | Measure-Object Length -Sum).Sum / 1MB, 1) + " MB) in " + $dst)
Write-Host ("[build]   " + $exe.FullName + "  " + $exe.Length + " bytes")
exit 0
