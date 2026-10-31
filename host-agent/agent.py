#!/usr/bin/env python3
"""
Android Device Portal - Host Agent (Windows)

Standard-library only: no pip installs needed on the host.
Drives ONE Android emulator at a time via emulator.exe + adb.exe and exposes
a small JSON HTTP API that the portal container calls.

Endpoints (all require header X-Agent-Token):
  GET  /health              environment report
  GET  /avds                installed AVD names
  GET  /status              live run state + log tail
  POST /run                 start a device run (409 if already running)
  POST /stop                stop current run + kill emulator
  POST /wipe                {"avd": "..."} wipe device data on next boot
  POST /kill-emulator       emergency kill of the emulator on our port
  GET  /screenshot          latest PNG frame (404 if none yet)

Config: agent-config.json next to this file (created by install-agent.ps1).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

AGENT_VERSION = "1.3"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "agent-config.json"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DEFAULT_CONFIG = {
    "port": 8765,
    "token": "change-me",
    "sdk_root": str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"),
    "data_root": str(HERE.parent / "data"),
    "java_exe": "",
    "bundletool_jar": str(HERE.parent / "tools" / "bundletool.jar"),
    "serial_port": 5554,
    "emulator": {
        "memory_mb": 3072,
        "cores": 4,
        "gpu": "auto",
        "fallback_gpu": "swiftshader_indirect",
        "headless": False,
        "extra_args": [],
        "boot_timeout_seconds": 300,
        "boot_attempts": 3,
        "recovery_wipe": True,
    },
    "limits": {
        "min_free_ram_mb": 3000,
        "min_free_disk_gb": 12,
        # Extra host memory kept free on top of the guest's RAM so Windows does
        # not start swapping while the emulator is running (a big cause of
        # "booted but extremely slow" and random emulator crashes).
        "ram_headroom_mb": 1024,
    },
}

CFG: dict = {}
STEPS = [
    "preflight",
    "boot_device",
    "install_app",
    "launch_app",
    "observe",
    "close_app",
    "collect",
    "shutdown_device",
    "save_result",
]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> None:
    global CFG
    user = {}
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] could not read {CONFIG_PATH}: {exc}", flush=True)
    CFG = deep_merge(DEFAULT_CONFIG, user)
    CFG["_root"] = str(HERE.parent)


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #
class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.wipe_next: dict[str, bool] = {}
        self.reset()

    def reset(self) -> None:
        self.state = "idle"  # idle | running | done | error | stopped
        self.run_id: str | None = None
        self.device: dict | None = None
        self.step = ""
        self.step_index = 0
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.log: list[dict] = []
        self.result: dict | None = None
        self.run_dir: str | None = None
        self.stop_requested = False
        self.latest_shot_png: bytes | None = None
        self.latest_shot_ts = 0.0
        self.emulator_proc: subprocess.Popen | None = None
        self.emulator_log_file = None


STATE = State()
_run_log_path: Path | None = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(msg: str, level: str = "info") -> None:
    line = {"ts": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    with STATE.lock:
        STATE.log.append(line)
        if len(STATE.log) > 300:
            STATE.log = STATE.log[-300:]
    if _run_log_path is not None:
        try:
            with open(_run_log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{line['ts']}] {msg}\n")
        except OSError:
            pass
    print(f"[{line['ts']}] {msg}", flush=True)


def set_step(name: str) -> None:
    with STATE.lock:
        STATE.step = name
        try:
            STATE.step_index = STEPS.index(name) + 1
        except ValueError:
            STATE.step_index = 0
    log(f"--- step: {name} ---")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class RunError(Exception):
    """a run failed for an understood reason"""


class RunStopped(Exception):
    """user requested stop"""


def check_stop() -> None:
    with STATE.lock:
        if STATE.stop_requested:
            raise RunStopped()


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #
def run(cmd: list, timeout: int = 60, cwd: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=cwd,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", "replace") if exc.stdout else ""
        return 124, partial + f"\n[timeout after {timeout}s]"
    except FileNotFoundError:
        return 127, f"executable not found: {cmd[0]}"


def sdk_root() -> Path:
    return Path(CFG.get("sdk_root", ""))


def emulator_exe() -> Path:
    return sdk_root() / "emulator" / "emulator.exe"


def adb_exe() -> Path:
    return sdk_root() / "platform-tools" / "adb.exe"


def serial() -> str:
    return f"emulator-{CFG.get('serial_port', 5554)}"


def adb(*args, timeout: int = 60) -> tuple[int, str]:
    return run([str(adb_exe()), "-s", serial(), *args], timeout=timeout)


def find_java() -> str | None:
    cfg_java = CFG.get("java_exe") or ""
    if cfg_java and Path(cfg_java).exists():
        return cfg_java
    tools = Path(CFG["_root"]) / "tools"
    for base in sorted(tools.glob("jdk*")):
        if base.is_dir():
            for cand in base.rglob("java.exe"):
                if cand.parent.name == "bin":
                    return str(cand)
    if os.name == "nt":
        for cand in (
            Path("C:/Program Files/Android/Android Studio/jbr/bin/java.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Android Studio/jbr/bin/java.exe",
        ):
            if cand.exists():
                return str(cand)
    which = shutil.which("java")
    return which or None


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def free_ram_mb() -> int:
    if os.name == "nt":
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return int(stat.ullAvailPhys // (1024 * 1024))
    return 0


def safe_list_avds() -> list[str]:
    if not emulator_exe().exists():
        return []
    rc, out = run([str(emulator_exe()), "-list-avds"], timeout=45, cwd=str(emulator_exe().parent))
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith("INFO")]


def capture_screenshot() -> bytes | None:
    try:
        proc = subprocess.run(
            [str(adb_exe()), "-s", serial(), "exec-out", "screencap", "-p"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        data = proc.stdout or b""
        if proc.returncode == 0 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return data
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def kill_port_holders() -> None:
    port = int(CFG.get("serial_port", 5554))
    rc, out = run(["netstat", "-ano", "-p", "TCP"], timeout=45)
    if rc != 0:
        return
    pids = set()
    for line in out.splitlines():
        if re.search(rf":{port}\s|:{port + 1}\s", line) and "LISTENING" in line.upper():
            pids.add(line.split()[-1])
    for pid in pids:
        log(f"killing stray process {pid} holding TCP {port}/{port + 1}")
        # /T also kills the qemu child started by emulator.exe. Without it an
        # orphaned qemu can keep the AVD locked (the classic "device will not
        # start the second time" bug).
        run(["taskkill", "/F", "/T", "/PID", pid], timeout=30)


# --------------------------------------------------------------------------- #
# AVD state / crash recovery
# --------------------------------------------------------------------------- #
def avd_home() -> Path:
    return Path(os.environ.get("ANDROID_AVD_HOME") or (Path.home() / ".android" / "avd"))


def avd_dir(avd: str) -> Path:
    return avd_home() / f"{avd}.avd"


def avd_exists(avd: str) -> bool:
    return (avd_dir(avd) / "config.ini").exists()


def _remove_path(path: Path) -> bool:
    """Remove a file/dir even when it is one of the emulator's odd lock dirs."""
    if not path.exists() and not path.is_symlink():
        return False
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        # Windows sometimes gives these lock folders an ACL that Python cannot
        # traverse; the shell builtins still remove them.
        if os.name == "nt":
            if path.is_dir():
                run(["cmd", "/c", "rmdir", "/s", "/q", str(path)], timeout=30)
            else:
                run(["cmd", "/c", "del", "/f", "/q", str(path)], timeout=30)
            return not path.exists()
    return False


