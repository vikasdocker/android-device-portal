# Android Device Portal - create the AVD test matrix from config\devices.json
#  * installs Android cmdline-tools + a JDK into .\tools (if missing)
#  * creates/updates AVDs for every device whose system image is already installed
#  * downloads the missing system images, then creates the rest
#  * downloads bundletool (needed for AAB installs)
# Safe to re-run any time: it skips downloads and refreshes AVD hardware settings.
[CmdletBinding()]
param(
  [string]$SdkRoot = '',
  [string]$DevicesFile = '',
  [switch]$SkipDownloads
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$toolsDir = Join-Path $root 'tools'
$downloadsDir = Join-Path $toolsDir 'downloads'
New-Item -ItemType Directory -Force -Path $downloadsDir | Out-Null

if (-not $DevicesFile) { $DevicesFile = Join-Path $root 'config\devices.json' }
if (-not (Test-Path $DevicesFile)) { throw "devices file not found: $DevicesFile" }
$devices = (Get-Content $DevicesFile -Raw | ConvertFrom-Json).devices

if (-not $SdkRoot) {
  $SdkRoot = if ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT }
             elseif ($env:ANDROID_HOME) { $env:ANDROID_HOME }
             else { Join-Path $env:LOCALAPPDATA 'Android\Sdk' }
}
if (-not (Test-Path $SdkRoot)) { throw "Android SDK not found at $SdkRoot" }

function Write-Section([string]$text) {
  Write-Host ''
  Write-Host "=== $text ===" -ForegroundColor Cyan
}

# PowerShell 5.1 turns native-tool stderr output into terminating errors while
# $ErrorActionPreference='Stop'. Run native tools inside this wrapper instead.
function Invoke-Quiet([scriptblock]$block) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $block } finally { $ErrorActionPreference = $prev }
}

function Get-File([string]$Url, [string]$OutFile, [switch]$Resume) {
  Remove-Item "$OutFile.part" -Force -ErrorAction SilentlyContinue
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ($curl) {
    $args = @('-L', '--fail', '--retry', '10', '--retry-delay', '5', '--connect-timeout', '30')
    if ($Resume -and (Test-Path $OutFile)) { $args += @('-C', '-') }
    $args += @('-o', $OutFile, $Url)
    & $curl.Source @args
    if ($LASTEXITCODE -ne 0) { throw "download failed (exit $LASTEXITCODE): $Url" }
  } else {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile
  }
}

# --------------------------------------------------------------------------- #
# 1) Java (needed by sdkmanager / avdmanager / bundletool)
# --------------------------------------------------------------------------- #
Write-Section 'Java'
$javaHome = $null
foreach ($base in (Get-ChildItem $toolsDir -Directory -Filter 'jdk*' -ErrorAction SilentlyContinue)) {
  $j = Get-ChildItem $base.FullName -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
       Where-Object { $_.DirectoryName -like '*\bin' } | Select-Object -First 1
  if ($j) { $javaHome = Split-Path $j.DirectoryName -Parent; break }
}
if ($javaHome) {
  Write-Host "[ok] using bundled Java: $javaHome"
} else {
  foreach ($cand in @('C:\Program Files\Android\Android Studio\jbr', (Join-Path $env:LOCALAPPDATA 'Programs\Android Studio\jbr'))) {
    if (Test-Path (Join-Path $cand 'bin\java.exe')) { $javaHome = $cand; break }
  }
}
if (-not $javaHome) {
  $jc = Get-Command java -ErrorAction SilentlyContinue
  if ($jc) { $javaHome = Split-Path (Split-Path $jc.Source -Parent) -Parent }
}
if (-not $javaHome) {
  Write-Host '[..] downloading JDK 17 (about 180 MB, resumable)'
  $jdkZip = Join-Path $downloadsDir 'jdk17.zip'
  $jdkSources = @(
    'https://aka.ms/download-jdk/microsoft-jdk-17-windows-x64.zip',
    'https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse'
  )
  $downloaded = $false
  foreach ($src in $jdkSources) {
    try { Get-File -Url $src -OutFile $jdkZip -Resume; $downloaded = $true; break }
    catch {
      Write-Warning "JDK download failed from $src"
      Remove-Item $jdkZip -Force -ErrorAction SilentlyContinue   # never resume across different sources
    }
  }
  if (-not $downloaded) { throw 'all JDK download sources failed' }
  Write-Host '[..] extracting JDK'
  Expand-Archive -Path $jdkZip -DestinationPath (Join-Path $toolsDir 'jdk') -Force
  $j = Get-ChildItem (Join-Path $toolsDir 'jdk') -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
       Where-Object { $_.DirectoryName -like '*\bin' } | Select-Object -First 1
  if (-not $j) { throw 'JDK extraction failed - no java.exe found' }
  $javaHome = Split-Path $j.DirectoryName -Parent
  Write-Host "[ok] JDK installed: $javaHome"
}
$env:JAVA_HOME = $javaHome
$env:PATH = "$javaHome\bin;$env:PATH"

