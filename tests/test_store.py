"""Tests for settings and run-history persistence."""
from __future__ import annotations

from portal.app import store


def test_settings_defaults_and_roundtrip(isolated_store):
    defaults = isolated_store.get_settings()
    assert defaults["install_mode"] == "installed"
    assert defaults["auto_stop_emulator"] is True

    saved = isolated_store.save_settings(
        {"package_name": "com.example.app", "settle_seconds": 12, "unknown": "ignored"}
    )

    assert saved["package_name"] == "com.example.app"
    assert saved["settle_seconds"] == 12
    assert "unknown" not in saved
    assert isolated_store.get_settings()["package_name"] == "com.example.app"


def test_run_lifecycle_and_latest_per_device(isolated_store):
    isolated_store.upsert_run(
        "20260101-000000_pixel4a",
        "pixel4a",
        "PD_Pixel4a_API34",
        "running",
        started_at="2026-01-01T00:00:00",
        run_dir="/data/runs/first",
    )
    isolated_store.upsert_run(
        "20260102-000000_pixel4a",
        "pixel4a",
        "PD_Pixel4a_API34",
        "pass",
        started_at="2026-01-02T00:00:00",
        finished_at="2026-01-02T00:05:00",
        result={"status": "pass", "reasons": []},
        run_dir="/data/runs/second",
    )
    isolated_store.upsert_run(
        "20260102-010000_pixel8",
        "pixel8",
        "PD_Pixel8_API35",
        "fail",
        started_at="2026-01-02T01:00:00",
        result={"status": "fail", "reasons": ["app crashed"]},
    )

    runs = isolated_store.list_runs()
    assert [run["id"] for run in runs] == [
        "20260102-010000_pixel8",
        "20260102-000000_pixel4a",
        "20260101-000000_pixel4a",
    ]

    latest = isolated_store.latest_run_per_device()
    assert latest["pixel4a"]["status"] == "pass"
    assert latest["pixel8"]["status"] == "fail"

    detail = isolated_store.get_run("20260102-010000_pixel8")
    assert detail["result"]["reasons"] == ["app crashed"]
    assert isolated_store.get_run("missing") is None


def test_upsert_keeps_existing_result_when_update_omits_it(isolated_store):
    run_id = "20260101-000000_pixel4a"
    isolated_store.upsert_run(
        run_id,
        "pixel4a",
        "PD_Pixel4a_API34",
        "running",
        started_at="2026-01-01T00:00:00",
    )
    isolated_store.upsert_run(
        run_id,
        "pixel4a",
        "PD_Pixel4a_API34",
        "pass",
        finished_at="2026-01-01T00:05:00",
        result={"status": "pass"},
    )

    detail = isolated_store.get_run(run_id)
    assert detail["status"] == "pass"
    assert detail["started_at"] == "2026-01-01T00:00:00"
    assert detail["finished_at"] == "2026-01-01T00:05:00"
    assert detail["result"] == {"status": "pass"}


def test_runs_are_capped_by_limit(isolated_store):
    for index in range(5):
        isolated_store.upsert_run(
            f"run-{index}",
            f"device-{index}",
            f"avd-{index}",
            "pass",
            started_at=f"2026-01-0{index + 1}T00:00:00",
        )

    assert len(isolated_store.list_runs(limit=2)) == 2
    assert len(isolated_store.list_runs(limit=50)) == 5


def test_store_is_usable_from_helper_module():
    # Guard against a regression where the module-level connection is typed but
    # never initialised before the first settings read.
    assert store.DEFAULT_SETTINGS["install_mode"] == "installed"
