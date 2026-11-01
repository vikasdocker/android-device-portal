# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.2] - 2026-11-01

### Changed

- Updated the BlueFluteX website to <https://blueflutex.in/> and the public
  contact address to <support@blueflutex.in> across the README, package
  metadata, container labels, portal footer, security policy, code of conduct,
  and contribution guide.

## [1.0.1] - 2026-10-31

### Changed

- Updated the copyright holder and project attribution to **BlueFluteX**, the
  software studio behind the project.
- Added BlueFluteX company links and contact details to the README, package
  metadata, container labels, portal footer, and security policy.
- Added a project-level `AGENTS.md` with build, test, and branding guidance.

## [1.0.0] - 2026-10-31

### Added

- Sequential 16-device Android test matrix (Pixel 4a through Pixel 10 Pro XL,
  Pixel Tablet, Pixel Fold, and a Galaxy S24 Ultra screen profile).
- FastAPI portal with a browser UI, live agent status, run history, and
  shareable HTML reports.
- Windows host agent (stdlib only) that drives one emulator at a time through
  `emulator.exe` and `adb.exe`.
- APK, AAB (`bundletool`), Play internal-testing link, and already-installed
  launch modes.
- Per-run screenshots, logcat capture, crash/ANR scanning, `meminfo.txt`, and a
  machine-readable `result.json`.
- RAM/disk preflight checks, cold-boot retries, stale-lock cleanup, optional
  quick boot, data wipe, and stuck-emulator recovery.
- Docker Compose deployment, no-Docker local fallback, environment doctor, and
  one-time AVD bootstrap scripts.
- Agent token authentication with portal-side proxying of screenshots.
- MIT license, contribution guide, security policy, and CI checks.

[Unreleased]: https://github.com/vikasdocker/android-device-portal/compare/v1.0.2...HEAD
[1.0.2]: https://github.com/vikasdocker/android-device-portal/releases/tag/v1.0.2
[1.0.1]: https://github.com/vikasdocker/android-device-portal/releases/tag/v1.0.1
[1.0.0]: https://github.com/vikasdocker/android-device-portal/releases/tag/v1.0.0
