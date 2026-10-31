"""Sequential queue engine: runs one device at a time through the agent."""
import asyncio
import time
from datetime import datetime

from . import config, store
from .agent_client import AgentBusy

AGENT_POLL_SECONDS = 2
AGENT_LOST_TIMEOUT_SECONDS = 120   # agent unreachable for 2 min -> fail the device
DEVICE_TIMEOUT_SECONDS = 1800      # hard safety net for one device (30 min)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Orchestrator:
    def __init__(self, agent, get_devices) -> None:
        self.agent = agent
        self.get_devices = get_devices
        self.task: asyncio.Task | None = None
        self.stop_requested = False
        self.skip_requested = False
        self.state = {
            "active": False,
            "queue": [],
            "index": -1,
            "total": 0,
            "current_device_id": None,
            "current_run_id": None,
            "message": "idle",
            "passed": 0,
            "failed": 0,
            "device_status": {},   # device_id -> queued|running|pass|fail|error|stopped|skipped
            "agent_step": None,
            "agent_step_index": 0,
            "agent_log": [],
        }

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        snap = dict(self.state)
        snap["device_status"] = dict(self.state["device_status"])
        snap["queue"] = list(self.state["queue"])
        return snap

    def start(self, device_ids: list[str], settings: dict) -> None:
        if self.state["active"]:
            raise RuntimeError("a run queue is already active")
        self.stop_requested = False
        self.skip_requested = False
        self.state.update(
            active=True,
            queue=list(device_ids),
            index=-1,
            total=len(device_ids),
            current_device_id=None,
            current_run_id=None,
            message="starting...",
            passed=0,
            failed=0,
            agent_step=None,
            agent_step_index=0,
            agent_log=[],
            device_status={d: "queued" for d in device_ids},
        )
        self.task = asyncio.create_task(self._run(device_ids, settings))

    def request_stop(self) -> None:
        self.stop_requested = True
        if self.state["active"]:
            self.state["message"] = "stopping..."

    def request_skip(self) -> None:
        self.skip_requested = True
        if self.state["active"]:
            self.state["message"] = "skipping current device..."

    # ------------------------------------------------------------------ #
    async def _run(self, device_ids: list[str], settings: dict) -> None:
        devices = {d["id"]: d for d in self.get_devices()}
        try:
            for i, did in enumerate(device_ids):
                if self.stop_requested:
                    break
                self.skip_requested = False
                device = devices.get(did)
                if not device:
                    self.state["device_status"][did] = "error"
                    continue

                device_statuses = self.state["device_status"]
                device_statuses[did] = "running"
                self.state["index"] = i
                self.state["current_device_id"] = did
                self.state["message"] = f"{device['name']}: starting emulator..."

                run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "_" + did
                self.state["current_run_id"] = run_id
                store.upsert_run(
                    run_id, did, device["avd"], "running",
                    started_at=_now(), run_dir=str(config.RUNS_DIR / run_id),
                )

                job = {
                    "run_id": run_id,
                    "device": {
                        "id": did,
                        "name": device["name"],
                        "avd": device["avd"],
                        "api": device.get("api"),
                        "android": device.get("android"),
                        # Optional per-profile tuning. The agent clamps these
                        # against the host CPU count and checks free RAM.
                        "memory_mb": device.get("memory_mb"),
                        "cores": device.get("cores"),
                        "profile": device.get("profile"),
                    },
                    "package_name": settings.get("package_name", ""),
                    "install": {
                        "mode": settings.get("install_mode", "apk"),
                        "apk_path": settings.get("apk_path", ""),
                        "aab_path": settings.get("aab_path", ""),
                        "play_url": settings.get("play_url", ""),
                    },
                    "observe": {
                        "settle_seconds": settings.get("settle_seconds", 8),
                        "screenshot_offsets": settings.get("screenshot_offsets", "2,8,20"),
                    },
                    "quick_boot": bool(settings.get("quick_boot", False)),
                    "wipe": bool(settings.get("wipe_before_run", False)),
                    "auto_stop": bool(settings.get("auto_stop_emulator", True)),
                }

                try:
                    await self.agent.start_run(job)
                except AgentBusy as exc:
                    device_statuses[did] = "error"
                    store.upsert_run(
                        run_id, did, device["avd"], "error",
                        finished_at=_now(),
                        result={"status": "error", "reasons": [f"agent busy: {exc}"]},
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    device_statuses[did] = "error"
                    store.upsert_run(
                        run_id, did, device["avd"], "error",
                        finished_at=_now(),
                        result={"status": "error", "reasons": [f"agent unreachable: {exc}"]},
                    )
                    continue

                # ---- poll agent until the device run is finished
                result = None
                started_mono = time.monotonic()
                lost_since: float | None = None
                stop_sent = False
                while True:
                    if (self.stop_requested or self.skip_requested) and not stop_sent:
                        # /stop now kills the emulator immediately, so a hung
                        # adb install cannot keep the queue stuck for minutes.
                        try:
                            await self.agent.stop()
                        except Exception:  # noqa: BLE001
                            pass
                        stop_sent = True

                    try:
                        st = await self.agent.status()
                        lost_since = None
                    except Exception as exc:  # noqa: BLE001
                        if lost_since is None:
                            lost_since = time.monotonic()
                        elapsed = time.monotonic() - lost_since
                        self.state["message"] = f"agent connection lost: {exc} (retrying)"
                        if elapsed > AGENT_LOST_TIMEOUT_SECONDS:
                            result = {
                                "status": "error",
                                "reasons": [
                                    f"lost contact with the host agent for {int(elapsed)}s; "
                                    "check that host-agent\\run-agent.bat is still running"
                                ],
                            }
                            break
                        await asyncio.sleep(3)
                        continue

                    self.state["agent_step"] = st.get("step")
                    self.state["agent_step_index"] = st.get("step_index", 0)
                    self.state["agent_log"] = (st.get("log") or [])[-40:]

                    if time.monotonic() - started_mono > DEVICE_TIMEOUT_SECONDS:
                        try:
                            await self.agent.stop()
                        except Exception:  # noqa: BLE001
                            pass
                        result = {
                            "status": "error",
                            "reasons": [
                                f"device run exceeded {DEVICE_TIMEOUT_SECONDS // 60} minutes "
                                "and was stopped (emulator or adb is stuck)"
                            ],
                        }
                        break

                    if st.get("state") in ("done", "error", "stopped"):
                        result = st.get("result")
                        break
                    if st.get("state") == "idle":
                        # the agent restarted or lost the run
                        result = {
                            "status": "error",
                            "reasons": ["agent lost the run (was it restarted mid-run?)"],
                        }
                        break
                    await asyncio.sleep(AGENT_POLL_SECONDS)

                status = ((result or {}).get("status") or "error").lower()
                if status == "pass":
                    self.state["passed"] += 1
                elif status == "fail":
                    self.state["failed"] += 1

                if status == "stopped" and self.skip_requested:
                    disp = "skipped"
                elif status == "stopped" and self.stop_requested:
                    disp = "stopped"
                elif status in ("pass", "fail", "error"):
                    disp = status
                else:
                    disp = "error"

                device_statuses[did] = disp
                store.upsert_run(
                    run_id, did, device["avd"], disp,
                    finished_at=_now(), result=result,
                )
                if self.stop_requested:
                    break

        finally:
            statuses = self.state["device_status"]
            for d, s in list(statuses.items()):
                if s == "queued":
                    statuses[d] = "idle"
            self.state["active"] = False
            self.state["current_device_id"] = None
            self.state["current_run_id"] = None
            self.state["agent_step"] = None
            self.state["message"] = "finished"
            self.stop_requested = False
            self.skip_requested = False
