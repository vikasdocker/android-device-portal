# Android Device Portal — project instructions

## Company attribution (required)

This project is owned and maintained by **BlueFluteX**.

- Website: <https://blueflutex.gt.tc/>
- Studio: Pune, Maharashtra, India
- Contact: <vikasshu7@gmail.com>
- Tagline: *Code with Harmony*

Keep BlueFluteX attribution in `LICENSE`, `README.md`, `pyproject.toml`,
`portal/Dockerfile` OCI labels, the portal footer, and release notes. Do not
remove upstream or third-party notices.

## Architecture

- `portal/` — FastAPI app, orchestrator, SQLite store, and static web UI.
- `host-agent/` — Windows-only Python agent; **standard library only**.
- `config/devices.json` — the device matrix, read live by the portal.
- `scripts/` — PowerShell bootstrap, doctor, and no-Docker launcher.
- `tests/` — pytest suite for config and persistence.

The agent drives exactly one emulator at a time. Do not introduce parallel
emulator execution; the RAM budget and the design assume a single guest.

## Commands

```powershell
# Install development dependencies
.\portal\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# Lint, tests, syntax check
.\portal\.venv\Scripts\python.exe -m ruff check .
.\portal\.venv\Scripts\python.exe -m pytest
.\portal\.venv\Scripts\python.exe -m compileall -q portal host-agent

# Run without Docker
scripts\run_host.ps1
```

## Rules

- Never commit `.env`, `host-agent/agent-config.json`, `data/`, or `tools/`.
- The agent must remain dependency-free (Python standard library only).
- Keep PowerShell scripts compatible with Windows PowerShell 5.1.
- Update `CHANGELOG.md` and the version in `pyproject.toml` for user-visible
  changes.
- Release notes must credit **BlueFluteX** and link to
  <https://blueflutex.gt.tc/>.
