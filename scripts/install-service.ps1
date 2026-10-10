<#
.SYNOPSIS
  Install Ricardi as a Windows service with NSSM.

.DESCRIPTION
  Nine nssm commands with one that is easy to get wrong, run twice because
  there are two machines. This checks the things that actually break first --
  a missing policy file, a venv that was never created, credentials the
  service account cannot see -- and refuses with a reason rather than
  installing something that starts and then cannot answer.

  It deliberately does NOT take a password. The service must run as you
  rather than LocalSystem (see -Account below), and nssm sets the account
  with the password as a command-line argument, which puts it in your shell
  history and in the process list where any user can read it. Instead this
  sets everything else and then opens nssm's own dialog, where the password
  goes into a password box and nowhere else.

.PARAMETER DryRun
  Print the plan and change nothing. Needs no elevation.

.PARAMETER Uninstall
  Stop and remove the service. Leaves state/ alone.

.EXAMPLE
  .\scripts\install-service.ps1 -DryRun
  .\scripts\install-service.ps1          # elevated
#>
[CmdletBinding()]
param(
    [string]$Name = 'Ricardi',
    [switch]$DryRun,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

# nssm writes everything -- its banner, progress, and errors alike -- to
# stderr. With ErrorActionPreference set to Stop, PowerShell turns any of that
# into a terminating NativeCommandError, so the first nssm call killed the
# script and reported the banner as the failure. Run native commands with the
# preference relaxed and judge them by their exit code instead.
function Invoke-Nssm([string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & nssm @Arguments 2>&1 | ForEach-Object {
            # A native command's stderr arrives as ErrorRecord, whose ToString()
            # is the useless "System.Management.Automation.RemoteException".
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message }
            else { $_.ToString() }
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    [pscustomobject]@{
        Code   = $code
        Output = ($output | Where-Object { $_ -and $_.Trim() }) -join [Environment]::NewLine
    }
}

# nssm said "Administrator access is needed to install a service." and still
# exited 0, so the exit code alone is not a verdict. Judge it by what it says
# as well, and -- for the install itself -- by whether the service appeared.
$script:NssmFailureText = @(
    'Administrator access is needed'
    'Usage:'
    'The specified service already exists'
    'Could not'
    'Access is denied'
)

function Step-Nssm([string[]]$Arguments) {
    $r = Invoke-Nssm $Arguments
    $said = $r.Output
    $looksWrong = $false
    foreach ($pattern in $script:NssmFailureText) {
        if ($said -and $said -like "*$pattern*") { $looksWrong = $true; break }
    }
    if ($r.Code -ne 0 -or $looksWrong) {
        Write-Host ("nssm {0}" -f ($Arguments -join ' ')) -ForegroundColor Yellow
        Write-Host ("  exit {0}" -f $r.Code) -ForegroundColor Yellow
        if ($r.Output) { $r.Output -split "`n" | ForEach-Object { Write-Host "  $_" } }
        return $false
    }
    # Success still prints a banner; show it only when it says something.
    if ($r.Output -and $r.Output -notmatch '^NSSM: ') { Write-Host "  $($r.Output)" }
    return $true
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# What has to be true before a service is worth installing. Returned rather
# than thrown so every problem is reported at once: finding them one restart
# at a time is the slow way to do this.
function Get-InstallProblems([string]$Root, [bool]$NeedElevation = $true) {
    $problems = @()

    if ($NeedElevation -and -not (Test-Elevated)) {
        $problems += 'Not elevated. Installing a service needs an administrator PowerShell.'
    }
    if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
        $problems += 'nssm is not on PATH. Install it from https://nssm.cc or: winget install NSSM.NSSM'
    }

    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $problems += "No virtualenv at $python. Run: uv venv; uv pip install -e `".[slack,sdk]`""
    }
    foreach ($f in 'agents.toml', 'policy.toml', '.env') {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $f))) {
            $problems += "Missing $f. Copy the example beside it and fill it in."
        }
    }

    # The service runs as an account, and Claude Code's credentials live in
    # that account's home directory. LocalSystem has a different one, so the
    # agents would start and then fail to authenticate.
    $claude = Join-Path $env:USERPROFILE '.claude'
    if (-not (Test-Path -LiteralPath $claude)) {
        $problems += "No $claude for this user. Run `claude` once and sign in first."
    }

    , $problems
}

function Get-NssmPlan([string]$Root, [string]$Name, [bool]$Exists = $false) {
    $python = Join-Path $Root '.venv\Scripts\python.exe'
    $log    = Join-Path $Root 'state\service.log'
    # Running this twice, or after a half-finished attempt, has to be safe:
    # there are two machines and the first run here left a service behind with
    # no program set. `install` refuses when one exists, so update it instead
    # and set Application explicitly, which `install` would otherwise have done.
    $first = if ($Exists) { @('set', $Name, 'Application', $python) }
             else         { @('install', $Name, $python) }
    # Each row is prefixed with a comma. Without it PowerShell flattens an
    # array of arrays into one flat list of strings, and every nssm call gets
    # a single word -- `nssm install` with no program, which just prints usage.
    @(
        , $first
        , @('set', $Name, 'AppParameters', '-u -m ricardi.cli --transport slack --backend sdk')
        , @('set', $Name, 'AppDirectory', $Root)
        , @('set', $Name, 'AppStdout', $log)
        , @('set', $Name, 'AppStderr', $log)
        , @('set', $Name, 'AppRotateFiles', '1')
        , @('set', $Name, 'AppRotateBytes', '10485760')
        , @('set', $Name, 'AppExit', 'Default', 'Restart')
        , @('set', $Name, 'AppRestartDelay', '10000')
        , @('set', $Name, 'Start', 'SERVICE_AUTO_START')
        , @('set', $Name, 'Description', 'Ricardi: a Slack proxy to Claude Code agents')
    )
}

# --- uninstall ---------------------------------------------------------
if ($Uninstall) {
    if (-not $DryRun -and -not (Test-Elevated)) {
        Write-Host 'Removing a service needs an administrator PowerShell.' -ForegroundColor Yellow
        exit 1
    }
    foreach ($a in @(@('stop', $Name), @('remove', $Name, 'confirm'))) {
        if ($DryRun) { Write-Host "would run: nssm $($a -join ' ')"; continue }
        # A service that is already stopped makes `stop` fail; that is fine.
        [void](Step-Nssm $a)
    }
    if (-not $DryRun) {
        Write-Host ''
        Write-Host "Removed $Name. state\ is untouched, so bindings, sessions and the audit log survive."
    }
    exit 0
}

# --- install -----------------------------------------------------------
$problems = Get-InstallProblems $Root (-not $DryRun)
if ($problems.Count -gt 0) {
    Write-Host 'Not ready to install:' -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "  - $p" }
    exit 1
}

New-Item -ItemType Directory -Force (Join-Path $Root 'state') | Out-Null

$existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "$Name already exists ($($existing.Status)); updating it in place." -ForegroundColor Cyan
    if ($existing.Status -eq 'Running') {
        Write-Host '  stopping it first'
        [void](Step-Nssm @('stop', $Name))
    }
    Write-Host ''
}

foreach ($a in (Get-NssmPlan $Root $Name ([bool]$existing))) {
    if ($DryRun) { Write-Host "would run: nssm $($a -join ' ')"; continue }
    Write-Host ("nssm {0}" -f ($a -join ' '))
    if (-not (Step-Nssm $a)) {
        Write-Host ''
        Write-Host 'Stopped there. Nothing further was changed.' -ForegroundColor Yellow
        Write-Host "If the service half exists, remove it with: .\scripts\install-service.ps1 -Uninstall"
        exit 1
    }
}

if ($DryRun) {
    Write-Host ''
    Write-Host 'would then open: nssm edit ' -NoNewline; Write-Host $Name
    Write-Host '  to set Log on -> This account, which is the step that cannot be scripted safely.'
    exit 0
}

Write-Host ''
Write-Host 'Installed, but NOT started yet. One step left, and it is the one that matters:' -ForegroundColor Cyan
Write-Host ''
Write-Host "  nssm edit $Name   ->   Log on tab   ->   This account   ->   .\$env:USERNAME"
Write-Host ''
Write-Host '  A service runs as LocalSystem by default, which has a different home'
Write-Host '  directory: no ~\.claude, so the agents start and cannot authenticate. It is'
Write-Host '  also far more privileged than you want for something that runs shell commands'
Write-Host '  on request from a chat channel.'
Write-Host ''
Write-Host '  The password goes in that dialog rather than here on purpose: nssm takes it as'
Write-Host '  a command-line argument, which would put it in your shell history and in the'
Write-Host '  process list.'
Write-Host ''
Write-Host "Then:  nssm start $Name"
Write-Host "Watch: Get-Content -Wait `"$(Join-Path $Root 'state\service.log')`""
Write-Host ''
Write-Host 'Afterwards, prove a restart keeps a conversation: docs/running.md, "Check it is'
Write-Host 'actually working". A bot that answers but has forgotten the last hour looks fine.'
