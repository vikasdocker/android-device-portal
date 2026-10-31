# Android Device Portal - run the portal WITHOUT Docker (fallback mode)
# Uses a local Python virtualenv for the portal and talks to the host agent
# on 127.0.0.1. Start the agent first (host-agent\install-agent.ps1 or run-agent.bat).
[CmdletBinding()]
param(
  [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $root 'portal\.venv'
$py = Join-Path $venv 'Scripts\python.exe'

function Resolve-BasePython {
  $c = Get-Command python -ErrorAction SilentlyContinue
  if ($c) { return @($c.Source) }
  $c = Get-Command py -ErrorAction SilentlyContinue
  if ($c) { return @($c.Source, '-3') }
  throw 'Python not found on PATH - install Python 3 first'
}

if (-not (Test-Path $py)) {
  Write-Host '[..] creating virtualenv (first run only)'
  $base = Resolve-BasePython
  $exe = $base[0]
  $args = @()
  if ($base.Count -gt 1) { $args += $base[1..($base.Count - 1)] }
  & $exe @args -m venv $venv
}

Write-Host '[..] installing portal requirements (first run only)'
& $py -m pip install --quiet --disable-pip-version-check -r (Join-Path $root 'portal\requirements.txt')

# environment for the portal process
$env:AGENT_URL = if ($env:AGENT_URL) { $env:AGENT_URL } else { 'http://127.0.0.1:8765' }
$env:DATA_DIR = Join-Path $root 'data'
$env:CONFIG_DIR = Join-Path $root 'config'
$env:HOST_DATA_DIR = Join-Path $root 'data'
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^AGENT_TOKEN=(.+)$' | Select-Object -First 1
  if ($m) { $env:AGENT_TOKEN = $m.Matches[0].Groups[1].Value.Trim() }
}
if (-not $env:AGENT_TOKEN) { $env:AGENT_TOKEN = 'change-me' }

Write-Host ''
Write-Host "portal: http://127.0.0.1:$Port   (Ctrl+C to stop)" -ForegroundColor Cyan
Write-Host ''
& $py -m uvicorn app.main:app --app-dir (Join-Path $root 'portal') --host 127.0.0.1 --port $Port
