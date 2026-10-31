"""Android Device Portal - FastAPI application."""
import asyncio
import html
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, store
from .agent_client import AgentClient
from .orchestrator import Orchestrator

agent = AgentClient()
orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global orchestrator
    store.init()
    orchestrator = Orchestrator(agent, config.load_devices)
    yield
    await agent.aclose()


app = FastAPI(title="Android Device Portal", lifespan=lifespan)

if config.WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


def orch() -> Orchestrator:
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="portal is still starting")
    return orchestrator


def device_rows() -> list[dict]:
    devices = config.load_devices()
    o = orch().snapshot()
    last = store.latest_run_per_device()
    rows = []
    for d in devices:
        did = d["id"]
        lr = last.get(did)
        status = o["device_status"].get(did) or (lr["status"] if lr else "idle")
        rows.append({
            **d,
            "status": status,
            "is_running": o["current_device_id"] == did,
            "last_run_id": lr["id"] if lr else None,
            "last_run_status": lr["status"] if lr else None,
        })
    return rows


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(config.WEB_DIR / "index.html")


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def api_health():
    try:
        h = await asyncio.wait_for(agent.health(), timeout=6)
        return {"portal": "ok", "agent": {"ok": True, **h}}
    except Exception as exc:  # noqa: BLE001
        return {"portal": "ok", "agent": {"ok": False, "error": str(exc)}}


@app.get("/api/state")
async def api_state():
    o = orch().snapshot()
    try:
        st = await asyncio.wait_for(agent.status(), timeout=4)
        agent_info = {
            "ok": True,
            "state": st.get("state"),
            "step": st.get("step"),
            "step_index": st.get("step_index"),
            "total_steps": st.get("total_steps"),
            "device": st.get("device"),
            "log": (st.get("log") or [])[-40:],
        }
    except Exception as exc:  # noqa: BLE001
        agent_info = {"ok": False, "error": str(exc)}
    return {
        "queue": o,
        "devices": device_rows(),
        "settings": store.get_settings(),
        "agent": agent_info,
    }


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@app.get("/api/settings")
async def api_settings_get():
    return store.get_settings()


