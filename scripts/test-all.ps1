[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$Fast,
    [string]$PythonPath = "",
    [string]$ReportDirectory = "reports/local"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot
$RunnerContractVersion = 3
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$StartedAt = Get-Date
$ReportRoot = Join-Path $RepoRoot $ReportDirectory
$null = New-Item -ItemType Directory -Path $ReportRoot -Force
$LogPath = Join-Path $ReportRoot "test-$Stamp.log"
$JUnitPath = Join-Path $ReportRoot "junit-$Stamp.xml"
$JsonReportPath = Join-Path $ReportRoot "report-$Stamp.json"
$script:RunResults = New-Object System.Collections.Generic.List[object]
$script:PythonExecutable = $null
$script:GitIdentity = "unavailable"
$script:Metadata = $null
$script:TranscriptStarted = $false
$script:OverallFailure = $false

function Get-GitIdentity {
    try {
        $branch = (& git -C $RepoRoot branch --show-current 2>$null | Select-Object -First 1)
        $sha = (& git -C $RepoRoot rev-parse --short HEAD 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -eq 0 -and $branch -and $sha) { return "$branch @ $sha" }
    } catch {}
    return "unavailable"
}

function Write-Banner {
    param([string]$Status = "RUNNING")
    $package = $script:Metadata.task_package
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor DarkCyan
    Write-Host "JINLIN AGENT VALIDATION — $Status" -ForegroundColor Cyan
    Write-Host ("Task package : {0} — {1}" -f $package.id, $package.name)
    Write-Host ("Package ver. : {0}" -f $package.version)
    Write-Host ("Suite version: {0}" -f $script:Metadata.suite_version)
    Write-Host ("Runner       : contract {0}" -f $RunnerContractVersion)
    Write-Host ("Repository   : {0}" -f $RepoRoot)
    Write-Host ("Branch / SHA : {0}" -f $script:GitIdentity)
    Write-Host ("Python       : {0}" -f $script:PythonExecutable)
    Write-Host ("Started      : {0}" -f $StartedAt.ToString("yyyy-MM-dd HH:mm:ss zzz"))
    Write-Host "============================================================" -ForegroundColor DarkCyan
}

function Invoke-Check {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host ""
    Write-Host ("========== {0} ==========" -f $Label) -ForegroundColor Cyan
    Write-Host ("$ {0} {1}" -f $Executable, ($Arguments -join " "))
    $exitCode = 999
    try {
        & $Executable @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 0 }
    } catch {
        Write-Host ("[ERROR] {0}" -f $_) -ForegroundColor Red
        $exitCode = 998
    }
    $watch.Stop()
    $status = if ($exitCode -eq 0) { "passed" } else { "failed" }
    $script:RunResults.Add([ordered]@{
        name = $Label
        status = $status
        exit_code = [int]$exitCode
        duration_ms = [long]$watch.ElapsedMilliseconds
        command = $Executable
        arguments = $Arguments
    }) | Out-Null
    if ($exitCode -eq 0) {
        Write-Host ("[PASS] {0} ({1} ms)" -f $Label, $watch.ElapsedMilliseconds) -ForegroundColor Green
        return $true
    }
    $script:OverallFailure = $true
    Write-Host ("[FAIL] {0} (exit {1}, {2} ms)" -f $Label, $exitCode, $watch.ElapsedMilliseconds) -ForegroundColor Red
    return $false
}

function Write-JsonReport {
    param([string]$Status, [string]$ErrorMessage = "")
    $finished = Get-Date
    $report = [ordered]@{
        schema_version = 1
        status = $Status
        task_package = $script:Metadata.task_package
        suite_version = $script:Metadata.suite_version
        runner_contract_version = $RunnerContractVersion
        repository = $RepoRoot
        branch_and_commit = $script:GitIdentity
        python = $script:PythonExecutable
        started_at = $StartedAt.ToString("o")
        finished_at = $finished.ToString("o")
        duration_ms = [long]($finished - $StartedAt).TotalMilliseconds
        log = $LogPath
        junit = if (Test-Path -LiteralPath $JUnitPath) { $JUnitPath } else { $null }
        error = $ErrorMessage
        checks = @($script:RunResults)
    }
    $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $JsonReportPath -Encoding UTF8
}

