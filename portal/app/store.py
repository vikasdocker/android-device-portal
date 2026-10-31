"""SQLite persistence for settings and run history."""
import json
import sqlite3
import threading

from . import config

DEFAULT_SETTINGS = {
    "package_name": "",
    "install_mode": "installed",    # apk | aab | play | installed
    "apk_path": "",
    "aab_path": "",
    "play_url": "",
    "settle_seconds": 8,
    "screenshot_offsets": "2,8,20",
    "quick_boot": False,
    "wipe_before_run": False,
    "auto_stop_emulator": True,
}

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    config.ensure_dirs()
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                device_id TEXT,
                avd TEXT,
                status TEXT,
                started_at TEXT,
                finished_at TEXT,
                result_json TEXT,
                run_dir TEXT
            );
            """
        )
        _conn.commit()


def get_settings() -> dict:
    row = None
    with _lock:
        row = _conn.execute("SELECT json FROM settings WHERE id = 1").fetchone()
    merged = dict(DEFAULT_SETTINGS)
    if row:
        try:
            merged.update(json.loads(row["json"]))
        except ValueError:
            pass
    return merged


def save_settings(update: dict) -> dict:
    merged = get_settings()
    merged.update({k: v for k, v in (update or {}).items() if k in DEFAULT_SETTINGS})
    with _lock:
        _conn.execute(
            "INSERT INTO settings (id, json) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
            (json.dumps(merged),),
        )
        _conn.commit()
    return merged


def _row_to_dict(row: sqlite3.Row, with_result: bool = False) -> dict:
    out = {
        "id": row["id"],
        "device_id": row["device_id"],
        "avd": row["avd"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "run_dir": row["run_dir"],
    }
    if with_result and row["result_json"]:
        try:
            out["result"] = json.loads(row["result_json"])
        except ValueError:
            out["result"] = None
    return out


def upsert_run(
    run_id: str,
    device_id: str,
    avd: str,
    status: str,
    started_at: str | None = None,
    finished_at: str | None = None,
    result: dict | None = None,
    run_dir: str | None = None,
) -> None:
    with _lock:
        _conn.execute(
            """
            INSERT INTO runs (id, device_id, avd, status, started_at, finished_at, result_json, run_dir)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                finished_at = excluded.finished_at,
                result_json = COALESCE(excluded.result_json, runs.result_json),
                run_dir = COALESCE(excluded.run_dir, runs.run_dir),
                started_at = COALESCE(runs.started_at, excluded.started_at)
            """,
            (
                run_id,
                device_id,
                avd,
                status,
                started_at,
                finished_at,
                json.dumps(result) if result is not None else None,
                run_dir,
            ),
        )
        _conn.commit()


def list_runs(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, device_id, avd, status, started_at, finished_at, run_dir, NULL as result_json "
            "FROM runs ORDER BY COALESCE(started_at, id) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_run(run_id: str) -> dict | None:
    with _lock:
        row = _conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_dict(row, with_result=True) if row else None


def latest_run_per_device() -> dict[str, dict]:
    with _lock:
        rows = _conn.execute(
            """
            SELECT r.* FROM runs r
            JOIN (SELECT device_id, MAX(COALESCE(started_at, id)) AS mx FROM runs GROUP BY device_id) m
              ON r.device_id = m.device_id AND COALESCE(r.started_at, r.id) = m.mx
            """
        ).fetchall()
    return {r["device_id"]: _row_to_dict(r) for r in rows}
