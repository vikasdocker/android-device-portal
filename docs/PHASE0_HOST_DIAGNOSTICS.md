# Phase 0 — Windows host diagnostics

Run these commands from the repository root in PowerShell. The scripts are read-only: they do not enable Windows features, change BIOS settings, create AVDs, or stop processes.

## 1. Collect host inventory

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\windows\diagnose-host.ps1
```

The script writes timestamped JSON and text reports under `diagnostics/`. Review:

- CPU virtualization fields and `AndroidSDK.AccelerationCheck`
- Installed and currently free RAM
- GPU names and driver versions
- Free space on each fixed disk
- Android SDK path, installed AVD names and connected ADB devices

On Windows, Android's current guidance recommends Windows Hypervisor Platform (WHPX). If acceleration is unavailable, check that Intel virtualization is enabled in BIOS/UEFI and that the Windows Hypervisor Platform optional feature is installed, then reboot. Do not change these settings automatically from the diagnostic script.

## 2. Measure idle baseline

Close Android Studio and unnecessary applications, but leave the portal and its normal background services running. Then:

```powershell
.\scripts\windows\measure-avd-load.ps1 -Label idle -DurationMinutes 10
```

## 3. Measure emulator concurrency

Use the same AVD profile and workload for each run. Start AVDs manually from Android Studio Device Manager or the existing local workflow. Wait until Android finishes booting, then keep the same app and interaction workload active for the full sample:

```powershell
.\scripts\windows\measure-avd-load.ps1 -Label one-avd -DurationMinutes 10
.\scripts\windows\measure-avd-load.ps1 -Label two-avds -DurationMinutes 10
.\scripts\windows\measure-avd-load.ps1 -Label three-avds -DurationMinutes 10
```

Run each command only when the matching number of AVDs is already running. The sampler does not launch or stop emulators. Save the generated CSV reports and note boot time, freezes, crashes and visible stream quality.

## 4. Measure public network quality

From the Windows host, open [Cloudflare Speed Test](https://speed.cloudflare.com/) and record upload Mbps, download Mbps, unloaded/loaded latency and packet loss if reported. Repeat at a busy time of day. Also confirm the ISP plan's upload cap and whether the PC can remain powered on. A speed-test result is a planning signal, not proof that WebRTC streaming will work through every client network.

## 5. Initial review gates

These are engineering guardrails for this 16 GB host, not Android vendor minimum requirements:

- Do not enable public sessions if WHPX acceleration is unavailable.
- Do not approve three concurrent sessions if the three-AVD run leaves less than 3 GB free RAM at any sustained point, causes paging-related stalls, or shows emulator crashes/freezes.
- Keep CPU load and graphics smooth under the actual browser-streaming workload; investigate sustained near-saturation rather than relying on a short peak.
- Repeat tests after a clean reboot and with the portal, reverse proxy and tunnel services running.
- Start the public beta at one session until the measured two- and three-session results are reviewed.

With 16 GB installed, three sessions may be possible only with carefully tuned AVDs and a light host workload. A 32 GB upgrade is the sensible next hardware step if three simultaneous public sessions are a hard product requirement.

## Reports to share for review

Share the generated `host-diagnostic-*.json` plus the four `load-*.csv` files (idle, one, two and three AVDs), and the Cloudflare Speed Test upload/latency results. Redact Windows username paths or other personal details if desired.