def clean_avd_locks(avd: str) -> int:
    """Delete stale emulator lock files left behind by a crash or force-kill."""
    folder = avd_dir(avd)
    if not folder.exists():
        return 0
    removed = 0
    candidates = list(folder.glob("*.lock"))
    snapshots = folder / "snapshots"
    if snapshots.exists():
        candidates += list(snapshots.rglob("*.lock"))
    for path in candidates:
        if _remove_path(path):
            removed += 1
    if removed:
        log(f"cleaned {removed} stale AVD lock(s) for {avd}")
    return removed


def clean_quick_boot_snapshot(avd: str) -> bool:
    """Drop a corrupt quick-boot snapshot. User data is left untouched."""
    folder = avd_dir(avd) / "snapshots" / "default_boot"
    if not folder.exists():
        return False
    if _remove_path(folder):
        log(f"deleted the quick-boot snapshot for {avd} (it may be stale/corrupt)")
        return True
    return False


def has_quick_boot_snapshot(avd: str) -> bool:
    """A snapshot is only usable when snapshot.pb is present and non-empty."""
    pb = avd_dir(avd) / "snapshots" / "default_boot" / "snapshot.pb"
    try:
        return pb.is_file() and pb.stat().st_size > 0
    except OSError:
        return False


def clean_hardware_ini(avd: str) -> bool:
    """Remove the generated hardware-qemu.ini so the next boot starts clean."""
    return _remove_path(avd_dir(avd) / "hardware-qemu.ini")


def snapshot_ram_mb(avd: str) -> int:
    """RAM stored inside an existing quick-boot snapshot, in MB."""
    hw = avd_dir(avd) / "snapshots" / "default_boot" / "hardware.ini"
    try:
        text = hw.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    match = re.search(r"^\s*hw\.ramSize\s*=\s*(\d+)", text, re.MULTILINE)
    return int(match.group(1)) if match else 0


def snapshot_cores(avd: str) -> int:
    hw = avd_dir(avd) / "snapshots" / "default_boot" / "hardware.ini"
    try:
        text = hw.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    match = re.search(r"^\s*hw\.cpu\.ncore\s*=\s*(\d+)", text, re.MULTILINE)
    return int(match.group(1)) if match else 0


def configured_guest_ram_mb(job: dict) -> int:
    device = job.get("device") or {}
    return int(device.get("memory_mb") or CFG["emulator"]["memory_mb"])


def configured_guest_cores(job: dict) -> int:
    device = job.get("device") or {}
    cores = int(device.get("cores") or CFG["emulator"]["cores"])
    return max(2, min(cores, os.cpu_count() or cores))


def prepare_quick_boot(job: dict) -> None:
    """Drop a snapshot whose RAM/CPU no longer matches the device profile.

    Otherwise the emulator reuses the snapshot's hardware (for example 4 GB)
    even though the profile asked for 3 GB, so the preflight RAM check ends up
    under-estimating the real footprint.
    """
    if not job.get("quick_boot"):
        return
    avd = (job.get("device") or {}).get("avd")
    if not avd:
        return
    if not has_quick_boot_snapshot(avd):
        # Leftover ram.img without snapshot.pb makes the emulator try (and
        # fail) to load a snapshot on every start.
        if clean_quick_boot_snapshot(avd):
            log(f"discarded incomplete quick-boot snapshot for {avd}", "warn")
        return
    want_ram = configured_guest_ram_mb(job)
    want_cores = configured_guest_cores(job)
    have_ram = snapshot_ram_mb(avd)
    have_cores = snapshot_cores(avd)
    if (have_ram and have_ram != want_ram) or (have_cores and have_cores != want_cores):
        if clean_quick_boot_snapshot(avd):
            log(
                f"discarded quick-boot snapshot for {avd} "
                f"(snapshot {have_ram} MB/{have_cores} cores, profile {want_ram} MB/{want_cores} cores)",
                "warn",
            )


def requested_guest_ram_mb(job: dict) -> int:
    ram = configured_guest_ram_mb(job)
    if job.get("quick_boot"):
        # Loading a 4 GB snapshot while the host has only 3 GB free is the main
        # reason quick boot appears to "hang". Check the snapshot's real size.
        ram = max(ram, snapshot_ram_mb(job["device"]["avd"]))
    return ram


def requested_guest_cores(job: dict) -> int:
    return configured_guest_cores(job)


def emulator_effective_ram_mb(run_dir: Path) -> int:
    """Read the RAM the emulator actually allocated from its log."""
    try:
        text = (run_dir / "emulator.log").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    for pattern in (
        r"Increasing RAM size to (\d+)MB",
        r"Physical RAM size: (\d+)MB",
    ):
        matches = re.findall(pattern, text)
        if matches:
            return int(matches[-1])
    return 0


def stop_emulator_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    log(f"stopping emulator process tree {proc.pid}")
    run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=45)
    try:
        proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        log("emulator did not exit after taskkill", "warn")
    kill_port_holders()


def reset_adb_server() -> None:
    """Recover from a wedged adb server after an emulator crash."""
    if not adb_exe().exists():
        return
    log("restarting the adb server")
    run([str(adb_exe()), "kill-server"], timeout=20)
    run([str(adb_exe()), "start-server"], timeout=60)


