# Build a transferable tarball of dpagent for a Linux target.
#
#   powershell -ExecutionPolicy Bypass -File scripts\package.ps1
#
# Produces dist\dpagent-<version>.tar.gz plus a .sha256 next to it.
#
# Staging is a copy: the source tree is never modified. Shell scripts are
# normalised to LF in the copy, because a single CRLF makes bash fail on the
# target with "$'\r': command not found" - a confusing error that says nothing
# about line endings.
#
# ASCII only, deliberately. Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# so a stray em-dash or accented character in a comment is enough to break the
# parser with an error pointing somewhere else entirely.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Name = "dpagent"

function Say { param($m) Write-Host ":: $m" -ForegroundColor Cyan }
function Ok  { param($m) Write-Host "ok $m" -ForegroundColor Green }
function Die { param($m) Write-Host "XX $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- version

$pyproject = Get-Content (Join-Path $Root "pyproject.toml") -Raw
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') { Die "no version in pyproject.toml" }
$Version = $Matches[1]
$Stem = "$Name-$Version"
Say "packaging $Stem"

# ---------------------------------------------------------------- stage

$Dist = Join-Path $Root "dist"
$Stage = Join-Path $env:TEMP ("dpagent-stage-" + [guid]::NewGuid().ToString('N').Substring(0,8))
$Out = Join-Path $Stage $Stem
New-Item -ItemType Directory -Force $Dist | Out-Null
New-Item -ItemType Directory -Force $Out | Out-Null

# What ships. "archive" and "dist" are deliberately absent: the v0.1 tarball is
# history, and nesting a package inside a package helps nobody.
$IncludeDirs = @("src", "packs", "suites", "tests", "examples", "scripts", "docs")
$IncludeFiles = @("pyproject.toml", "README.md", ".env.example", ".gitignore")

foreach ($dir in $IncludeDirs) {
    $src = Join-Path $Root $dir
    if (Test-Path $src) { Copy-Item $src $Out -Recurse -Force }
}
foreach ($file in $IncludeFiles) {
    $src = Join-Path $Root $file
    if (Test-Path $src) { Copy-Item $src $Out -Force }
}

# Drop anything that must never travel: caches, local state, secrets.
Get-ChildItem $Out -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -in @("__pycache__", ".pytest_cache", ".venv", ".env") -or
        $_.Extension -in @(".pyc", ".db", ".jsonl") -or
        $_.Name -eq "errors.proposed.yaml" -or
        $_.Name -like "*.dpagent-*.bak"
    } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path (Join-Path $Out ".env")) {
    Die "a .env file reached the staging copy - refusing to package secrets"
}

# ---------------------------------------------------------------- line endings

Say "normalising line endings in the staged copy"
$fixed = 0
Get-ChildItem $Out -Recurse -File -Include *.sh,*.yaml,*.yml,*.py,*.md,*.toml |
    ForEach-Object {
        $text = [System.IO.File]::ReadAllText($_.FullName)
        if ($text.Contains("`r`n")) {
            [System.IO.File]::WriteAllText($_.FullName, $text.Replace("`r`n", "`n"))
            $script:fixed = $script:fixed + 1
        }
    }
if ($fixed -gt 0) {
    Write-Host ("   converted {0} file(s) from CRLF to LF" -f $fixed) -ForegroundColor Yellow
} else {
    Ok "already LF throughout"
}

# ---------------------------------------------------------------- sanity

$MustExist = @(
    "src\dpagent\cli\__init__.py",
    "src\dpagent\cli\doctor.py",
    "packs\_lib\dp.sh",
    "packs\base\pack.yaml",
    "packs\python-modern\pack.yaml",
    "packs\postgres\pack.yaml",
    "packs\dbt\pack.yaml",
    "packs\airflow\pack.yaml",
    "suites\postgres\suite.yaml",
    "examples\etl-stack.yaml",
    "scripts\bootstrap.sh",
    "scripts\setup.sh",
    "pyproject.toml"
)
foreach ($rel in $MustExist) {
    if (-not (Test-Path (Join-Path $Out $rel))) { Die ("missing from the package: " + $rel) }
}
Ok "contents check passed"

# ---------------------------------------------------------------- tar

$Tarball = Join-Path $Dist ($Stem + ".tar.gz")
if (Test-Path $Tarball) { Remove-Item $Tarball -Force }

Say ("writing " + $Tarball)
# -C <stage> <stem> so the archive expands into one directory, never into cwd.
& tar -czf $Tarball -C $Stage $Stem
if ($LASTEXITCODE -ne 0) { Die "tar failed" }

$hash = (Get-FileHash $Tarball -Algorithm SHA256).Hash.ToLower()
Set-Content -Path ($Tarball + ".sha256") -Value ($hash + "  " + $Stem + ".tar.gz") -Encoding ascii

Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue

$sizeKb = [math]::Round((Get-Item $Tarball).Length / 1KB, 1)
Ok ("{0}.tar.gz  ({1} KB)" -f $Stem, $sizeKb)
Write-Host ("   sha256 " + $hash) -ForegroundColor DarkGray

Write-Host ""
Write-Host "Transfer and unpack:" -ForegroundColor DarkGray
Write-Host ("  scp dist\" + $Stem + ".tar.gz USER@HOST:/tmp/") -ForegroundColor DarkGray
Write-Host "  ssh USER@HOST" -ForegroundColor DarkGray
Write-Host ("    sha256sum /tmp/" + $Stem + ".tar.gz      # compare with the hash above") -ForegroundColor DarkGray
Write-Host ("    tar tzf /tmp/" + $Stem + ".tar.gz | head # look before extracting") -ForegroundColor DarkGray
Write-Host ""
Write-Host "Then follow docs\deploy.md - it installs nothing on its own." -ForegroundColor DarkGray
