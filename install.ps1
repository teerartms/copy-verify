<#
.SYNOPSIS
    Set up copy-verify on Windows.

.DESCRIPTION
    Creates a local virtual environment in .venv, installs the dependencies,
    and drops copyverify.cmd / verify-copy.cmd launchers into a bin directory
    (default: %LOCALAPPDATA%\Programs\copy-verify), adding it to your user PATH.

.PARAMETER NoLaunchers
    Skip creating the .cmd launchers and the PATH change.

.PARAMETER BinDir
    Directory for the launcher scripts.

.PARAMETER Python
    Explicit Python executable to use (default: auto-detected via py / python).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
#>
[CmdletBinding()]
param(
    [switch] $NoLaunchers,
    [string] $BinDir = (Join-Path $env:LOCALAPPDATA 'Programs\copy-verify'),
    [string] $Python
)

$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RepoDir '.venv'

function Find-Python {
    param([string] $Explicit)
    if ($Explicit) { return $Explicit }
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try { & py -3.10 -c 'pass' 2>$null; if ($LASTEXITCODE -eq 0) { return 'py -3' } } catch {}
        return 'py -3'
    }
    foreach ($cand in @('python', 'python3')) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { return $cand }
    }
    return $null
}

$pyCmd = Find-Python -Explicit $Python
if (-not $pyCmd) {
    Write-Error 'No Python interpreter found. Install Python 3.10+ from https://python.org and retry.'
    exit 1
}

# Split "py -3" into file + args so we can invoke it via the call operator.
$pyParts = @($pyCmd -split ' ')
$pyExe   = $pyParts[0]
if ($pyParts.Length -gt 1) { $pyArgs = $pyParts[1..($pyParts.Length - 1)] } else { $pyArgs = @() }

# Note: native-command args are passed WITHOUT embedded quotes here because
# Windows PowerShell 5.1 strips double quotes when handing them to an .exe.
& $pyExe @pyArgs -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)'
if ($LASTEXITCODE -ne 0) {
    $found = & $pyExe @pyArgs -c 'import sys; print(sys.version.split()[0])'
    Write-Error "Python 3.10+ required, found $found."
    exit 1
}
$pyVer = & $pyExe @pyArgs -c 'import sys; print(sys.version.split()[0])'
Write-Host "Using Python $pyVer ($pyCmd)"

# --- virtual environment ---------------------------------------------------
if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating virtual environment in $VenvDir"
    & $pyExe @pyArgs -m venv $VenvDir
} else {
    Write-Host "Reusing existing virtual environment in $VenvDir"
}

$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
& $VenvPy -m pip install --upgrade pip | Out-Null
Write-Host 'Installing dependencies...'
& $VenvPy -m pip install -r (Join-Path $RepoDir 'requirements.txt')

# --- smoke test ----------------------------------------------------------
& $VenvPy (Join-Path $RepoDir 'copyverify.py') --version
& $VenvPy (Join-Path $RepoDir 'verify_copy.py') --version

# --- launchers ---------------------------------------------------------
if (-not $NoLaunchers) {
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

    $cvCmd = @"
@echo off
"$VenvPy" "$RepoDir\copyverify.py" %*
"@
    $vcCmd = @"
@echo off
"$VenvPy" "$RepoDir\verify_copy.py" %*
"@
    Set-Content -Path (Join-Path $BinDir 'copyverify.cmd')  -Value $cvCmd -Encoding ascii
    Set-Content -Path (Join-Path $BinDir 'verify-copy.cmd') -Value $vcCmd -Encoding ascii

    Write-Host ''
    Write-Host 'Launchers installed:'
    Write-Host "  $BinDir\copyverify.cmd"
    Write-Host "  $BinDir\verify-copy.cmd"

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (($userPath -split ';') -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$BinDir", 'User')
        Write-Host ''
        Write-Host "Added $BinDir to your user PATH. Open a new terminal to pick it up."
    }
}

Write-Host ''
Write-Host 'Done. Try:'
Write-Host '  copyverify <src> <dst>'
Write-Host '  verify-copy --manifest <dst>\copyverify-manifest.json <dst>'
Write-Host ''
Write-Host 'Or without launchers:'
Write-Host "  $VenvPy $RepoDir\copyverify.py <src> <dst>"