@app.post("/api/settings")
async def api_settings_post(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return store.save_settings(payload or {})


# --------------------------------------------------------------------------- #
# Queue control
# --------------------------------------------------------------------------- #
@app.post("/api/queue/start")
async def api_queue_start(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    payload = payload or {}

    settings = store.get_settings()
    overrides = payload.get("settings") or {}
    if overrides:
        settings = store.save_settings(overrides)

    mode = settings.get("install_mode", "installed")
    if mode == "apk" and not settings.get("apk_path"):
        raise HTTPException(400, "No APK path set - upload an APK or type its path in settings.")
    if mode == "aab" and not settings.get("aab_path"):
        raise HTTPException(400, "No AAB path set - upload an AAB or type its path in settings.")
    if mode == "play" and not settings.get("play_url"):
        raise HTTPException(400, "Play-link mode needs the internal-testing link.")
    # Package name is optional: the agent auto-detects the app when exactly one
    # third-party package is installed (or when it is the only new package).

    device_ids = payload.get("device_ids")
    if not device_ids:
        device_ids = [d["id"] for d in config.load_devices()]

    try:
        orch().start(device_ids, settings)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "queued": device_ids}


@app.post("/api/queue/stop")
async def api_queue_stop():
    o = orch()
    if o.state["active"]:
        o.request_stop()
    else:
        try:
            await agent.stop()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@app.post("/api/queue/skip")
async def api_queue_skip():
    o = orch()
    if not o.state["active"]:
        raise HTTPException(409, "no active run")
    o.request_skip()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Device actions
# --------------------------------------------------------------------------- #
@app.get("/api/devices/{device_id}/live.png")
async def device_live(device_id: str):
    png = await agent.screenshot()
    if not png:
        raise HTTPException(404, "no screenshot yet")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/devices/{device_id}/wipe")
async def device_wipe(device_id: str):
    devices = {d["id"]: d for d in config.load_devices()}
    dev = devices.get(device_id)
    if not dev:
        raise HTTPException(404, "unknown device")
    try:
        return await agent.wipe(dev["avd"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"agent error: {exc}") from exc


@app.post("/api/agent/kill-emulator")
async def kill_emulator():
    try:
        return await agent.kill_emulator()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"agent error: {exc}") from exc


# --------------------------------------------------------------------------- #
# Runs / artifacts
# --------------------------------------------------------------------------- #
@app.get("/api/runs")
async def api_runs(limit: int = 50):
    return {"runs": store.list_runs(max(1, min(limit, 200)))}


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    run_dir = config.resolve_run_dir(run.get("run_dir"))
    artifacts = []
    if run_dir.exists():
        for p in sorted(run_dir.rglob("*")):
            if p.is_file():
                artifacts.append({
                    "path": str(p.relative_to(run_dir)).replace("\\", "/"),
                    "size": p.stat().st_size,
                })
    run["artifacts"] = artifacts
    return run


@app.get("/api/runs/{run_id}/file")
async def api_run_file(run_id: str, path: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    base = config.resolve_run_dir(run.get("run_dir")).resolve()
    target = (base / path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(404, "file not found") from None
    if not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), kind: str = "apk"):  # noqa: B008
    config.ensure_dirs()
    safe = Path(file.filename or f"upload.{kind}").name
    fname = datetime.now().strftime("%Y%m%d-%H%M%S") + "_" + safe
    dest = config.UPLOADS_DIR / fname
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    host_path = None
    if config.HOST_DATA_DIR:
        host_path = config.HOST_DATA_DIR + "\\uploads\\" + fname
    return {
        "filename": fname,
        "container_path": f"/data/uploads/{fname}",
        "host_path": host_path,
        "size": dest.stat().st_size,
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
@app.get("/api/report", response_class=HTMLResponse)
async def api_report():
    runs = store.list_runs(100)
    devs = {d["id"]: d for d in config.load_devices()}
    total = len(runs)
    passed = sum(1 for r in runs if r["status"] == "pass")
    failed = sum(1 for r in runs if r["status"] == "fail")
    errors = sum(1 for r in runs if r["status"] == "error")

    sections = []
    for r in runs:
        detail = store.get_run(r["id"]) or {}
        res = detail.get("result") or {}
        status = r["status"]
        color = {"pass": "#34d399", "fail": "#f87171", "error": "#fbbf24"}.get(status, "#9ca3af")
        dev = devs.get(r["device_id"], {})
        shots = ""
        run_dir = config.resolve_run_dir(detail.get("run_dir"))
        shots_dir = run_dir / "screenshots"
        if shots_dir.exists():
            for p in sorted(shots_dir.glob("*.png")):
                rel = str(p.relative_to(run_dir)).replace("\\", "/")
                shots += (
                    f'<img src="/api/runs/{html.escape(r["id"])}/file?path={html.escape(rel)}" '
                    f'style="height:260px;border:1px solid #2d3748;border-radius:8px;margin:4px">'
                )
        reasons = "<br>".join(html.escape(str(x)) for x in (res.get("reasons") or []))
        crashes = "\n".join(str(x) for x in (res.get("crashes") or [])[:15])
        info = res.get("device_info") or {}
        info_html = " · ".join(
            f"{html.escape(str(k))}: {html.escape(str(v))}"
            for k, v in info.items() if v
        )
        sections.append(f"""
        <div style="border:1px solid #1f2937;border-radius:10px;padding:14px;margin:14px 0;background:#111827">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h3 style="margin:0">{html.escape(dev.get('name', r['device_id']))}
              <small style="color:#6b7280"> · API {html.escape(str(dev.get('api', '?')))} · {html.escape(r['avd'] or '')}</small>
            </h3>
            <span style="background:{color};color:#03121f;padding:2px 10px;border-radius:999px;font-weight:700">{html.escape(status)}</span>
          </div>
          <div style="color:#9ca3af;font-size:13px;margin:6px 0">{html.escape(r['started_at'] or '')} → {html.escape(r['finished_at'] or '')}</div>
          <div style="margin:6px 0">{reasons}</div>
          {f'<pre style="background:#0b1220;padding:10px;border-radius:8px;overflow:auto;font-size:12px;color:#fca5a5">{html.escape(crashes)}</pre>' if crashes else ''}
          <div style="color:#6b7280;font-size:12px;margin:6px 0">{info_html}</div>
          <div>{shots}</div>
        </div>""")

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Device Portal report</title></head>
<body style="background:#030712;color:#e5e7eb;font-family:system-ui,Segoe UI,sans-serif;max-width:1100px;margin:0 auto;padding:24px">
<h1>📱 Device Portal report</h1>
<p style="color:#9ca3af">Generated {html.escape(datetime.now().strftime('%Y-%m-%d %H:%M'))} ·
{runs and html.escape(runs[-1]['id'][:15]) or ''}</p>
<p><b>{total}</b> runs · <span style="color:#34d399">{passed} pass</span> ·
<span style="color:#f87171">{failed} fail</span> · <span style="color:#fbbf24">{errors} error</span></p>
{''.join(sections) or '<p>No runs yet.</p>'}
</body></html>"""
