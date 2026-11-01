# Contributing to Android Device Portal

Android Device Portal is a [BlueFluteX](https://blueflutex.in/) project.
Thanks for taking the time to contribute. This project is a Windows-first,
single-machine test harness, so changes should keep that deployment model in
mind: one emulator at a time, no cloud dependency, and no secrets in the repo.

## Before you start

- Read the [README](README.md) and the [security policy](SECURITY.md).
- Search existing issues before opening a new one.
- For a larger change, open an issue first so the approach can be agreed on.

## Development setup

```powershell
# 1. Portal dependencies in a local virtualenv
py -3 -m venv portal\.venv
.\portal\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 2. Static checks and tests
.\portal\.venv\Scripts\python.exe -m ruff check .
.\portal\.venv\Scripts\python.exe -m pytest

# 3. Run the portal without Docker
scripts\run_host.ps1
```

The host agent is standard-library only. Run it with
`host-agent\run-agent.bat`; it needs an `agent-config.json`, which
`host-agent\install-agent.ps1` generates locally.

## Code style

- Python 3.10+ syntax, 4-space indentation, type hints where they add clarity.
- Keep the agent dependency-free (standard library only).
- Use `ruff` for linting; `pyproject.toml` holds the configuration.
- Keep PowerShell scripts compatible with Windows PowerShell 5.1.
- Prefer small, focused functions over broad refactors.

## Tests

- Add or update tests under `tests/` for behavior changes.
- Run `pytest` before opening a pull request.
- For emulator changes, include the device profile, install mode, and a short
  description of the manual smoke test in the pull request.

## Commit messages

Use short imperative subjects, for example:

```text
Fix AAB install path translation on Windows
Add per-device screenshot offsets
Document Play internal-testing flow
```

## Pull requests

- Keep each pull request focused on one change.
- Fill in the pull request template, including how the change was tested.
- Update `README.md` and `CHANGELOG.md` when behavior or setup changes.
- Never commit `.env`, `host-agent/agent-config.json`, `data/`, or `tools/`.
  They are ignored by default and contain machine-specific data.
- CI must pass before a pull request is merged.

## Releases

Releases follow [Semantic Versioning](https://semver.org/). Maintainers:

1. Update `CHANGELOG.md` and `pyproject.toml` with the new version.
2. Run `ruff check .` and `pytest`.
3. Tag the commit (`git tag -a vX.Y.Z -m "vX.Y.Z"`) and push the tag.
4. Publish the GitHub release with notes from `CHANGELOG.md`.
