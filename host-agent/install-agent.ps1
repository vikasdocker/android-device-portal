# Android Device Portal - host agent installer
#  * generates a shared token (.env for docker compose)
#  * writes host-agent\agent-config.json
#  * adds a firewall rule for the portal container (needs admin once)
#  * registers a scheduled task so the agent starts at every logon
#  * starts the agent right away and verifies it responds
[CmdletBinding()]
param(
  [int]$Port = 8765,
  [switch]$NoTask,
  [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$agentDir = $PSScriptRoot
$root = Split-Path $agentDir -Parent

Write-Host ''
Write-Host '=== Android Device Portal - host agent installer ===' -ForegroundColor Cyan

# --- token + .env (docker compose reads this) --------------------------------
$envFile = Join-Path $root '.env'
$token = $null
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^AGENT_TOKEN=(.+)$' | Select-Object -First 1
  if ($m) { $token = $m.Matches[0].Groups[1].Value.Trim() }
}
if (-not $token) {
  $token = [guid]::NewGuid().ToString('N')
  Write-Host '[new] generated a fresh agent token'
} else {
  Write-Host '[ok ] reusing existing agent token from .env'
}
@(
  "AGENT_URL=http://host.docker.internal:$Port",
  "AGENT_TOKEN=$token",
  "HOST_DATA_DIR=$root\data"
) | Set-Content -Path $envFile -Encoding ASCII
Write-Host "[ok ] wrote $envFile"

# --- SDK + java detection ----------------------------------------------------
$sdk = if ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT }
       elseif ($env:ANDROID_HOME) { $env:ANDROID_HOME }
       else { Join-Path $env:LOCALAPPDATA 'Android\Sdk' }
if (-not (Test-Path (Join-Path $sdk 'emulator\emulator.exe'))) {
  Write-Warning "emulator.exe not found under $sdk - check the SDK path"
}

$java = ''
foreach ($base in (Get-ChildItem (Join-Path $root 'tools') -Directory -Filter 'jdk*' -ErrorAction SilentlyContinue)) {
  $j = Get-ChildItem $base.FullName -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
       Where-Object { $_.DirectoryName -like '*\bin' } | Select-Object -First 1
  if ($j) { $java = $j.FullName; break }
}
if (-not $java) {
  foreach ($cand in @(
      'C:\Program Files\Android\Android Studio\jbr\bin\java.exe',
      (Join-Path $env:LOCALAPPDATA 'Programs\Android Studio\jbr\bin\java.exe'))) {
    if (Test-Path $cand) { $java = $cand; break }
  }
}
if (-not $java) {
  $jc = Get-Command java -ErrorAction SilentlyContinue
  if ($jc) { $java = $jc.Source }
}
if ($java) { Write-Host "[ok ] java: $java" } else {
  Write-Warning 'no Java found - AAB mode needs it. Run scripts\setup_devices.ps1 once (installs a JDK).'
}

# --- agent config -------------------------------------------------------------
$cfg = [ordered]@{
  port            = $Port
  token           = $token
  sdk_root        = $sdk
  data_root       = (Join-Path $root 'data')
  java_exe        = $java
  bundletool_jar  = (Join-Path $root 'tools\bundletool.jar')
  serial_port     = 5554
  emulator        = @{
    memory_mb            = 3072
    cores                = 4
    gpu                  = 'auto'
    fallback_gpu         = 'swiftshader_indirect'
    headless             = $false
    extra_args           = @()
    boot_timeout_seconds = 300
    boot_attempts        = 3
    recovery_wipe        = $true
  }
  limits          = @{
    min_free_ram_mb   = 3000
    min_free_disk_gb  = 12
    ram_headroom_mb   = 1024
    ram_headroom_4gb_mb = 512
  }
}
$cfgJson = $cfg | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $agentDir 'agent-config.json'), $cfgJson, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "[ok ] wrote $agentDir\agent-config.json"
New-Item -ItemType Directory -Force -Path (Join-Path $root 'data\runs') | Out-Null

# --- firewall rule (portal container -> host) ----------------------------------
$ruleName = "Android Device Portal Agent (TCP $Port)"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
  Write-Host '[ok ] firewall rule already exists'
} elseif ($isAdmin) {
  New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private,Domain | Out-Null
  Write-Host '[ok ] firewall rule added'
} else {
  Write-Warning "not running as admin - add the firewall rule manually (required for the Docker portal, skip if using the no-Docker mode):"
  Write-Host "   New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private,Domain" -ForegroundColor Yellow
}

# --- python / pythonw ----------------------------------------------------------
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { throw 'Python not found on PATH. Install Python 3 (https://www.python.org/downloads/) and re-run.' }
$pyExe = $pyCmd.Source
$pyw = Join-Path (Split-Path $pyExe -Parent) 'pythonw.exe'
$runExe = if (Test-Path $pyw) { $pyw } else { $pyExe }

# --- scheduled task ------------------------------------------------------------
if (-not $NoTask) {
  $action = New-ScheduledTaskAction -Execute $runExe -Argument "`"$agentDir\agent.py`"" -WorkingDirectory $agentDir
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $settingsSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName 'AndroidDevicePortalAgent' -Action $action -Trigger $trigger `
    -Settings $settingsSet -Description 'Android Device Portal host agent - drives emulators for the portal' -Force | Out-Null
  Write-Host '[ok ] scheduled task "AndroidDevicePortalAgent" registered (starts at logon)'
}

# --- start + verify ------------------------------------------------------------
if (-not $NoStart) {
  $alreadyUp = $false
  try {
    Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Headers @{ 'X-Agent-Token' = $token } -TimeoutSec 3 | Out-Null
    $alreadyUp = $true
  } catch { }
  if (-not $alreadyUp) {
    Write-Host '[..] starting agent now'
    Start-Process -FilePath $runExe -ArgumentList "`"$agentDir\agent.py`"" -WorkingDirectory $agentDir -WindowStyle Hidden
  }
  $health = $null
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
      $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Headers @{ 'X-Agent-Token' = $token } -TimeoutSec 4
      break
    } catch { }
  }
  if ($health) {
    Write-Host ''
    Write-Host '[PASS] agent is responding!' -ForegroundColor Green
    Write-Host ("   SDK:       {0}" -f $health.sdk_root)
    Write-Host ("   emulator:  {0}" -f $health.emulator_found)
    Write-Host ("   AVDs:      {0}" -f (($health.avds | Measure-Object).Count))
    Write-Host ("   RAM free:  {0} MB" -f $health.free_ram_mb)
    Write-Host ("   java:      {0}" -f $health.java)
  } else {
    Write-Warning "agent did not answer on port $Port - run host-agent\run-agent.bat to see its console output"
  }
}

Write-Host ''
Write-Host 'Next:  check everything with  scripts\doctor.ps1' -ForegroundColor Cyan
Write-Host '       then start the portal:  docker compose up -d   ->  http://localhost:8080' -ForegroundColor Cyan