def kill_stray_emulator(avd: str | None = None) -> None:
    if not adb_exe().exists():
        return
    run([str(adb_exe()), "start-server"], timeout=60)
    rc, out = run([str(adb_exe()), "devices"], timeout=30)
    if serial() in out:
        log(f"stray {serial()} found - shutting it down")
        adb("emu", "kill", timeout=20)
        for _ in range(20):
            time.sleep(1)
            rc, out = run([str(adb_exe()), "devices"], timeout=30)
            if serial() not in out:
                break
    kill_port_holders()
    if avd:
        clean_avd_locks(avd)


def check_hardware_acceleration() -> tuple[bool, str]:
    rc, out = run([str(emulator_exe()), "-accel-check"], timeout=45, cwd=str(emulator_exe().parent))
    text = " ".join((out or "").split())
    if rc == 0 and re.search(r"(installed and usable|is usable|operational)", text, re.IGNORECASE) \
            and not re.search(r"not (installed|usable)|isn't usable", text, re.IGNORECASE):
        return True, text
    return False, text


def tail_file(path: Path, lines: int = 40) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


# --------------------------------------------------------------------------- #
# Screenshot loop (also keeps the portal's live thumbnail fresh)
# --------------------------------------------------------------------------- #
class ScreenshotLoop(threading.Thread):
    def __init__(self, interval: float = 2.5) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._scheduled: list[tuple[float, str, Path]] = []
        self.taken: list[str] = []

    def schedule(self, due_epoch: float, label: str, folder: Path) -> None:
        with self._lock:
            self._scheduled.append((due_epoch, label, folder))

    def pending(self) -> int:
        with self._lock:
            return len(self._scheduled)

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            png = capture_screenshot()
            if png:
                with STATE.lock:
                    STATE.latest_shot_png = png
                    STATE.latest_shot_ts = time.time()
                now = time.time()
                with self._lock:
                    due = [j for j in self._scheduled if j[0] <= now]
                    self._scheduled = [j for j in self._scheduled if j[0] > now]
                for _, label, folder in due:
                    try:
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / f"{label}.png").write_bytes(png)
                        self.taken.append(label)
                        log(f"screenshot saved: {label}")
                    except OSError as exc:
                        log(f"screenshot save failed: {exc}", "warn")
            self._stop_evt.wait(self.interval)


# --------------------------------------------------------------------------- #
# Run worker
# --------------------------------------------------------------------------- #
def base_result(job: dict, status: str, reasons: list[str]) -> dict:
    started = job.get("_started_epoch", time.time())
    return {
        "status": status,
        "reasons": reasons,
        "run_id": job.get("run_id"),
        "device": job.get("device"),
        "package_name": job.get("package_name") or "",
        "install_mode": (job.get("install") or {}).get("mode"),
        "install_ok": bool(job.get("_install_ok")),
        "launch_ok": bool(job.get("_launch_ok")),
        "process_alive": job.get("_process_alive"),
        "crashes": job.get("_crashes", []),
        "screenshots": job.get("_screenshots", []),
        "device_info": job.get("_device_info", {}),
        "meminfo_tail": job.get("_meminfo_tail", ""),
        "timings": {
            "boot_seconds": job.get("_boot_seconds"),
            "total_seconds": round(time.time() - started, 1),
        },
        "boot": {
            "attempts": job.get("_boot_attempts"),
            "memory_mb": job.get("_guest_ram_mb"),
            "cores": job.get("_guest_cores"),
            "gpu": job.get("_gpu_used"),
            "snapshot": bool(job.get("_snapshot_boot")),
            "recovery": job.get("_recovery_actions", []),
        },
        "started_at": job.get("_started_at"),
        "finished_at": now_iso(),
    }


def step_preflight(job: dict) -> None:
    check_stop()
    if not emulator_exe().exists():
        raise RunError(f"emulator.exe not found at {emulator_exe()} - is the Android SDK in place?")
    if not adb_exe().exists():
        raise RunError(f"adb.exe not found at {adb_exe()}")
    avd = job["device"]["avd"]
    if not avd_exists(avd):
        raise RunError(
            f"AVD {avd} is not installed - run scripts\\setup_devices.ps1 to create the device matrix"
        )

    accel_ok, accel_text = check_hardware_acceleration()
    log(f"hypervisor: {accel_text or 'no output'}")
    if not accel_ok:
        raise RunError(
            "hardware acceleration is not usable - enable Windows Hypervisor Platform "
            "(Windows Features) or turn on virtualization in BIOS, then retry"
        )

    # Recover stale state BEFORE the RAM check: an orphaned qemu holds both
    # port 5554 and a large chunk of RAM, which used to make preflight fail
    # with a bogus "not enough RAM" error.
    kill_stray_emulator(avd)
    clean_avd_locks(avd)
    prepare_quick_boot(job)
    check_stop()

    ram = free_ram_mb()
    guest_mb = requested_guest_ram_mb(job)
    cores = requested_guest_cores(job)
    headroom = int(CFG["limits"].get("ram_headroom_mb", 1024))
    if guest_mb >= 4096:
        # A 4 GB guest is only used where the emulator forces it (API 36.1).
        # Requiring a full extra gigabyte makes those profiles almost
        # unrunnable on a 16 GB host; 512 MB still leaves the OS breathing
        # room while the guest allocates RAM lazily.
        headroom = int(CFG["limits"].get("ram_headroom_4gb_mb", 512))
    need_mb = max(int(CFG["limits"]["min_free_ram_mb"]), guest_mb + headroom)
    job["_guest_ram_mb"] = guest_mb
    job["_guest_cores"] = cores
    log(f"free RAM: {ram} MB (guest {guest_mb} MB + {headroom} MB headroom; need {need_mb} MB)")
    if ram and ram < need_mb:
        # A just-stopped emulator can take a while to hand its RAM back to
        # Windows. Without this grace period the next device in a 16-device
        # sweep randomly fails with a bogus "not enough RAM" error.
        wait_seconds = 90 if guest_mb >= 4096 else 30
        log(f"waiting up to {wait_seconds}s for host RAM to recover (need {need_mb} MB)", "warn")
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            check_stop()
            time.sleep(3)
            ram = free_ram_mb()
            if ram >= need_mb:
                log(f"host RAM recovered to {ram} MB")
                break
    if ram and ram < need_mb:
        raise RunError(
            f"only {ram} MB RAM free - this device needs {need_mb} MB free "
            f"({guest_mb} MB guest + {headroom} MB headroom). Close Android Studio / Chrome / "
            "other heavy apps and retry"
        )

    du = shutil.disk_usage(Path(CFG["data_root"]).anchor or "C:\\")
    free_gb = du.free / (1024**3)
    log(f"free disk: {free_gb:.1f} GB")
    if free_gb < float(CFG["limits"]["min_free_disk_gb"]):
        raise RunError(f"only {free_gb:.1f} GB free disk - clean up before running")

    # Catch a stale upload / package-name mismatch before spending a minute
    # booting an emulator just to fail at launch.
    install = job.get("install") or {}
    mode = install.get("mode", "apk")
    artifact = install.get("aab_path" if mode == "aab" else "apk_path") or ""
    if mode in ("apk", "aab") and artifact and Path(artifact).exists():
        check_artifact_package(job, artifact, mode)

    check_stop()


