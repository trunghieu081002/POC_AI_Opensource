# Set up a Windows machine to develop dpagent and run its test suite.
#
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap-dev.ps1
#
# This machine is not a target. dpagent installs onto Linux hosts with systemd
# and bash; what you can do here is run the engine tests (pure Python) and
# exercise planning with --fake-os. Applying a pack needs a VM.
#
# ASCII only, deliberately: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# so one non-ASCII character in a comment can break the parser.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Say  { param($m) Write-Host ":: $m" -ForegroundColor Cyan }
function Ok   { param($m) Write-Host "ok $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "!! $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "XX $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- python

function Get-RealPython {
    # The Microsoft Store aliases in WindowsApps are stubs: they resolve as
    # commands but only open the Store. Anything under that path does not count.
    foreach ($name in @("py", "python3", "python")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if ($cmd.Source -like "*\WindowsApps\*") { continue }
        try {
            $ver = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($ver -match '^3\.(1[0-9]|[2-9][0-9])$') { return @{ Path = $cmd.Source; Version = $ver } }
    }
    return $null
}

$py = Get-RealPython
if ($null -eq $py) {
    Say "no real Python 3.10+ found (Store aliases do not count)"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Die @"
Python is missing and winget is not available to install it.
Install Python 3.12 from https://www.python.org/downloads/ - tick
"Add python.exe to PATH" - then re-run this script.
"@
    }
    Say "installing Python 3.12 via winget"
    & winget install --id Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements --silent
    Warn "winget updates PATH for NEW shells only - close this terminal and re-run this script"
    exit 0
}
Ok "python $($py.Version) at $($py.Path)"

# ---------------------------------------------------------------- venv

$venv = Join-Path $Root ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Say "creating the virtualenv at .venv"
    & $py.Path -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"

Say "installing dpagent in editable mode with dev extras"
& $vpy -m pip install --quiet --upgrade pip setuptools wheel
& $vpy -m pip install --quiet -e "$Root[dev]"
Ok "dependencies installed"

# ---------------------------------------------------------------- optional: git

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "git is not installed - you cannot version-control packs or run the clone path in bootstrap.sh"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    install it with: winget install --id Git.Git" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------- checks

Say "running the engine test suite"
& $vpy -m pytest
if ($LASTEXITCODE -ne 0) { Die "tests failed - see the output above" }
Ok "tests passed"

Say "linting the pack library"
& $vpy -m dpagent lint
if ($LASTEXITCODE -ne 0) { Warn "lint reported blocking issues" }

if (-not (Get-Command bash -ErrorAction SilentlyContinue)) {
    Warn @"
bash is not on this machine, so 'dpagent lint' SKIPPED every shell syntax check.
A clean lint here does not mean the pack scripts parse. They are only really
checked on a Linux host. Install Git for Windows to get bash, or rely on the VM.
"@
}

Write-Host ""
Ok "dev environment ready"
Write-Host @"

  .\.venv\Scripts\Activate.ps1
  dpagent info
  dpagent install postgres --dry-run --fake-os ubuntu

Applying a pack for real needs a Linux VM (Ubuntu 22 or Oracle Linux 9):

  scp scripts\bootstrap.sh root@vm:/tmp/ ; ssh root@vm 'bash /tmp/bootstrap.sh'
"@ -ForegroundColor DarkGray
