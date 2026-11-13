[CmdletBinding()]
param(
 [ValidateSet("idle","one-avd","two-avds","three-avds","custom")][string]$Label="custom",
 [ValidateRange(1,240)][int]$DurationMinutes=10,
 [ValidateRange(2,60)][int]$IntervalSeconds=5,
 [string]$OutputDirectory=(Join-Path $PSScriptRoot "..\..\diagnostics")
)
$ErrorActionPreference="Stop"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$timestamp=Get-Date -Format "yyyyMMdd-HHmmss"
$csvPath=Join-Path $OutputDirectory "load-$Label-$timestamp.csv"
$endAt=(Get-Date).AddMinutes($DurationMinutes)
$rows=[System.Collections.Generic.List[object]]::new()
Write-Host "Sampling '$Label' for $DurationMinutes minutes. Keep the selected AVD workload steady." -ForegroundColor Cyan
Write-Host "Do not run multiple test labels simultaneously. Press Ctrl+C to stop early."
while ((Get-Date) -lt $endAt) {
 $os=Get-CimInstance Win32_OperatingSystem
 $cpu=Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average
 $emulators=@(Get-Process -Name emulator* -ErrorAction SilentlyContinue)
 $qemu=@(Get-Process -Name qemu-system-* -ErrorAction SilentlyContinue)
 $allVm=@($emulators)+@($qemu)
 $vmWorkingSetMB=[math]::Round((($allVm | Measure-Object -Property WorkingSet64 -Sum).Sum/1MB),0)
 $vmCpuSeconds=[math]::Round((($allVm | Measure-Object -Property CPU -Sum).Sum),1)
 $rows.Add([pscustomobject]@{
  Timestamp=(Get-Date).ToString("o"); Label=$Label
  FreeRAM_GB=[math]::Round(($os.FreePhysicalMemory*1KB)/1GB,2)
  UsedRAM_Percent=[math]::Round((1-($os.FreePhysicalMemory/$os.TotalVisibleMemorySize))*100,1)
  HostCPU_AveragePercent=[math]::Round($cpu.Average,1)
  EmulatorProcessCount=$allVm.Count; EmulatorWorkingSet_MB=$vmWorkingSetMB
  EmulatorCumulativeCPUSeconds=$vmCpuSeconds
 })
 Start-Sleep -Seconds $IntervalSeconds
}
$rows | Export-Csv -NoTypeInformation -Encoding UTF8 -Path $csvPath
[pscustomobject]@{
 Label=$Label; Samples=$rows.Count
 MinFreeRAM_GB=[math]::Round(($rows|Measure-Object FreeRAM_GB -Minimum).Minimum,2)
 AvgFreeRAM_GB=[math]::Round(($rows|Measure-Object FreeRAM_GB -Average).Average,2)
 MinHostCPU_Percent=[math]::Round(($rows|Measure-Object HostCPU_AveragePercent -Minimum).Minimum,1)
 AvgHostCPU_Percent=[math]::Round(($rows|Measure-Object HostCPU_AveragePercent -Average).Average,1)
 MaxHostCPU_Percent=[math]::Round(($rows|Measure-Object HostCPU_AveragePercent -Maximum).Maximum,1)
 MaxEmulatorWorkingSet_MB=[math]::Round(($rows|Measure-Object EmulatorWorkingSet_MB -Maximum).Maximum,0)
 CSV=$csvPath
} | Format-List