def _adb_device_visible() -> bool:
    rc, out = run([str(adb_exe()), "devices"], timeout=20)
    if rc != 0:
        return False
    for line in out.splitlines():
        if line.strip().startswith(serial() + "\t"):
            return "offline" not in line.lower()
    return False


def _adb_state() -> str:
    """Return `device`, `offline`, or an empty string when not listed."""
    rc, out = run([str(adb_exe()), "devices"], timeout=20)
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial():
            return parts[1].lower()
    return ""


def _wait_for_boot(
    job: dict,
    proc: subprocess.Popen,
    timeout: int,
    attempt: int,
    snapshot_boot: bool = False,
) -> tuple[bool, str]:
    t0 = time.time()
    adb_restarted = False
    offline_since = 0.0
    while time.time() - t0 < timeout:
        check_stop()
        if proc.poll() is not None:
            return False, f"emulator exited during boot (exit code {proc.returncode})"
        rc, out = adb("shell", "getprop", "sys.boot_completed", timeout=20)
        if rc == 0 and out.strip() == "1":
            rc2, out2 = adb("shell", "pm", "path", "android", timeout=25)
            if rc2 == 0 and "package:" in out2:
                return True, ""
        if snapshot_boot:
            state = _adb_state()
            if state == "offline":
                if not offline_since:
                    offline_since = time.time()
                elif time.time() - offline_since > 60:
                    return False, "emulator stayed offline after loading its quick-boot snapshot"
            else:
                offline_since = 0.0
        if not adb_restarted and time.time() - t0 > 75 and not _adb_device_visible():
            log("adb still cannot see the emulator - restarting the adb server once", "warn")
            reset_adb_server()
            adb_restarted = True
        time.sleep(4)
    return False, f"boot timed out after {timeout}s"


def step_boot(job: dict) -> None:
    avd = job["device"]["avd"]
    emu = CFG["emulator"]
    attempts = max(1, int(emu.get("boot_attempts", 3)))
    boot_timeout = max(60, int(emu.get("boot_timeout_seconds", 300)))
    wipe_requested = bool(job.get("wipe"))
    if STATE.wipe_next.pop(avd, False):
        wipe_requested = True
        log("cold wipe of AVD data requested")

    job["_boot_attempts"] = 0
    job["_gpu_used"] = ""
    job["_recovery_actions"] = []
    emu_log = open(Path(job["_run_dir"]) / "emulator.log", "wb")  # noqa: SIM115
    with STATE.lock:
        STATE.emulator_log_file = emu_log

    last_reason = ""
    try:
        for attempt in range(1, attempts + 1):
            check_stop()
            quick_boot = bool(job.get("quick_boot", False))
            snapshot_boot = (
                quick_boot
                and attempt == 1
                and not wipe_requested
                and has_quick_boot_snapshot(avd)
            )
            cold_boot = attempt > 1 or not quick_boot
            gpu = str(emu.get("gpu", "auto"))

            if attempt > 1:
                fallback = str(emu.get("fallback_gpu") or "").strip()
                if fallback:
                    gpu = fallback
                log(f"boot attempt {attempt}/{attempts} after: {last_reason}", "warn")
                # A corrupt quick-boot snapshot or generated hardware file is
                # the usual reason attempt 1 failed. Remove them and try cold.
                clean_quick_boot_snapshot(avd)
                clean_hardware_ini(avd)
                clean_avd_locks(avd)
                reset_adb_server()
                snapshot_boot = False
                job["_recovery_actions"].append(
                    f"attempt {attempt}: cleaned snapshot/hardware state, cold boot"
                )

            wipe = wipe_requested or (
                attempt > 1 and attempt >= attempts and bool(emu.get("recovery_wipe", True))
            )
            if wipe:
                if snapshot_boot:
                    clean_quick_boot_snapshot(avd)
                    snapshot_boot = False
                if attempt == 1:
                    job["_recovery_actions"].append("wipe AVD data requested")
                else:
                    job["_recovery_actions"].append(f"attempt {attempt}: wiped AVD data")
                log("booting with -wipe-data (fresh device data)")

            args = [
                str(emulator_exe()),
                "-avd", avd,
                "-port", str(CFG.get("serial_port", 5554)),
                "-memory", str(job["_guest_ram_mb"]),
                "-cores", str(job["_guest_cores"]),
                "-gpu", gpu,
                "-no-boot-anim",
                "-no-audio",
                "-netdelay", "none",
                "-netspeed", "full",
            ]
            if emu.get("headless"):
                args.append("-no-window")
            if cold_boot or wipe:
                args += ["-no-snapshot-load", "-no-snapshot-save"]
            if wipe:
                args.append("-wipe-data")
            args += [str(a) for a in (emu.get("extra_args") or [])]

            # hardware-qemu.ini and the snapshot belong together: loading a
            # snapshot against a freshly regenerated hardware file can boot
            # the guest into an "offline forever" state. Only discard the
            # file when we are not loading a snapshot.
            if snapshot_boot:
                log("loading quick-boot snapshot")
            elif clean_hardware_ini(avd):
                log("removed cached hardware-qemu.ini (stale RAM/GPU settings)")

            job["_boot_attempts"] = attempt
            job["_gpu_used"] = gpu
            job["_snapshot_boot"] = snapshot_boot
            job["_discard_snapshot_after_shutdown"] = (not quick_boot) or attempt > 1 or wipe
            log(f"starting emulator (attempt {attempt}/{attempts}): " + " ".join(args))
            try:
                child_env = os.environ.copy()
                # `adb emu kill` makes the emulator spawn a helper that waits
                # 20s before terminating qemu. Shorten that so a 16-device
                # batch does not spend 6 minutes just shutting down.
                child_env.setdefault("ANDROID_EMULATOR_WAIT_TIME_BEFORE_KILL", "5")
                proc = subprocess.Popen(
                    args,
                    stdout=emu_log,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    cwd=str(emulator_exe().parent),
                    env=child_env,
                )
            except OSError as exc:
                raise RunError(f"could not start emulator: {exc}") from exc
            with STATE.lock:
                STATE.emulator_proc = proc

            t0 = time.time()
            ok, reason = _wait_for_boot(job, proc, boot_timeout, attempt, snapshot_boot)
            if ok:
                job["_boot_seconds"] = round(time.time() - t0, 1)
                actual_ram = emulator_effective_ram_mb(Path(job["_run_dir"]))
                if actual_ram and actual_ram != job["_guest_ram_mb"]:
                    log(
                        f"emulator raised guest RAM to {actual_ram} MB "
                        f"(profile requested {job['_guest_ram_mb']} MB)",
                        "warn",
                    )
                    job["_guest_ram_mb"] = actual_ram
                log(
                    f"device booted in {job['_boot_seconds']}s "
                    f"(attempt {attempt}/{attempts}, {job['_guest_ram_mb']} MB RAM, "
                    f"{job['_guest_cores']} cores, gpu={gpu})"
                )
                break

            last_reason = reason
            log(f"boot attempt {attempt}/{attempts} failed: {reason}", "warn")
            stop_emulator_proc(proc)
            with STATE.lock:
                STATE.emulator_proc = None
            clean_avd_locks(avd)
        else:
            detail = tail_file(Path(job["_run_dir"]) / "emulator.log", 35)
            raise RunError(
                f"emulator failed to boot after {attempts} attempt(s): {last_reason}. "
                f"See emulator.log.\n{detail[-1800:]}"
            )

        # make the device cooperative for screenshots / automation
        adb("shell", "wm", "dismiss-keyguard", timeout=20)
        adb("shell", "input", "keyevent", "82", timeout=20)
        adb("shell", "svc", "power", "stayon", "true", timeout=20)
        adb("shell", "settings", "put", "system", "screen_off_timeout", "2147483647", timeout=20)
        for key, val in (
            ("window_animation_scale", "0"),
            ("transition_animation_scale", "0"),
            ("animator_duration_scale", "0"),
        ):
            adb("shell", "settings", "put", "global", key, val, timeout=20)
    finally:
        # The handle stays open for step_shutdown/release_emulator; flushing
        # here makes emulator.log readable while the rest of the run proceeds.
        try:
            emu_log.flush()
        except OSError:
            pass