$FinalExitCode = 1
$FailureMessage = ""
try {
    $script:GitIdentity = Get-GitIdentity
    if ($PythonPath) {
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "PythonPath does not exist: $PythonPath" }
        $script:PythonExecutable = $PythonPath
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw "Python 3.11+ was not found. Install Python or activate your virtual environment first." }
        $script:PythonExecutable = $pythonCommand.Source
    }

    $metadataPath = Join-Path $RepoRoot "test-suite.json"
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { throw "Test suite metadata is missing: $metadataPath" }
    $script:Metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($script:Metadata.schema_version -ne 1) { throw "Unsupported test-suite schema: $($script:Metadata.schema_version)" }
    if ($script:Metadata.runner_contract_version -ne $RunnerContractVersion) {
        throw ("STALE TEST RUNNER: script contract {0}, metadata requires {1}. Pull the latest branch." -f $RunnerContractVersion, $script:Metadata.runner_contract_version)
    }
    if (-not $script:Metadata.task_package.id -or -not $script:Metadata.task_package.name -or -not $script:Metadata.task_package.version) {
        throw "Metadata must declare task_package id, name and version."
    }
    if (-not $script:Metadata.checks -or $script:Metadata.checks.Count -lt 1) {
        throw "Metadata must declare at least one validation check."
    }

    try {
        Start-Transcript -Path $LogPath -Force | Out-Null
        $script:TranscriptStarted = $true
    } catch {
        throw "Cannot create test transcript at '$LogPath': $($_.Exception.Message)"
    }

    Write-Banner
    if (-not (Invoke-Check "Python version and runtime preflight" $script:PythonExecutable @(
        "-c", "import sys; print(sys.executable); print(sys.version); assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
    ))) { throw "Python runtime preflight failed." }

    if (-not $SkipInstall -and -not $Fast) {
        if (-not (Invoke-Check "Install editable package and test dependencies" $script:PythonExecutable @(
            "-m", "pip", "install", "-e", ".[dev]"
        ))) { throw "Dependency installation failed; remaining checks would be unreliable." }
    }

    if ($Fast) {
        $null = Invoke-Check "Compile source and tests" $script:PythonExecutable @("-m", "compileall", "-q", "src", "tests")
        $null = Invoke-Check "Focused Harness and Workbench regression tests" $script:PythonExecutable @(
            "-m", "pytest", "-q",
            "tests/test_smoke.py",
            "tests/test_read_only_workflow.py",
            "tests/test_loop_final_answer.py",
            "tests/test_workbench_tasks.py",
            "tests/test_test_runner_metadata.py"
        )
    } else {
        $null = Invoke-Check "Compile source and tests" $script:PythonExecutable @("-m", "compileall", "-q", "src", "tests")
        $null = Invoke-Check "Full pytest suite (JUnit report)" $script:PythonExecutable @(
            "-m", "pytest", "-q", "--junitxml=$JUnitPath"
        )
        $null = Invoke-Check "Harness conformance evaluation (all offline probes)" $script:PythonExecutable @(
            "-m", "jinlin_agent", "eval", "--suite", "all"
        )
        $null = Invoke-Check "Rescue lifecycle end-to-end smoke test" $script:PythonExecutable @("scripts/rescue_smoke.py")
        $null = Invoke-Check "Rescue fault-injection, crash and seeded-chaos matrix" $script:PythonExecutable @("scripts/test-rescue-fault-matrix.py")
        $null = Invoke-Check "CLI root command registration" $script:PythonExecutable @("-m", "jinlin_agent", "--help")
        $null = Invoke-Check "CLI Rescue command registration" $script:PythonExecutable @("-m", "jinlin_agent", "rescue", "--help")
        if (Get-Command git -ErrorAction SilentlyContinue) {
            $null = Invoke-Check "Git whitespace/conflict-marker check" "git" @("-C", $RepoRoot, "diff", "--check")
        }
    }

    if ($script:OverallFailure) {
        $FailureMessage = "One or more validation checks failed. Review the check summary and transcript."
        $FinalExitCode = 1
    } else {
        $FinalExitCode = 0
    }
}
catch {
    $FailureMessage = $_.Exception.Message
    $script:OverallFailure = $true
    Write-Host ""
    Write-Host ("[FATAL] {0}" -f $FailureMessage) -ForegroundColor Red
    $FinalExitCode = 1
}
finally {
    if ($script:TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
    if ($script:Metadata) {
        $status = if ($FinalExitCode -eq 0) { "passed" } else { "failed" }
        try { Write-JsonReport -Status $status -ErrorMessage $FailureMessage } catch {
            Write-Host ("[WARN] Could not write JSON report: {0}" -f $_) -ForegroundColor Yellow
        }
        Write-Host ""
        if ($FinalExitCode -eq 0) {
            Write-Host "====================== ALL CHECKS PASSED ======================" -ForegroundColor Green
        } else {
            Write-Host "========================= TEST FAILED =========================" -ForegroundColor Red
        }
        Write-Host ("Passed : {0}" -f (@($script:RunResults | Where-Object status -eq "passed").Count))
        Write-Host ("Failed : {0}" -f (@($script:RunResults | Where-Object status -eq "failed").Count))
        Write-Host ("Log    : {0}" -f $LogPath)
        Write-Host ("Report : {0}" -f $JsonReportPath)
        if (Test-Path -LiteralPath $JUnitPath) { Write-Host ("JUnit  : {0}" -f $JUnitPath) }
        Write-Host ("Branch : {0}" -f $script:GitIdentity)
        Write-Host ("Started: {0}" -f $StartedAt.ToString("yyyy-MM-dd HH:mm:ss zzz"))
    }
}
exit $FinalExitCode
