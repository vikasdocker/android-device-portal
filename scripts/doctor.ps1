# Android Device Portal - environment doctor (read-only checks)
[CmdletBinding()]
param(
  [string]$SdkRoot = ''
)

$root = Split-Path $PSScriptRoot -Parent
$results = @()
function Check([string]$name, [string]$state, [string]$detail) {
  $script:results += [pscustomobject]@{ Name = $name; State = $state; Detail = $detail }
  $color = switch ($state) { 'PASS' { 'Green' } 'WARN' { 'Yellow' } default { 'Red' } }
  Write-Host ("[{0,-4}] {1,-28} {2}" -f $state, $name, $detail) -ForegroundColor $color
}

Write-Host ''
Write-Host '=== Android Device Portal - doctor ===' -ForegroundColor Cyan
Write-Host ''

# --- SDK ---------------------------------------------------------------------
if (-not $SdkRoot) {
  $SdkRoot = if ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT }
             elseif ($env:ANDROID_HOME) { $env:ANDROID_HOME }
             else { Join-Path $env:LOCALAPPDATA 'Android\Sdk' }
}
if (Test-Path $SdkRoot) {
  Check 'Android SDK' 'PASS' $SdkRoot
} else {
  Check 'Android SDK' 'FAIL' "not found at $SdkRoot (install Android Studio or cmdline-tools)"
}
$emulator = Join-Path $SdkRoot 'emulator\emulator.exe'
$adb = Join-Path $SdkRoot 'platform-tools\adb.exe'
$avdmanager = Join-Path $SdkRoot 'cmdline-tools\latest\bin\avdmanager.bat'
Check 'emulator.exe' $(if (Test-Path $emulator) { 'PASS' } else { 'FAIL' }) $emulator
Check 'adb.exe' $(if (Test-Path $adb) { 'PASS' } else { 'FAIL' }) $adb
Check 'avdmanager (cmdline-tools)' $(if (Test-Path $avdmanager) { 'PASS' } else { 'WARN' }) $(if (Test-Path $avdmanager) { 'installed' } else { 'missing - run scripts\setup_devices.ps1' })

# --- java / bundletool ---------------------------------------------------------
$java = $null
foreach ($base in (Get-ChildItem (Join-Path $root 'tools') -Directory -Filter 'jdk*' -ErrorAction SilentlyContinue)) {
  $j = Get-ChildItem $base.FullName -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
       Where-Object { $_.DirectoryName -like '*\bin' } | Select-Object -First 1
  if ($j) { $java = $j.FullName; break }
}
if (-not $java) { $jc = Get-Command java -ErrorAction SilentlyContinue; if ($jc) { $java = $jc.Source } }
foreach ($cand in @('C:\Program Files\Android\Android Studio\jbr\bin\java.exe', (Join-Path $env:LOCALAPPDATA 'Programs\Android Studio\jbr\bin\java.exe'))) {
  if (-not $java -and (Test-Path $cand)) { $java = $cand }
}
Check 'Java (for bundletool/aab)' $(if ($java) { 'PASS' } else { 'WARN' }) $(if ($java) { $java } else { 'missing - AAB mode will not work; run setup_devices.ps1' })
$jar = Join-Path $root 'tools\bundletool.jar'
Check 'bundletool.jar' $(if (Test-Path $jar) { 'PASS' } else { 'WARN' }) $(if (Test-Path $jar) { 'installed' } else { 'missing - AAB mode will not work; run setup_devices.ps1' })

# --- acceleration --------------------------------------------------------------
if (Test-Path $emulator) {
  $accel = & $emulator -accel-check 2>&1 | Out-String
  $ok = $accel -match 'is installed and usable|WHPX.*usable|HAXM.*usable'
  Check 'hardware acceleration' $(if ($ok) { 'PASS' } else { 'WARN' }) ($accel.Trim() -replace '\s+', ' ')
} else {
  Check 'hardware acceleration' 'WARN' 'skipped (no emulator.exe)'
}