def third_party_packages() -> set[str]:
    rc, out = adb("shell", "pm", "list", "packages", "-3", timeout=45)
    return {
        line.strip().split(":", 1)[1]
        for line in out.splitlines()
        if line.strip().startswith("package:")
    }


def newest_build_tool(name: str) -> Path | None:
    root = sdk_root() / "build-tools"
    if not root.exists():
        return None
    matches = sorted(root.glob(f"*/{name}"), reverse=True)
    return matches[0] if matches else None


def apk_package_name(apk: str) -> str | None:
    """Read the application id from an APK without installing it."""
    aapt2 = newest_build_tool("aapt2.exe")
    if aapt2 is None:
        return None
    rc, out = run([str(aapt2), "dump", "badging", apk], timeout=120)
    if rc != 0:
        return None
    match = re.search(r"^package: name='([^']+)'", out, re.MULTILINE)
    return match.group(1) if match else None


def aab_package_name(aab: str) -> str | None:
    """Read the application id from an AAB via bundletool."""
    jar = Path(CFG.get("bundletool_jar", ""))
    java = find_java()
    if not jar.exists() or not java:
        return None
    rc, out = run(
        [java, "-jar", str(jar), "dump", "manifest", f"--bundle={aab}"],
        timeout=180,
    )
    if rc != 0:
        return None
    match = re.search(r'\bpackage="([^"]+)"', out)
    return match.group(1) if match else None


def detect_single_package(candidates: set[str]) -> str:
    """Pick the app automatically when exactly one third-party package exists."""
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        raise RunError(
            "no third-party app is installed on this AVD yet - install the testing app once "
            "(Play Store link or adb), then run again"
        )
    raise RunError(
        "multiple third-party apps are installed ("
        + ", ".join(sorted(candidates))
        + "). Set the package name in portal settings to choose the testing app."
    )


def check_artifact_package(job: dict, artifact: str, mode: str) -> str:
    """Fail early when the upload and the configured package disagree."""
    pkg = (job.get("package_name") or "").strip()
    if job.get("_artifact_path") == artifact and "_artifact_package" in job:
        actual = job.get("_artifact_package") or ""
    else:
        actual = aab_package_name(artifact) if mode == "aab" else apk_package_name(artifact)
        job["_artifact_path"] = artifact
        job["_artifact_package"] = actual or ""
    if not actual:
        return pkg
    if pkg and pkg != actual:
        kind = "AAB" if mode == "aab" else "APK"
        raise RunError(
            f"the uploaded {kind} is for {actual!r}, but portal settings say {pkg!r}. "
            f"Update the package name to {actual!r} (or upload the matching build) and retry."
        )
    if not pkg:
        job["package_name"] = actual
        log(f"detected package from {mode.upper()}: {actual}")
    return actual


TRANSIENT_ADB_RE = re.compile(
    r"offline|closed|connection reset|device not found|no devices|transport|protocol|"
    r"timeout|broken pipe|adb: device",
    re.IGNORECASE,
)


