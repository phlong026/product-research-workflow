# Cross-border product research workbench - Windows installer (run via install.bat or directly).
# Idempotent: safe to run again. Never writes credentials; .env keeps placeholders only.
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Check] [-NonInteractive] [-NoSmoke] [-AdminPasswordEnv NAME]
param(
    [switch]$Check,
    [switch]$NonInteractive,
    [switch]$NoSmoke,
    [string]$AdminPasswordEnv = "",
    [string]$AdminUsername = "admin",
    [int]$Port = 8800
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"

function Step($message) { Write-Host "==> $message" }
function Fail($message) { Write-Host "ERROR: $message" -ForegroundColor Red; exit 1 }

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin", "$env:LOCALAPPDATA\Programs\uv")) {
        if (Test-Path "$dir\uv.exe") { $env:Path = "$dir;$env:Path"; return "$dir\uv.exe" }
    }
    return $null
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$pyArgs = @("scripts\install_customer.py", "--port", "$Port", "--admin-username", $AdminUsername)
if ($Check) { $pyArgs += "--check" }
if ($NonInteractive) { $pyArgs += "--non-interactive" }
if ($NoSmoke) { $pyArgs += "--no-smoke" }
if ($AdminPasswordEnv) { $pyArgs += @("--admin-password-env", $AdminPasswordEnv) }

if ($Check -and -not (Test-Path $python)) { Fail "Not installed yet. Run install.bat first." }

if (-not $Check) {
    $uv = Find-Uv
    if (-not $uv) {
        Step "Installing uv (Python package manager) from https://astral.sh/uv/install.ps1"
        try {
            powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        } catch {
            Write-Host "uv installer failed: $_"
        }
        $uv = Find-Uv
        if (-not $uv -and (Get-Command winget -ErrorAction SilentlyContinue)) {
            Step "Trying winget: astral-sh.uv"
            winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements
            $uv = Find-Uv
        }
        if (-not $uv) { Fail "Could not install uv. Install it manually (https://docs.astral.sh/uv/) and run install.bat again." }
    }
    Step "Using uv: $uv"

    Step "Installing Python 3.12 (managed by uv, does not touch your system Python)"
    & $uv python install 3.12
    if ($LASTEXITCODE -ne 0) { Fail "uv python install failed (network/proxy?)." }

    Step "Installing locked dependencies into .venv (first run downloads ~200 MB)"
    & $uv sync --frozen --python 3.12 --extra workflow --extra data --extra xlsx --extra llm
    if ($LASTEXITCODE -ne 0) { Fail "uv sync failed (network/proxy?). Re-run install.bat after fixing the network." }
}

Step "Configuring and self-checking"
& $python @pyArgs
if ($LASTEXITCODE -ne 0) { Fail "Self-check did not pass. See the JSON report above." }
exit 0