# --------------------------------------------------------------------------- #
# 2) Android cmdline-tools (sdkmanager / avdmanager)
# --------------------------------------------------------------------------- #
Write-Section 'Android cmdline-tools'
$ctBin = Join-Path $SdkRoot 'cmdline-tools\latest\bin'
if (Test-Path (Join-Path $ctBin 'sdkmanager.bat')) {
  Write-Host '[ok] cmdline-tools already installed'
} else {
  $zip = Join-Path $downloadsDir 'cmdline-tools.zip'
  if (-not (Test-Path $zip)) {
    if ($SkipDownloads) { throw 'cmdline-tools missing and -SkipDownloads was set' }
    Write-Host '[..] locating the latest cmdline-tools download'
    [xml]$repo = (Invoke-WebRequest -UseBasicParsing 'https://dl.google.com/android/repository/repository2-3.xml').Content
    $best = $null
    foreach ($pkg in $repo.SelectNodes("//*[local-name()='remotePackage']")) {
      if ($pkg.GetAttribute('path') -like 'cmdline-tools;*') {
        foreach ($arch in $pkg.SelectNodes(".//*[local-name()='archive']")) {
          $hos = $arch.SelectSingleNode(".//*[local-name()='host-os']")
          if ($hos -and $hos.InnerText -eq 'windows') {
            $urlNode = $arch.SelectSingleNode(".//*[local-name()='complete']//*[local-name()='url']")
            if ($urlNode) {
              $u = $urlNode.InnerText
              if ($u -match 'commandlinetools-win') {
                if ($u -notmatch '^https?://') { $u = 'https://dl.google.com/android/repository/' + $u }
                $build = 0
                if ($u -match 'win-(\d+)_') { $build = [int]$matches[1] }
                if ($null -eq $best -or $build -gt $best.Build) {
                  $best = [pscustomobject]@{ Url = $u; Build = $build }
                }
              }
            }
          }
        }
      }
    }
    if ($null -eq $best) { throw 'could not find a cmdline-tools download for Windows' }
    Write-Host "[..] downloading $($best.Url)"
    Get-File -Url $best.Url -OutFile $zip
  }
  Write-Host '[..] extracting cmdline-tools'
  $tmp = Join-Path $env:TEMP ('adp-ct-' + [guid]::NewGuid().ToString('N'))
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  New-Item -ItemType Directory -Force -Path (Join-Path $SdkRoot 'cmdline-tools') | Out-Null
  $dest = Join-Path $SdkRoot 'cmdline-tools\latest'
  if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
  Move-Item -Path (Join-Path $tmp 'cmdline-tools') -Destination $dest
  Remove-Item $tmp -Recurse -Force
  Write-Host '[ok] cmdline-tools installed'
}
$sdkmanager = Join-Path $ctBin 'sdkmanager.bat'
$avdmanager = Join-Path $ctBin 'avdmanager.bat'

# --------------------------------------------------------------------------- #
# 3) helpers for AVD work
# --------------------------------------------------------------------------- #
$emulatorExe = Join-Path $SdkRoot 'emulator\emulator.exe'
if (-not (Test-Path $emulatorExe)) { throw "emulator.exe not found: $emulatorExe" }
$avdHome = if ($env:ANDROID_AVD_HOME) { $env:ANDROID_AVD_HOME } else { Join-Path $env:USERPROFILE '.android\avd' }

