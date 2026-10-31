# Android Device Portal - keep the portal running without Docker.
# Used by the "AndroidDevicePortal" scheduled task (starts at logon).
# Same environment as scripts\run_host.ps1, but runs detached and logs to data\.
[CmdletBinding()]
param(
  [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root 'portal\.venv\Scripts\python.exe'
$dataDir = Join-Path $root 'data'

if (-not (Test-Path $py)) {
  throw "Portal virtualenv missing ($py). Run scripts\run_host.ps1 once to create it."
}
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$env:AGENT_URL = if ($env:AGENT_URL) { $env:AGENT_URL } else { 'http://127.0.0.1:8765' }
$env:DATA_DIR = $dataDir
$env:CONFIG_DIR = Join-Path $root 'config'
$env:HOST_DATA_DIR = $dataDir

$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^AGENT_TOKEN=(.+)$' | Select-Object -First 1
  if ($m) { $env:AGENT_TOKEN = $m.Matches[0].Groups[1].Value.Trim() }
}
if (-not $env:AGENT_TOKEN) { $env:AGENT_TOKEN = 'change-me' }

$uvicornArgs = @(
  '-m', 'uvicorn', 'app.main:app',
  '--app-dir', (Join-Path $root 'portal'),
  '--host', '127.0.0.1',
  '--port', "$Port"
)

# Start python as a child so stdout/stderr get separate log files, then stay
# alive for the task (a task restart kicks in if the server exits unexpectedly).
$proc = Start-Process -FilePath $py -ArgumentList $uvicornArgs `
  -WorkingDirectory $root -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $dataDir 'portal-host.log') `
  -RedirectStandardError (Join-Path $dataDir 'portal-host.err.log')

Write-Host "portal started on http://127.0.0.1:$Port (pid $($proc.Id))"
Wait-Process -Id $proc.Id
exit $proc.ExitCode
