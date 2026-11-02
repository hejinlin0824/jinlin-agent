[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Keep this contract number in sync with test-suite.json. Bump it whenever the
# runner's expected metadata contract changes; stale runner copies fail closed.
$RunnerContractVersion = 2
$MetadataPath = Join-Path $RepoRoot "test-suite.json"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $env:TEMP "jinlin-agent-test-$Stamp.log"
$Python = (Get-Command python -ErrorAction Stop).Source

function Write-TestBanner {
    param([Parameter(Mandatory = $true)]$Metadata)

    $package = $Metadata.task_package
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor DarkCyan
    Write-Host "JINLIN AGENT TEST SUITE" -ForegroundColor Cyan
    Write-Host ("Task package : {0} — {1}" -f $package.id, $package.name) -ForegroundColor Cyan
    Write-Host ("Package ver. : {0}" -f $package.version)
    Write-Host ("Suite version: {0}" -f $Metadata.suite_version)
    Write-Host ("Runner       : contract {0}" -f $RunnerContractVersion)
    Write-Host ("Repository   : {0}" -f $RepoRoot)
    Write-Host ("Branch / SHA : {0}" -f $script:GitIdentity)
    Write-Host ("Started      : {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"))
    Write-Host "============================================================" -ForegroundColor DarkCyan
}

$script:GitIdentity = "unavailable"
try {
    $branch = (& git -C $RepoRoot branch --show-current 2>$null)
    $sha = (& git -C $RepoRoot rev-parse --short HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $branch -and $sha) {
        $script:GitIdentity = "$branch @ $sha"
    }
} catch {
    $script:GitIdentity = "unavailable"
}

if (-not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) {
    throw "Test suite metadata is missing: $MetadataPath. Update the repository before running tests."
}
try {
    $Metadata = Get-Content -LiteralPath $MetadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw "Test suite metadata is invalid JSON: $($_.Exception.Message)"
}
if ($Metadata.schema_version -ne 1) {
    throw "Unsupported test-suite metadata schema: $($Metadata.schema_version)"
}
if ($Metadata.runner_contract_version -ne $RunnerContractVersion) {
    throw ("STALE TEST RUNNER: script contract {0}, metadata requires {1}. Pull the latest branch before testing." -f $RunnerContractVersion, $Metadata.runner_contract_version)
}
if (-not $Metadata.task_package.id -or -not $Metadata.task_package.name -or -not $Metadata.task_package.version) {
    throw "Test suite metadata is missing task package id/name/version."
}
if (-not $Metadata.checks -or $Metadata.checks.Count -lt 1) {
    throw "Test suite metadata must declare at least one required check."
}

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

$FinalStatus = "FAILED"
try {
    Write-TestBanner -Metadata $Metadata
    Write-Host "Python: $Python"
    & $Python --version
    if ($LASTEXITCODE -ne 0) { throw "Python is unavailable." }

    if (-not $SkipInstall) {
        Invoke-Check "Install editable package and test dependencies" @("-m", "pip", "install", "-e", ".[dev]")
    }

    Invoke-Check "Full pytest suite" @("-m", "pytest", "-q")
    Invoke-Check "Harness evaluation (all)" @("-m", "jinlin_agent", "eval", "--suite", "all")
    Invoke-Check "Rescue lifecycle end-to-end smoke test" @("scripts/rescue_smoke.py")
    Invoke-Check "Rescue fault-injection + crash + seeded-chaos matrix" @("scripts/test-rescue-fault-matrix.py")
    Invoke-Check "Rescue CLI command registration" @("-m", "jinlin_agent", "rescue", "--help")

    $FinalStatus = "ALL CHECKS PASSED"
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ("RESULT       : {0}" -f $FinalStatus) -ForegroundColor Green
    Write-Host ("Task package : {0} — {1}" -f $Metadata.task_package.id, $Metadata.task_package.name) -ForegroundColor Green
    Write-Host ("Package ver. : {0}" -f $Metadata.task_package.version)
    Write-Host ("Suite version: {0}" -f $Metadata.suite_version)
    Write-Host ("Branch / SHA : {0}" -f $script:GitIdentity)
    Write-Host ("Finished     : {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"))
    Write-Host ("Log          : {0}" -f $LogPath)
    Write-Host "============================================================" -ForegroundColor Green
}
catch {
    $FinalStatus = "FAILED"
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host ("RESULT       : {0}" -f $FinalStatus) -ForegroundColor Red
    Write-Host ("Task package : {0} — {1}" -f $Metadata.task_package.id, $Metadata.task_package.name) -ForegroundColor Red
    Write-Host ("Suite version: {0}" -f $Metadata.suite_version)
    Write-Host ("Branch / SHA : {0}" -f $script:GitIdentity)
    Write-Host ("TEST FAILED  : {0}" -f $_) -ForegroundColor Red
    Write-Host ("Log          : {0}" -f $LogPath)
    Write-Host "============================================================" -ForegroundColor Red
    exit 1
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