def step_install(job: dict) -> None:
    check_stop()
    install = job.get("install") or {}
    mode = install.get("mode", "apk")
    pkg = (job.get("package_name") or "").strip()
    before = third_party_packages()

    if mode == "apk":
        apk = install.get("apk_path") or ""
        if not apk or not Path(apk).exists():
            raise RunError(f"APK not found: {apk!r} - set a valid Windows path in portal settings or upload the APK")
        pkg = check_artifact_package(job, apk, "apk") or pkg
        rc, out = adb("install", "-r", "-t", "-g", apk, timeout=420)
        log(out.strip()[-1500:] or "adb install finished")
        if rc != 0 or "Success" not in out:
            if TRANSIENT_ADB_RE.search(out):
                log("install hit a transient adb/emulator error - reconnecting and retrying once", "warn")
                reset_adb_server()
                time.sleep(5)
                rc, out = adb("install", "-r", "-t", "-g", apk, timeout=420)
                log(out.strip()[-1500:] or "adb install retry finished")
        if rc != 0 or "Success" not in out:
            raise RunError("APK install failed (see run log for adb output)")

    elif mode == "aab":
        aab = install.get("aab_path") or ""
        jar = Path(CFG.get("bundletool_jar", ""))
        java = find_java()
        if not aab or not Path(aab).exists():
            raise RunError(f"AAB not found: {aab!r} - set a valid path or upload the .aab")
        if not jar.exists():
            raise RunError(f"bundletool jar missing ({jar}) - run scripts\\setup_devices.ps1 once")
        if not java:
            raise RunError("no Java found for bundletool - run scripts\\setup_devices.ps1 once (it installs a JDK)")
        pkg = check_artifact_package(job, aab, "aab") or pkg
        apks = Path(job["_run_dir"]) / "app.apks"
        rc, out = run(
            [java, "-jar", str(jar), "build-apks", f"--bundle={aab}",
             f"--output={apks}", f"--device-id={serial()}"],
            timeout=900,
        )
        if rc != 0:
            log("build-apks --device-id failed, retrying with --connected-device")
            rc, out = run(
                [java, "-jar", str(jar), "build-apks", f"--bundle={aab}",
                 f"--output={apks}", "--connected-device"],
                timeout=900,
            )
        if rc != 0:
            raise RunError("bundletool build-apks failed:\n" + out[-1200:])
        rc, out = run(
            [java, "-jar", str(jar), "install-apks", f"--apks={apks}",
             f"--device-id={serial()}"],
            timeout=900,
        )
        if rc != 0 and TRANSIENT_ADB_RE.search(out):
            log("bundletool hit a transient adb/emulator error - reconnecting and retrying once", "warn")
            reset_adb_server()
            time.sleep(5)
            rc, out = run(
                [java, "-jar", str(jar), "install-apks", f"--apks={apks}",
                 f"--device-id={serial()}"],
                timeout=900,
            )
        if rc != 0:
            raise RunError("bundletool install-apks failed:\n" + out[-1200:])
        log("AAB installed")

    elif mode == "play":
        rc, out = adb("shell", "pm", "path", "com.android.vending", timeout=20)
        if "package:" not in out:
            raise RunError("this device image has no Play Store - use a device with Play Store (play_store: true)")
        url = install.get("play_url") or ""
        if not url:
            raise RunError("no Play internal-testing link configured")
        already = False
        if pkg:
            rc, out = adb("shell", "pm", "path", pkg, timeout=20)
            already = "package:" in out
        if already:
            # Already installed (for example from an earlier Play-link run or a
            # manual install). Skip the store page and go straight to launch.
            log(f"{pkg} is already installed - skipping the Play Store link")
        else:
            log("opening Play link inside the emulator")
            adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, timeout=45)
            deadline = time.time() + int(install.get("play_wait_seconds", 300))
            while time.time() < deadline:
                check_stop()
                if pkg:
                    rc, out = adb("shell", "pm", "path", pkg, timeout=20)
                    if "package:" in out:
                        break
                else:
                    fresh = third_party_packages() - before
                    if len(fresh) == 1:
                        pkg = next(iter(fresh))
                        job["package_name"] = pkg
                        log(f"detected newly installed app: {pkg}")
                        break
                    if len(fresh) > 1:
                        raise RunError(
                            "multiple new apps appeared after opening the Play link ("
                            + ", ".join(sorted(fresh))
                            + "). Set the package name in portal settings."
                        )
                time.sleep(6)
            else:
                raise RunError(
                    "app did not get installed from the Play link within the timeout. "
                    "Check: Google account signed in on this AVD? Tester opted in / link valid?"
                )

    elif mode == "installed":
        # Manual workflow: the app was installed once on each AVD (Play Store,
        # internal-test link, or a manual adb install). Just verify it is there.
        if not pkg:
            pkg = detect_single_package(before)
            job["package_name"] = pkg
            log(f"auto-detected installed app: {pkg}")
        rc, out = adb("shell", "pm", "path", pkg, timeout=20)
        if "package:" not in out:
            raise RunError(
                f"package {pkg!r} is not installed on this AVD. Install it once (Play link or adb), "
                "or switch Install mode to Play internal-test link so the portal installs it."
            )
        log(f"{pkg} is installed - launching it")

    else:
        raise RunError(f"unknown install mode: {mode}")

    after = third_party_packages()
    new_pkgs = sorted(after - before)
    if pkg and pkg not in after:
        # `pm list packages -3` can lag for a moment right after install.
        rc, out = adb("shell", "pm", "path", pkg, timeout=20)
        if "package:" not in out:
            raise RunError(
                f"package {pkg!r} is not installed on the emulator after the upload. "
                f"New package(s): {new_pkgs or 'none'}. Check the package name / build artifact."
            )
    if not pkg and len(new_pkgs) == 1:
        pkg = new_pkgs[0]
        job["package_name"] = pkg
        log(f"detected package from install: {pkg}")
    elif not pkg and new_pkgs:
        log(f"multiple new packages: {new_pkgs} - set the package name in portal settings")
    job["_install_ok"] = True


def step_launch(job: dict) -> None:
    check_stop()
    pkg = (job.get("package_name") or "").strip()
    if not pkg:
        raise RunError("package name not set - add it in portal settings (or it could not be auto-detected)")
    adb("logcat", "-c", timeout=30)
    rc, out = adb("shell", "cmd", "package", "resolve-activity", "--brief", pkg, timeout=45)
    component = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if "/" in line and "package:" not in line and "=" not in line:
            component = line
            break
    launch_t0 = time.time()
    if component:
        rc, out = adb("shell", "am", "start", "-W", "-n", component, timeout=120)
        log(f"am start -> {component}: {out.strip().splitlines()[-1] if out.strip() else 'no output'}")
        launch_ok = rc == 0 and "Error" not in out
    else:
        rc, out = adb("shell", "monkey", "-p", pkg, "-c",
                      "android.intent.category.LAUNCHER", "1", timeout=120)
        log("launched via monkey (launcher activity could not be resolved)")
        launch_ok = rc == 0 and "No activities found" not in out
    job["_launch_epoch"] = launch_t0
    job["_launch_ok"] = launch_ok
    if not launch_ok:
        raise RunError(f"could not launch {pkg} - check the package name ({out.strip()[-400:]})")


CRASH_RE = re.compile(r"FATAL EXCEPTION|beginning of crash|E AndroidRuntime|\bANR in\b")


