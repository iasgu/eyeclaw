param(
  [switch]$SkipDependencyInstall,
  [switch]$InstallPlaywrightChromium
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

function Resolve-PythonCommand {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
      & py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
      if ($LASTEXITCODE -eq 0) {
        return @{ Exe = "py"; Args = @("-3.11") }
      }
    } catch {}
    try {
      & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
      if ($LASTEXITCODE -eq 0) {
        return @{ Exe = "py"; Args = @("-3") }
      }
    } catch {}
  }
  if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -eq 0) {
      return @{ Exe = "python"; Args = @() }
    }
  }
  throw "Python 3.11+ was not found. Install Python 3.11 or 3.12, then rerun this script."
}

function Invoke-BasePython {
  param([string[]]$Arguments)
  & $script:PythonCommand.Exe @($script:PythonCommand.Args + $Arguments)
}

$script:PythonCommand = Resolve-PythonCommand
$PythonVersion = Invoke-BasePython @("-c", "import sys; print(sys.version.split()[0])")
Write-Host "Using Python $PythonVersion"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  Write-Host "Creating virtual environment..."
  Invoke-BasePython @("-m", "venv", ".venv")
}

$VenvPython = Resolve-Path ".venv\Scripts\python.exe"

if (-not $SkipDependencyInstall) {
  Write-Host "Installing Python dependencies..."
  & $VenvPython -m pip install --upgrade pip setuptools wheel
  & $VenvPython -m pip install -r requirements.txt
  & $VenvPython -m pip check
}

if ($InstallPlaywrightChromium) {
  Write-Host "Installing Playwright Chromium into the project-local .browser folder..."
  $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot ".browser\playwright-browsers"
  & $VenvPython -m playwright install chromium
}

foreach ($dir in @("artifacts", "artifacts\downloads", "artifacts\uploads", "artifacts\session_recordings", "artifacts\task_runs", ".browser")) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example. Edit .env and fill in your API keys before running analysis/execution."
}

Write-Host ""
Write-Host "Install complete."
Write-Host "Next:"
Write-Host "  notepad .env"
Write-Host "  .\scripts\run_windows.ps1"

