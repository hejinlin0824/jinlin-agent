[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $env:TEMP "jinlin-agent-test-$Stamp.log"
$Python = (Get-Command python -ErrorAction Stop).Source

Start-Transcript -Path $LogPath -Force | Out-Null

function Invoke-Check {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$CommandArgs
    )
    Write-Host ""
    Write-Host "========== $Label ==========" -ForegroundColor Cyan
    & $Python @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
    Write-Host "[PASS] $Label" -ForegroundColor Green
}

try {
    Write-Host "Repository: $RepoRoot"
    Write-Host "Python: $Python"
    & $Python --version
    if ($LASTEXITCODE -ne 0) { throw "Python is unavailable." }

    if (-not $SkipInstall) {
        Invoke-Check "Install editable package and test dependencies" @("-m", "pip", "install", "-e", ".[dev]")
    }

    Invoke-Check "Full pytest suite" @("-m", "pytest", "-q")
    Invoke-Check "Harness evaluation (all)" @("-m", "jinlin_agent", "eval", "--suite", "all")
    Invoke-Check "Rescue lifecycle end-to-end smoke test" @("scripts/rescue_smoke.py")
    Invoke-Check "Rescue CLI command registration" @("-m", "jinlin_agent", "rescue", "--help")

    Write-Host ""
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
    Write-Host "Log: $LogPath"
}
catch {
    Write-Host ""
    Write-Host "TEST FAILED: $_" -ForegroundColor Red
    Write-Host "Log: $LogPath"
    Stop-Transcript | Out-Null
    exit 1
}
finally {
    if ($global:transcript -or (Test-Path $LogPath)) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}
