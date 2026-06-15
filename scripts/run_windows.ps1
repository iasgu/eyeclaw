param(
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8018,
  [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  throw "Virtual environment not found. Run .\scripts\install_windows.ps1 first."
}

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Warning "Created .env from .env.example. Fill in API keys before using model analysis or Browser Use execution."
}

$PortInUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($PortInUse) {
  throw "Port $Port is already in use. Stop the existing service or rerun with -Port 8021."
}

foreach ($dir in @("artifacts", "artifacts\downloads", "artifacts\uploads", "artifacts\session_recordings", "artifacts\task_runs", ".browser")) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$VenvPython = Resolve-Path ".venv\Scripts\python.exe"
$AppUrl = "http://${HostName}:${Port}/app"

function Resolve-EdgePath {
  $candidates = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
    "${env:LocalAppData}\Microsoft\Edge\Application\msedge.exe"
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path $candidate)) {
      return $candidate
    }
  }
  return $null
}

if (-not $NoBrowser) {
  $EdgePath = Resolve-EdgePath
  if ($EdgePath) {
    $ProfileDir = Join-Path $ProjectRoot ".browser\edge-profile"
    $ExtensionDir = Join-Path $ProjectRoot "browser_listener_extension"
    $EdgeArgs = @(
      "--user-data-dir=$ProfileDir",
      "--remote-debugging-port=9222",
      "--load-extension=$ExtensionDir",
      "--no-first-run",
      "--disable-features=msEdgeAccountExtension,msRewards",
      $AppUrl
    )
    Start-Process -FilePath $EdgePath -ArgumentList $EdgeArgs -WindowStyle Normal
  } else {
    Write-Warning "Microsoft Edge was not found. Open $AppUrl manually after the server starts."
  }
}

Write-Host "Starting EyeClaw at $AppUrl"
Write-Host "Press Ctrl+C in this window to stop."
& $VenvPython -m uvicorn app_web:app --host $HostName --port $Port