# --- resources -----------------------------------------------------------------
$os = Get-CimInstance Win32_OperatingSystem
# Win32_OperatingSystem reports these two values in KB
$ramFree = [math]::Round($os.FreePhysicalMemory / 1KB, 0)
$ramTotal = [math]::Round($os.TotalVisibleMemorySize / 1KB, 0)
$needRam = 4000
$agentCfgPath = Join-Path $root 'host-agent\agent-config.json'
if (Test-Path $agentCfgPath) {
  try {
    $ac = Get-Content $agentCfgPath -Raw | ConvertFrom-Json
    $guest = [int]$ac.emulator.memory_mb
    $head = [int]$ac.limits.ram_headroom_mb
    $floor = [int]$ac.limits.min_free_ram_mb
    if ($guest -and $head) { $needRam = [math]::Max($floor, $guest + $head) }
    $devicesPath = Join-Path $root 'config\devices.json'
    if (Test-Path $devicesPath) {
      $dv = Get-Content $devicesPath -Raw | ConvertFrom-Json
      $maxGuest = [int](($dv.devices | Measure-Object -Property memory_mb -Maximum).Maximum)
      if ($maxGuest -ge 4096) {
        $head4 = if ($ac.limits.ram_headroom_4gb_mb) { [int]$ac.limits.ram_headroom_4gb_mb } else { 512 }
        $needRam = [math]::Max($needRam, $maxGuest + $head4)
      }
    }
  } catch { }
}
Check 'RAM' $(if ($ramFree -ge $needRam) { 'PASS' } else { 'WARN' }) "$ramTotal MB total, $ramFree MB free (portal wants $needRam+ MB free; 4 GB API 36.1 profiles need ~4.6 GB)"
try {
  $sdkDrive = (Split-Path -Qualifier $SdkRoot) -replace ':', ''
  $disk = Get-PSDrive -Name $sdkDrive -ErrorAction Stop
  $diskFree = [math]::Round($disk.Free / 1GB, 1)
  Check 'disk free (SDK drive)' $(if ($diskFree -ge 20) { 'PASS' } else { 'WARN' }) "$diskFree GB free"
} catch {
  Check 'disk free (SDK drive)' 'WARN' 'could not determine'
}

# --- docker --------------------------------------------------------------------
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
  $engine = docker version --format '{{.Server.Version}}' 2>$null
  if ($LASTEXITCODE -eq 0) {
    Check 'docker engine' 'PASS' "v$engine"
  } else {
    Check 'docker engine' 'WARN' 'Docker Desktop is not running (start it for the portal container)'
  }
} else {
  Check 'docker' 'WARN' 'not installed (fine if you use scripts\run_host.ps1 instead)'
}

# --- agent ---------------------------------------------------------------------
$envFile = Join-Path $root '.env'
$token = $null
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^AGENT_TOKEN=(.+)$' | Select-Object -First 1
  if ($m) { $token = $m.Matches[0].Groups[1].Value.Trim() }
}
if ($token) {
  try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -Headers @{ 'X-Agent-Token' = $token } -TimeoutSec 4
    Check 'host agent' 'PASS' "online - $(($h.avds | Measure-Object).Count) AVD(s), $($h.free_ram_mb) MB RAM free"
  } catch {
    Check 'host agent' 'WARN' 'not reachable - run host-agent\install-agent.ps1 (or run-agent.bat)'
  }
} else {
  Check 'host agent' 'WARN' '.env not found - run host-agent\install-agent.ps1 first'
}

# --- AVDs ----------------------------------------------------------------------
if (Test-Path $emulator) {
  $avds = @(& $emulator -list-avds 2>$null)
  $devicesFile = Join-Path $root 'config\devices.json'
  if (Test-Path $devicesFile) {
    $expectedAvds = @((Get-Content $devicesFile -Raw | ConvertFrom-Json).devices | Select-Object -ExpandProperty avd)
    $missing = @($expectedAvds | Where-Object { $avds -notcontains $_ })
    $detail = "$($avds.Count) total, $($expectedAvds.Count - $missing.Count)/$($expectedAvds.Count) portal AVD(s) present"
    if ($missing.Count -gt 0) { $detail += " - missing: $($missing -join ', ') (run scripts\setup_devices.ps1)" }
    Check 'AVDs' $(if ($missing.Count -eq 0) { 'PASS' } else { 'WARN' }) $detail
  } else {
    Check 'AVDs' 'WARN' "$($avds.Count) total, config\devices.json not found"
  }
}

# --- port 5554 -----------------------------------------------------------------
$busy = netstat -ano -p TCP | Select-String ':5554\s' | Select-Object -First 1
Check 'port 5554' $(if ($busy) { 'WARN' } else { 'PASS' }) $(if ($busy) { 'in use - an emulator may already be running' } else { 'free' })

Write-Host ''
$fails = @($results | Where-Object { $_.State -eq 'FAIL' }).Count
$warns = @($results | Where-Object { $_.State -eq 'WARN' }).Count
if ($fails -gt 0) {
  Write-Host "Summary: $fails FAIL, $warns WARN - fix the FAILs first." -ForegroundColor Red
  exit 1
} else {
  Write-Host "Summary: all good ($warns warning(s))." -ForegroundColor Green
  exit 0
}