function Test-ImageInstalled([string]$image) {
  $parts = $image -split ';'
  $imgPath = Join-Path $SdkRoot ("system-images\{0}\{1}\{2}" -f $parts[1], $parts[2], $parts[3])
  return (Test-Path $imgPath) -and ((Get-ChildItem $imgPath -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)
}

function Set-IniValues([string]$path, [hashtable]$pairs) {
  $lines = @(Get-Content $path)
  foreach ($k in $pairs.Keys) {
    $esc = [regex]::Escape($k)
    $found = $false
    $next = @()
    foreach ($line in $lines) {
      # AVD files produced by Android Studio use "key = value"; accept both
      # styles and collapse duplicates left by older script versions.
      if ($line -match "^\s*$esc\s*=") {
        if (-not $found) {
          $next += "$k=$($pairs[$k])"
          $found = $true
        }
      } else {
        $next += $line
      }
    }
    if (-not $found) { $next += "$k=$($pairs[$k])" }
    $lines = $next
  }
  Set-Content -Path $path -Value $lines -Encoding ASCII
}

function Get-AvdHardware([object]$device) {
  $mem = if ($device.memory_mb) { [int]$device.memory_mb } else { 3072 }
  $cores = if ($device.cores) { [int]$device.cores } else { 4 }
  $pairs = @{
    'hw.ramSize'              = "$mem"
    'hw.cpu.ncore'            = "$cores"
    'hw.keyboard'             = 'yes'
    'disk.dataPartition.size' = '3G'
    'showDeviceFrame'         = 'yes'
    'hw.gpu.mode'             = 'auto'
  }
  if ($device.play_store) { $pairs['PlayStore.enabled'] = 'yes' }
  if ($device.ini) {
    foreach ($prop in $device.ini.PSObject.Properties) {
      $pairs[$prop.Name] = [string]$prop.Value
    }
  }
  return $pairs
}

function New-PortalAvd($device, $availableProfiles) {
  $cfg = Join-Path $avdHome "$($device.avd).avd\config.ini"
  $hardware = Get-AvdHardware $device
  if (Test-Path $cfg) {
    # Keep existing AVD/data, but refresh memory/CPU and any custom screen
    # overrides (e.g. the Samsung Ultra 1440x3120 profile).
    Set-IniValues $cfg $hardware
    return 'exists'
  }
  $profiles = @($device.profile, 'pixel_6', 'medium_phone')
  foreach ($prof in $profiles) {
    if ($availableProfiles.Count -gt 0 -and ($availableProfiles -notcontains $prof)) {
      Write-Host "[warn] profile '$prof' is not in this SDK, trying the next fallback"
      continue
    }
    Write-Host "[new ] $($device.avd)  (profile: $prof, image: $($device.image))"
    Invoke-Quiet { 'no' | & $avdmanager create avd -n "$($device.avd)" -k "$($device.image)" -d "$prof" --force 2>&1 | Out-Null }
    if (Test-Path $cfg) {
      Set-IniValues $cfg $hardware
      return 'created'
    }
  }
  return 'failed'
}

# --------------------------------------------------------------------------- #
# 4) licenses + image status
# --------------------------------------------------------------------------- #
Write-Section 'SDK licenses'
$yesAll = @(1..60 | ForEach-Object { 'y' })
Invoke-Quiet { $yesAll | & $sdkmanager --sdk_root="$SdkRoot" --licenses 2>&1 | Out-Null }
Write-Host '[ok] licenses accepted'

$profileIds = @()
$devListRaw = Invoke-Quiet { & $avdmanager list device 2>&1 | Out-String }
foreach ($m in [regex]::Matches($devListRaw, 'id:\s*\d+\s+or\s+"([^"]+)"')) { $profileIds += $m.Groups[1].Value }
Write-Host "[info] $($profileIds.Count) device profiles available in this SDK"

# pass 1 - devices whose image is already installed get an AVD right away
Write-Section 'AVDs (installed images first)'
$created = 0; $skipped = 0; $failed = @()
foreach ($device in $devices) {
  if (-not (Test-ImageInstalled $device.image)) { continue }
  switch (New-PortalAvd $device $profileIds) {
    'created' { $created++ }
    'exists'  { $skipped++ }
    'failed'  { $failed += $device.avd }
  }
}
Write-Host "[info] pass 1: created $created, already existed $skipped"

# pass 2 - download missing images, then create the remaining AVDs
$missingImages = @($devices | Where-Object { -not (Test-ImageInstalled $_.image) } | Select-Object -ExpandProperty image -Unique)
if ($missingImages.Count -gt 0) {
  Write-Section "Downloading $($missingImages.Count) missing system image(s)"
  Write-Host '[info] this is the big download (roughly 1.5-2 GB per image). It is resumable:'
  Write-Host '       if it fails, just run this script again and it continues where it left off.'
  # Newer cmdline-tools ship `android.exe`; the legacy `sdkmanager.bat` is a shim that
  # mangles ';'-separated package paths into separate args, so prefer the new CLI
  # (which expects '/' paths) and keep the legacy call only as a fallback.
  $androidCli = Join-Path $ctBin 'android.exe'
  foreach ($img in $missingImages) {
    if ($SkipDownloads) { Write-Host "[skip] $img (-SkipDownloads)"; continue }
    Write-Host "[dl  ] $img"
    if (Test-Path $androidCli) {
      $pkgPath = $img -replace ';', '/'
      Invoke-Quiet { & $androidCli --sdk="$SdkRoot" sdk install "$pkgPath" }
      if ($LASTEXITCODE -ne 0) { Write-Warning "android sdk install returned $LASTEXITCODE for $img (re-run to retry)" }
    } else {
      Invoke-Quiet { & $sdkmanager --sdk_root="$SdkRoot" --install "$img" }
      if ($LASTEXITCODE -ne 0) { Write-Warning "sdkmanager returned $LASTEXITCODE for $img (re-run to retry)" }
    }
  }
  Write-Section 'AVDs (after downloads)'
  foreach ($device in $devices) {
    if (-not (Test-ImageInstalled $device.image)) { continue }
    switch (New-PortalAvd $device $profileIds) {
      'created' { $created++ }
      'exists'  { $skipped++ }
      'failed'  { $failed += $device.avd }
    }
  }
} else {
  Write-Host '[ok] all system images are installed'
}

# --------------------------------------------------------------------------- #
# 5) bundletool (AAB installs)
# --------------------------------------------------------------------------- #
Write-Section 'bundletool'
$jar = Join-Path $toolsDir 'bundletool.jar'
if (Test-Path $jar) {
  Write-Host '[ok] bundletool.jar already present'
} elseif ($SkipDownloads) {
  Write-Warning 'bundletool.jar missing (-SkipDownloads) - AAB mode will not work'
} else {
  Write-Host '[..] downloading bundletool (about 31 MB, resumable)'
  Get-File -Url 'https://github.com/google/bundletool/releases/download/1.18.3/bundletool-all-1.18.3.jar' -OutFile $jar -Resume
  Write-Host '[ok] bundletool.jar downloaded'
}

# --------------------------------------------------------------------------- #
# 6) summary
# --------------------------------------------------------------------------- #
Write-Section 'Summary'
$allAvds = Invoke-Quiet { & $emulatorExe -list-avds 2>$null }
$expectedAvds = @($devices | Select-Object -ExpandProperty avd)
$portalAvds = @($expectedAvds | Where-Object { $allAvds -contains $_ })
Write-Host "created: $created   already existed: $skipped   failed: $($failed.Count)"
if ($failed.Count -gt 0) { Write-Warning ("failed AVDs: " + ($failed -join ', ') + " (re-run this script)") }
Write-Host "portal AVDs now available ($($portalAvds.Count)/$($expectedAvds.Count)):"
$portalAvds | ForEach-Object { Write-Host "  - $_" }
Write-Host ''
Write-Host 'Next steps:' -ForegroundColor Cyan
Write-Host '  1) host-agent\install-agent.ps1        (registers + starts the host agent)'
Write-Host '  2) scripts\doctor.ps1                  (verify everything)'
Write-Host '  3) docker compose up -d                (portal -> http://localhost:8080)'
