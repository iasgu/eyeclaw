param(
  [int]$Port = 8018
)

$ErrorActionPreference = "Continue"
Set-StrictMode -Version Latest

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$Problems = New-Object System.Collections.Generic.List[string]

function Add-Problem {
  param([string]$Message)
  $Problems.Add($Message) | Out-Null
  Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Add-Ok {
  param([string]$Message)
  Write-Host "[ OK ] $Message" -ForegroundColor Green
}

if (Test-Path ".venv\Scripts\python.exe") {
  Add-Ok "Virtual environment exists."
  $VenvPython = Resolve-Path ".venv\Scripts\python.exe"
  $ImportCheck = @"
import importlib
mods = ["uvicorn", "starlette", "multipart", "playwright", "browser_use", "selenium", "cv2", "pydantic"]
missing = []
for mod in mods:
    try:
        importlib.import_module(mod)
    except Exception as exc:
        missing.append(f"{mod}: {exc}")
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
print("imports ok")
"@
  $TempCheck = New-TemporaryFile
  Set-Content -LiteralPath $TempCheck -Value $ImportCheck -Encoding UTF8
  $Output = & $VenvPython $TempCheck 2>&1
  Remove-Item -LiteralPath $TempCheck -Force -ErrorAction SilentlyContinue
  if ($LASTEXITCODE -eq 0) {
    Add-Ok "Core Python imports passed."
  } else {
    Add-Problem "Core Python imports failed: $Output"
  }
} else {
  Add-Problem "Virtual environment missing. Run .\scripts\install_windows.ps1."
}

if (Test-Path ".env") {
  $EnvText = Get-Content ".env" -Raw
  if ($EnvText -match "your-zhipu-api-key|your-.*api-key") {
    Add-Problem ".env still contains placeholder API keys."
  } else {
    Add-Ok ".env exists and does not contain default placeholders."
  }
} else {
  Add-Problem ".env missing. Copy .env.example to .env and fill in API keys."
}

$EdgeCandidates = @(
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
  "${env:LocalAppData}\Microsoft\Edge\Application\msedge.exe"
)
if ($EdgeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1) {
  Add-Ok "Microsoft Edge found."
} else {
  Add-Problem "Microsoft Edge not found."
}

$PortInUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($PortInUse) {
  Add-Problem "Port $Port is already in use."
} else {
  Add-Ok "Port $Port is available."
}

foreach ($dir in @("browser_listener_extension", "web", "src", "config")) {
  if (Test-Path $dir) {
    Add-Ok "$dir exists."
  } else {
    Add-Problem "$dir is missing."
  }
}

Write-Host ""
if ($Problems.Count -eq 0) {
  Write-Host "Doctor passed. Run .\scripts\run_windows.ps1" -ForegroundColor Green
  exit 0
}

Write-Host "Doctor found $($Problems.Count) issue(s)." -ForegroundColor Yellow
exit 1
