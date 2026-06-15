param(
  [ValidateSet("smoke", "integration", "full", "extension")]
  [string]$Mode = "integration",
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8018,
  [switch]$SkipUi,
  [switch]$UseExistingServer,
  [switch]$KeepServer
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  throw "Virtual environment not found. Run .\scripts\install_windows.ps1 first."
}

$ArgsList = @(
  "scripts\auto_test.py",
  "--mode", $Mode,
  "--host", $HostName,
  "--port", [string]$Port
)

if ($SkipUi) {
  $ArgsList += "--skip-ui"
}
if ($UseExistingServer) {
  $ArgsList += "--use-existing-server"
}
if ($KeepServer) {
  $ArgsList += "--keep-server"
}

& ".venv\Scripts\python.exe" @ArgsList
exit $LASTEXITCODE
