"""
Camera API - HTTP endpoints for recording control.
Communicates with recorder process via file-based commands.
"""

import json
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from config import get_config
from logger import log_info


def _check_ntp_sync() -> bool:
    """
    Check if system clock is properly NTP synchronized.

    Returns False if:
    - NTP not synchronized
    - Stratum > 10 (synced to local fallback, not real NTP)
    """
    try:
        # Check if NTP is synchronized
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.stdout.strip().lower() != "yes":
            return False

        # Check stratum - if > 10, we're synced to a local fallback
        result = subprocess.run(
            ["/usr/bin/chronyc", "tracking"],
            capture_output=True,
            text=True,
            timeout=2
        )
        for line in result.stdout.splitlines():
            if line.startswith("Stratum"):
                stratum = int(line.split(":")[1].strip())
                if stratum > 10:
                    return False  # Synced to local fallback, not real NTP
                break

        return True
    except Exception as e:
        # Log error for debugging
        import sys
        print(f"NTP check error: {e}", file=sys.stderr)
        return False

app = FastAPI(title="Camera Node API", version="2.0.0")

# Paths for IPC with recorder/uploader processes
CMD_DIR = Path("/data/cam/cmd")
RECORDER_STATE = Path("/data/cam/state.json")
UPLOADER_STATE = Path("/data/cam/uploader_state.json")
UPLOAD_PAUSE_FILE = Path("/data/cam/upload_paused")


def _read_json(path: Path) -> dict:
    """Read JSON file, return empty dict if missing."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _send_command(cmd: str):
    """Send command to recorder by creating command file."""
    CMD_DIR.mkdir(parents=True, exist_ok=True)
    (CMD_DIR / cmd).touch()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/preflight")
async def preflight():
    """Check if camera is ready for recording."""
    config = get_config()
    recorder_state = _read_json(RECORDER_STATE)

    camera_ok = recorder_state.get("camera_available", False)
    is_idle = not recorder_state.get("recording", False)
    ntp_synced = _check_ntp_sync()

    # Check disk space
    try:
        disk = shutil.disk_usage(str(config.recording.recordings_path))
        disk_free_gb = disk.free / (1024**3)
        disk_ok = disk_free_gb > 1.0
    except Exception:
        disk_free_gb = 0
        disk_ok = False

    return {
        "success": True,
        "data": {
            "ready": camera_ok and is_idle and disk_ok and ntp_synced,
            "camera": camera_ok,
            "idle": is_idle,
            "ntp_synced": ntp_synced,
            "disk_ok": disk_ok,
            "disk_free_gb": round(disk_free_gb, 2),
            "node": config.node.name,
        },
    }


@app.get("/status")
async def status():
    """Get current camera status."""
    config = get_config()
    recorder_state = _read_json(RECORDER_STATE)
    uploader_state = _read_json(UPLOADER_STATE)

    # Disk space
    try:
        disk = shutil.disk_usage(str(config.recording.recordings_path))
        disk_free_gb = disk.free / (1024**3)
    except Exception:
        disk_free_gb = 0

    return {
        "success": True,
        "data": {
            "state": "recording" if recorder_state.get("recording") else "idle",
            "uuid": recorder_state.get("uuid"),
            "segment": recorder_state.get("segment", 0),
            "duration": recorder_state.get("duration", 0),
            "camera_available": recorder_state.get("camera_available", False),
            "ntp_synced": _check_ntp_sync(),
            "pending_uploads": uploader_state.get("pending", 0),
            "uploading": uploader_state.get("uploading"),
            "disk_free_gb": round(disk_free_gb, 2),
            "node": config.node.name,
        },
    }


@app.post("/record/start")
async def start_recording(
    uuid: str = Query(..., description="Session UUID"),
    start_at: int = Query(None, description="Synchronized start time (Unix ms)"),
):
    """Start recording session."""
    recorder_state = _read_json(RECORDER_STATE)

    if recorder_state.get("recording"):
        raise HTTPException(status_code=409, detail="Already recording")

    # Send start command with optional start_at
    # Format: start:{uuid} or start:{uuid}:{start_at}
    if start_at:
        _send_command(f"start:{uuid}:{start_at}")
    else:
        _send_command(f"start:{uuid}")

    log_info("api", f"Recording start command sent", uuid=uuid, start_at=start_at)
    return {"success": True, "uuid": uuid, "start_at": start_at}


@app.post("/record/stop")
async def stop_recording():
    """Stop current recording session."""
    recorder_state = _read_json(RECORDER_STATE)

    if not recorder_state.get("recording"):
        raise HTTPException(status_code=409, detail="Not recording")

    uuid = recorder_state.get("uuid")

    # Send stop command
    _send_command("stop")

    log_info("api", f"Recording stop command sent", uuid=uuid)
    return {
        "success": True,
        "uuid": uuid,
        "duration": recorder_state.get("duration", 0),
        "segments": recorder_state.get("segment", 0),
    }


@app.post("/upload/pause")
async def pause_uploads():
    """
    Pause segment uploads (for safe HDD unmount).
    Creates a flag file that the uploader checks.
    """
    UPLOAD_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_PAUSE_FILE.touch()
    log_info("api", "Upload paused")
    return {"success": True, "paused": True}


@app.post("/upload/resume")
async def resume_uploads():
    """
    Resume segment uploads (after HDD remount).
    Removes the pause flag file.
    """
    try:
        if UPLOAD_PAUSE_FILE.exists():
            UPLOAD_PAUSE_FILE.unlink()
    except Exception:
        pass
    log_info("api", "Upload resumed")
    return {"success": True, "paused": False}


@app.get("/upload/status")
async def upload_status():
    """Get upload status including pause state."""
    uploader_state = _read_json(UPLOADER_STATE)
    return {
        "success": True,
        "data": {
            "paused": UPLOAD_PAUSE_FILE.exists(),
            "pending": uploader_state.get("pending", 0),
            "uploading": uploader_state.get("uploading"),
            "progress": uploader_state.get("progress"),
        },
    }


@app.post("/shutdown")
async def shutdown():
    """
    Safely shut down this camera node.
    Triggers shutdown in background and returns immediately.
    """
    import subprocess
    log_info("api", "Shutdown requested")
    # Run shutdown in background - returns before shutdown completes
    subprocess.Popen(["sudo", "shutdown", "-h", "now"])
    return {"success": True, "message": "Shutting down"}