def step_observe(job: dict, shotter: ScreenshotLoop) -> dict:
    observe = job.get("observe") or {}
    settle = float(observe.get("settle_seconds", 8))
    offsets = []
    for part in str(observe.get("screenshot_offsets", "2,8,20")).replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                offsets.append(float(part))
            except ValueError:
                pass
    if not offsets:
        offsets = [2.0, 8.0, 20.0]

    launch_t0 = job.get("_launch_epoch", time.time())
    shots_dir = Path(job["_run_dir"]) / "screenshots"
    for off in offsets:
        shotter.schedule(launch_t0 + off, f"shot_{int(off)}s", shots_dir)

    deadline = launch_t0 + max(offsets) + 8
    while time.time() < deadline:
        check_stop()
        time.sleep(1)
    if settle > 0:
        while time.time() < launch_t0 + settle:
            check_stop()
            time.sleep(1)

    job["_screenshots"] = list(shotter.taken)

    # ---- evidence
    rc, crash_buf = adb("logcat", "-b", "crash", "-d", timeout=90)
    rc2, main_buf = adb("logcat", "-d", "-v", "threadtime", timeout=120)
    full_log = (crash_buf or "") + "\n" + (main_buf or "")
    try:
        (Path(job["_run_dir"]) / "logcat.txt").write_text(full_log[-400_000:], encoding="utf-8", errors="replace")
    except OSError:
        pass

    pkg = job["package_name"]
    crashes = []
    for line in (crash_buf + "\n" + main_buf).splitlines():
        if CRASH_RE.search(line):
            if "ANR in" in line and pkg and pkg not in line:
                continue
            crashes.append(line.strip()[:400])
    job["_crashes"] = crashes[:50]

    rc, out = adb("shell", "pidof", pkg, timeout=30)
    alive = bool(out.strip())
    job["_process_alive"] = alive

    rc, mem = adb("shell", "dumpsys", "meminfo", pkg, timeout=60)
    job["_meminfo_tail"] = "\n".join(mem.splitlines()[:40])
    try:
        (Path(job["_run_dir"]) / "meminfo.txt").write_text(mem[:60_000], encoding="utf-8", errors="replace")
    except OSError:
        pass

    # ---- device info snapshot
    def prop(name: str) -> str:
        rc, out = adb("shell", "getprop", name, timeout=20)
        return out.strip()

    info = {
        "model": prop("ro.product.model"),
        "manufacturer": prop("ro.product.manufacturer"),
        "android_release": prop("ro.build.version.release"),
        "sdk": prop("ro.build.version.sdk"),
        "abi": prop("ro.product.cpu.abi"),
        "fingerprint": prop("ro.build.fingerprint"),
    }
    rc, wm_size = adb("shell", "wm", "size", timeout=20)
    rc, wm_density = adb("shell", "wm", "density", timeout=20)
    info["wm_size"] = wm_size.strip()
    info["wm_density"] = wm_density.strip()
    job["_device_info"] = info
    try:
        (Path(job["_run_dir"]) / "device.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass

    # ---- verdict
    status = "pass"
    reasons: list[str] = []
    if job["_crashes"]:
        status = "fail"
        reasons.append(f"{len(job['_crashes'])} crash/ANR log line(s) detected")
    if not alive:
        status = "fail"
        reasons.append("app process was not running at the end of the observation window")
    if not reasons:
        reasons.append("app installed, launched and stayed alive with no crashes")

    result = base_result(job, status, reasons)
    return result


def step_close_app(job: dict) -> None:
    """Close the tested app before the emulator is shut down.

    Auto-run sweeps should leave each device clean: test, close, next device.
    This also gives a deterministic end state when auto-stop is disabled.
    """
    pkg = (job.get("package_name") or "").strip()
    if not pkg:
        return
    check_stop()
    log(f"closing {pkg}")
    rc, out = adb("shell", "am", "force-stop", pkg, timeout=45)
    if rc != 0:
        log(f"could not force-stop {pkg}: {out.strip()[:300]}", "warn")


def step_collect(job: dict, result: dict) -> None:
    check_stop()
    try:
        (Path(job["_run_dir"]) / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log(f"could not write result.json: {exc}", "warn")


def step_shutdown(avd: str | None = None) -> None:
    proc = None
    with STATE.lock:
        proc = STATE.emulator_proc
    if adb_exe().exists():
        rc, out = run([str(adb_exe()), "devices"], timeout=30)
        if serial() in out:
            log("shutting down emulator (adb emu kill)")
            adb("emu", "kill", timeout=20)
            # Wait for the emulator process itself, not just for adb to drop
            # the device. Quick-boot snapshot saving takes a few seconds and
            # force-killing at that point corrupts the snapshot / leaves locks.
            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(1)
                if proc is not None and proc.poll() is not None:
                    break
                rc, out = run([str(adb_exe()), "devices"], timeout=30)
                if serial() not in out and proc is None:
                    break
    if proc is not None and proc.poll() is None:
        log("emulator did not exit after graceful shutdown - killing process tree", "warn")
        # emulator.exe owns a qemu child, and an orphaned qemu is what keeps
        # the AVD lock (and port 5554) alive.
        stop_emulator_proc(proc)
    kill_port_holders()
    if avd:
        clean_avd_locks(avd)
    with STATE.lock:
        STATE.emulator_proc = None
        if STATE.emulator_log_file is not None:
            try:
                STATE.emulator_log_file.close()
            except OSError:
                pass
        STATE.emulator_log_file = None


def release_emulator() -> None:
    """Drop our handle on a still-running emulator without stopping it."""
    with STATE.lock:
        STATE.emulator_proc = None
        if STATE.emulator_log_file is not None:
            try:
                STATE.emulator_log_file.close()
            except OSError:
                pass
        STATE.emulator_log_file = None


def run_job(job: dict) -> None:
    global _run_log_path
    started_epoch = time.time()
    auto_stop = bool(job.get("auto_stop", True))
    job["_started_epoch"] = started_epoch
    job["_started_at"] = now_iso()
    run_dir = Path(CFG["data_root"]) / "runs" / str(job.get("run_id") or f"run_{int(started_epoch)}")
    run_dir.mkdir(parents=True, exist_ok=True)
    job["_run_dir"] = str(run_dir)
    _run_log_path = run_dir / "run.log"

    with STATE.lock:
        STATE.state = "running"
        STATE.run_id = job.get("run_id")
        STATE.device = job.get("device")
        STATE.started_at = now_iso()
        STATE.finished_at = None
        STATE.result = None
        STATE.log = []
        STATE.stop_requested = False
        STATE.run_dir = str(run_dir)
        STATE.latest_shot_png = None
        STATE.latest_shot_ts = 0.0

    avd = (job.get("device") or {}).get("avd")
    shotter = ScreenshotLoop()
    result: dict | None = None

    try:
        set_step("preflight")
        step_preflight(job)
        set_step("boot_device")
        step_boot(job)
        # Start grabbing frames only after boot: polling screencap during boot
        # competes with the boot process and can make slow devices much slower.
        shotter.start()
        set_step("install_app")
        step_install(job)
        set_step("launch_app")
        step_launch(job)
        set_step("observe")
        result = step_observe(job, shotter)
        set_step("close_app")
        step_close_app(job)
        set_step("collect")
        step_collect(job, result)
        set_step("shutdown_device")
        if auto_stop:
            step_shutdown(avd)
        else:
            release_emulator()
            log("auto-stop disabled - emulator left running")
        set_step("save_result")
    except RunStopped:
        log("run stopped by user")
        result = base_result(job, "stopped", ["stopped by user"])
    except RunError as exc:
        with STATE.lock:
            stopped_by_user = STATE.stop_requested
        if stopped_by_user:
            log("run stopped by user (emulator terminated)")
            result = base_result(job, "stopped", ["stopped by user"])
        else:
            log(f"RUN ERROR: {exc}", "error")
            result = base_result(job, "error", [str(exc)])
    except Exception as exc:  # noqa: BLE001
        log(f"unexpected error: {exc}\n{traceback.format_exc()}", "error")
        result = base_result(job, "error", [f"unexpected: {exc}"])
    finally:
        with STATE.lock:
            stopped_by_user = STATE.stop_requested
        if auto_stop or stopped_by_user:
            try:
                step_shutdown(avd)
            except Exception:  # noqa: BLE001
                pass
            if avd and job.get("_discard_snapshot_after_shutdown"):
                clean_quick_boot_snapshot(avd)
        elif STATE.emulator_proc is not None or STATE.emulator_log_file is not None:
            release_emulator()
            log("auto-stop disabled - emulator left running")
        shotter.stop()
        try:
            (run_dir / "result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass
        with STATE.lock:
            STATE.result = result
            STATE.finished_at = now_iso()
            st = (result or {}).get("status")
            STATE.state = "stopped" if st == "stopped" else ("done" if st in ("pass", "fail") else "error")
            STATE.emulator_proc = None
        _run_log_path = None


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
def status_payload() -> dict:
    with STATE.lock:
        return {
            "state": STATE.state,
            "step": STATE.step,
            "step_index": STATE.step_index,
            "total_steps": len(STEPS),
            "run_id": STATE.run_id,
            "device": STATE.device,
            "started_at": STATE.started_at,
            "finished_at": STATE.finished_at,
            "log": list(STATE.log[-150:]),
            "result": STATE.result,
            "run_dir": STATE.run_dir,
            "latest_shot_ts": STATE.latest_shot_ts,
        }


def health_payload() -> dict:
    data_root = Path(CFG["data_root"])
    try:
        free_disk = round(shutil.disk_usage(data_root.anchor or "C:\\").free / (1024**3), 1)
    except OSError:
        free_disk = -1
    return {
        "ok": True,
        "agent": "android-device-portal-agent",
        "version": AGENT_VERSION,
        "sdk_root": str(sdk_root()),
        "emulator_found": emulator_exe().exists(),
        "adb_found": adb_exe().exists(),
        "cmdline_tools_found": (sdk_root() / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat").exists(),
        "java": find_java(),
        "bundletool": Path(CFG.get("bundletool_jar", "")).exists(),
        "avds": safe_list_avds(),
        "free_ram_mb": free_ram_mb(),
        "free_disk_gb": free_disk,
        "state": STATE.state,
        "step": STATE.step,
        "run_id": STATE.run_id,
        "device": STATE.device,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"ADPAgent/{AGENT_VERSION}"

    def log_message(self, *args) -> None:  # silence console noise
        pass

    # -- helpers -----------------------------------------------------------
    def _auth(self) -> bool:
        if self.headers.get("X-Agent-Token", "") != str(CFG.get("token")):
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))
        except (ValueError, OSError):
            return {}

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._auth():
            return
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, health_payload())
        elif path == "/avds":
            self._json(200, {"avds": safe_list_avds()})
        elif path == "/status":
            self._json(200, status_payload())
        elif path == "/screenshot":
            with STATE.lock:
                png = STATE.latest_shot_png
            if not png:
                self._json(404, {"error": "no screenshot yet"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
        else:
            self._json(404, {"error": "not found"})

    # -- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._auth():
            return
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/run":
            with STATE.lock:
                if STATE.state == "running":
                    self._json(409, {"error": "agent is busy with another run", "run_id": STATE.run_id})
                    return
                STATE.reset()
                STATE.state = "running"  # claim the slot before the thread starts
                STATE.run_id = body.get("run_id")
                STATE.device = body.get("device")
            if not (body.get("device") or {}).get("avd"):
                with STATE.lock:
                    STATE.reset()
                self._json(400, {"error": "device.avd is required"})
                return
            threading.Thread(target=run_job, args=(body,), daemon=True).start()
            self._json(200, {"ok": True, "run_id": body.get("run_id")})

        elif path == "/stop":
            with STATE.lock:
                running = STATE.state == "running"
                STATE.stop_requested = True
                proc = STATE.emulator_proc
            # Kill the emulator immediately instead of waiting for a long adb
            # install/logcat call to return. The worker marks the run as stopped.
            if running and proc is not None:
                threading.Thread(target=stop_emulator_proc, args=(proc,), daemon=True).start()
            self._json(200, {"ok": True, "was_running": running})

        elif path == "/wipe":
            avd = body.get("avd")
            if not avd:
                self._json(400, {"error": "avd is required"})
                return
            STATE.wipe_next[avd] = True
            log(f"AVD {avd} will be wiped on next boot")
            self._json(200, {"ok": True, "avd": avd})

        elif path == "/kill-emulator":
            kill_stray_emulator()
            with STATE.lock:
                device = STATE.device or {}
            avd = device.get("avd")
            if avd:
                clean_avd_locks(avd)
            self._json(200, {"ok": True, "avd": avd})

        else:
            self._json(404, {"error": "not found"})


def main() -> None:
    load_config()
    print(f"Android Device Portal agent v{AGENT_VERSION}")
    print(f"  listening on 0.0.0.0:{CFG['port']}")
    print(f"  SDK root:    {CFG['sdk_root']}")
    print(f"  data root:   {CFG['data_root']}")
    print(f"  emulator:    {emulator_exe()}")
    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(CFG["port"])), Handler)
    except OSError as exc:
        print(f"FATAL: cannot bind port {CFG['port']}: {exc}")
        sys.exit(1)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
