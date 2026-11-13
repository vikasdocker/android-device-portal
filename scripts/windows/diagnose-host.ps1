[CmdletBinding()]
param([string]$OutputDirectory = (Join-Path $PSScriptRoot "..\..\diagnostics"))
$ErrorActionPreference = "Continue"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
function Get-SafeValue([scriptblock]$Script) { try { & $Script } catch { $null } }
$os = Get-CimInstance Win32_OperatingSystem
$computer = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [pscustomobject]@{ Name=$_.Name; DriverVersion=$_.DriverVersion; AdapterRAM_GB=$(if ($_.AdapterRAM) {[math]::Round($_.AdapterRAM / 1GB,2)} else {$null}); VideoMode=$_.VideoModeDescription }
})
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    [pscustomobject]@{ Drive=$_.DeviceID; Size_GB=[math]::Round($_.Size/1GB,1); Free_GB=[math]::Round($_.FreeSpace/1GB,1) }
})
$features = @{}
if (Get-Command Get-WindowsOptionalFeature -ErrorAction SilentlyContinue) {
    foreach ($feature in @("HypervisorPlatform","VirtualMachinePlatform","Microsoft-Hyper-V-All")) {
        $features[$feature] = Get-SafeValue { (Get-WindowsOptionalFeature -Online -FeatureName $feature).State.ToString() }
    }
}
$sdkCandidates = @($env:ANDROID_HOME,$env:ANDROID_SDK_ROOT,(Join-Path $env:LOCALAPPDATA "Android\Sdk")) | Where-Object { $_ } | Select-Object -Unique
$sdk = $sdkCandidates | Where-Object { Test-Path (Join-Path $_ "emulator\emulator.exe") } | Select-Object -First 1
$emulatorPath = if ($sdk) { Join-Path $sdk "emulator\emulator.exe" } else { $null }
$adbPath = if ($sdk) { Join-Path $sdk "platform-tools\adb.exe" } else { $null }
$accel = $null; $avds = @(); $adbDevices = @()
if ($emulatorPath) {
    $accel = (& $emulatorPath -accel-check 2>&1 | Out-String).Trim()
    $avds = @(& $emulatorPath -list-avds 2>&1 | Where-Object { $_ -and $_ -notmatch "^INFO" })
}
if ($adbPath -and (Test-Path $adbPath)) { $adbDevices = @(& $adbPath devices -l 2>&1) }
$result = [pscustomobject]@{
    GeneratedAt=(Get-Date).ToString("o"); Hostname=$env:COMPUTERNAME
    OS=[pscustomobject]@{ Caption=$os.Caption; Version=$os.Version; BuildNumber=$os.BuildNumber; Architecture=$os.OSArchitecture }
    CPU=[pscustomobject]@{ Name=$cpu.Name; Cores=$cpu.NumberOfCores; LogicalProcessors=$cpu.NumberOfLogicalProcessors; MaxClockSpeed_MHz=$cpu.MaxClockSpeed; VirtualizationFirmwareEnabled=$cpu.VirtualizationFirmwareEnabled; VMMonitorModeExtensions=$cpu.VMMonitorModeExtensions; SecondLevelAddressTranslationExtensions=$cpu.SecondLevelAddressTranslationExtensions }
    Memory=[pscustomobject]@{ Installed_GB=[math]::Round($computer.TotalPhysicalMemory/1GB,1); FreeNow_GB=[math]::Round(($os.FreePhysicalMemory*1KB)/1GB,1) }
    HypervisorPresent=$computer.HypervisorPresent; WindowsFeatures=$features; GPUs=$gpus; FixedDisks=$disks
    AndroidSDK=[pscustomobject]@{ SDKPath=$sdk; EmulatorPath=$emulatorPath; ADBPath=$adbPath; AccelerationCheck=$accel; AVDs=$avds; ConnectedDevices=$adbDevices }
    Notes=@("Read-only diagnostic; does not enable features, change BIOS settings, or start/stop emulators.","Prefer WHPX on Windows and verify emulator -accel-check reports usable acceleration.","Run the load sampler separately with one, two, then three AVDs.")
}
$jsonPath=Join-Path $OutputDirectory "host-diagnostic-$timestamp.json"
$txtPath=Join-Path $OutputDirectory "host-diagnostic-$timestamp.txt"
$result | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $jsonPath
$result | Format-List | Out-String -Width 240 | Set-Content -Encoding UTF8 $txtPath
$result | Format-List
Write-Host "`nSaved:`n$jsonPath`n$txtPath" -ForegroundColor Green
