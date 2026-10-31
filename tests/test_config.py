"""Tests for configuration loading and path resolution."""
from __future__ import annotations

import json

from portal.app import config


def test_load_devices_reads_devices_key(tmp_path, monkeypatch):
    payload = {"devices": [{"id": "pixel4a", "name": "Pixel 4a"}]}
    (tmp_path / "devices.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    devices = config.load_devices()

    assert devices == [{"id": "pixel4a", "name": "Pixel 4a"}]


def test_resolve_run_dir_maps_container_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    run_dir = tmp_path / "runs" / "20260101-000000_pixel4a"
    run_dir.mkdir(parents=True)

    resolved = config.resolve_run_dir("/data/runs/20260101-000000_pixel4a")

    assert resolved == run_dir


def test_resolve_run_dir_keeps_existing_absolute_path(tmp_path):
    run_dir = tmp_path / "artifacts"
    run_dir.mkdir()

    assert config.resolve_run_dir(str(run_dir)) == run_dir


def test_resolve_run_dir_handles_empty_value():
    assert config.resolve_run_dir(None) == config.Path()
