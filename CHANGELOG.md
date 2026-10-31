# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/vikasdocker/android-device-portal/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/vikasdocker/android-device-portal/releases/tag/v1.0.0
