"""
Segment storage module for controller.
Receives and stores video segments from cameras.
Handles external HDD storage with mount verification.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import AsyncGenerator, Optional, Tuple

from config import get_config
from state import get_state
from logger import log_info, log_error, log_warning
from database import get_db


# Mount points for the two partitions
STORAGE_MOUNT = Path("/mnt/storage")  # ext4 - active storage
EXPORT_MOUNT = Path("/mnt/export")    # exFAT - Mac-readable export


def run_command(cmd: list, timeout: int = 10) -> Tuple[bool, str]:
    """Run a shell command and return (success, output)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, str(e)


def is_mounted(path: Path) -> bool:
    """Check if a path is a mount point."""
    try:
        return path.is_mount()
    except Exception:
        return False


def mount_storage() -> Tuple[bool, str]:
    """
    Mount both storage partitions.
    Returns (success, message).
    """
    results = []

    for mount_point in [STORAGE_MOUNT, EXPORT_MOUNT]:
        if is_mounted(mount_point):
            results.append(f"{mount_point.name}: already mounted")
            continue

        success, output = run_command(["sudo", "mount", str(mount_point)])
        if success:
            results.append(f"{mount_point.name}: mounted")
            log_info("storage", f"Mounted {mount_point}")
        else:
            log_error("storage", f"Failed to mount {mount_point}: {output}")
            return False, f"Failed to mount {mount_point.name}: {output}"

    return True, "; ".join(results)


def unmount_storage() -> Tuple[bool, str]:
    """
    Safely unmount both storage partitions.
    Syncs first, then unmounts.
    Returns (success, message).
    """
    # First sync to flush buffers
    log_info("storage", "Syncing filesystems before unmount...")
    run_command(["sync"])

    results = []

    # Unmount in reverse order (export first, then storage)
    for mount_point in [EXPORT_MOUNT, STORAGE_MOUNT]:
        if not is_mounted(mount_point):
            results.append(f"{mount_point.name}: not mounted")
            continue

        success, output = run_command(["sudo", "umount", str(mount_point)])
        if success:
            results.append(f"{mount_point.name}: unmounted")
            log_info("storage", f"Unmounted {mount_point}")
        else:
            # Check if busy
            if "busy" in output.lower() or "target is busy" in output.lower():
                log_error("storage", f"Cannot unmount {mount_point}: device busy")
                return False, f"{mount_point.name} is busy - close any open files first"
            log_error("storage", f"Failed to unmount {mount_point}: {output}")
            return False, f"Failed to unmount {mount_point.name}: {output}"

    return True, "; ".join(results)


def get_full_storage_status() -> dict:
    """
    Get comprehensive storage status for both partitions.
    Used by the iOS app.
    """
    storage_mounted = is_mounted(STORAGE_MOUNT)
    export_mounted = is_mounted(EXPORT_MOUNT)

    status = {
        "storage": {
            "path": str(STORAGE_MOUNT),
            "mounted": storage_mounted,
        },
        "export": {
            "path": str(EXPORT_MOUNT),
            "mounted": export_mounted,
        },
        "all_mounted": storage_mounted and export_mounted,
        "safe_to_eject": False,
    }

    # Add disk usage for mounted partitions
    if storage_mounted:
        usage = get_disk_usage(STORAGE_MOUNT)
        if usage:
            status["storage"].update(usage)

    if export_mounted:
        usage = get_disk_usage(EXPORT_MOUNT)
        if usage:
            status["export"].update(usage)

    # Safe to eject if mounted and no active uploads
    state = get_state()
    active_uploads = state.get_active_uploads() if hasattr(state, 'get_active_uploads') else []
    status["active_uploads"] = len(active_uploads)
    status["safe_to_eject"] = status["all_mounted"] and len(active_uploads) == 0

    return status


def is_mount_ready(path: Path) -> bool:
    """
    Check if storage path is ready (mounted and writable).
    For external HDD, verifies the mount is present.
    """
    try:
        # First check if the storage mount point is mounted
        if not is_mounted(STORAGE_MOUNT):
            return False

        # Check path exists (create if needed)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)

        # Verify writable
        test_file = path / ".write_test"
        test_file.touch()
        test_file.unlink()
        return True
    except Exception:
        return False


def get_disk_usage(path: Path) -> Optional[dict]:
    """Get disk usage info for the storage path."""
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "percent_used": round((usage.used / usage.total) * 100, 1),
        }
    except Exception:
        return None


def check_storage_space(path: Path, required_bytes: int) -> bool:
    """Check if enough space is available."""
    try:
        usage = shutil.disk_usage(path)
        # Leave at least 1GB buffer
        min_free = 1024 * 1024 * 1024
        return usage.free > (required_bytes + min_free)
    except Exception:
        return False


async def receive_segment(
    camera: str,
    uuid: str,
    filename: str,
    content_length: int,
    body_stream: AsyncGenerator[bytes, None],
) -> bool:
    """
    Receive a segment upload from a camera.

    Writes to .tmp first, then renames atomically on completion.
    Tracks progress in state for iOS visualization.
    Verifies external HDD mount before writing.

    Returns True on success.
    """
    config = get_config()
    state = get_state()
    db = get_db()

    recordings_path = config.storage.recordings_path

    # Check for duplicate (already received this segment)
    if db.segment_exists(uuid, camera, filename):
        log_info(
            "storage",
            f"Segment already received, skipping: {filename}",
            camera=camera,
            uuid=uuid,
        )
        return True  # Return success - segment already stored

    # Verify storage is ready (important for external HDD)
    if not is_mount_ready(recordings_path):
        log_error(
            "storage",
            "Storage not ready - drive may be unmounted",
            path=str(recordings_path),
            camera=camera,
        )
        return False

    # Check disk space
    if not check_storage_space(recordings_path, content_length):
        log_error(
            "storage",
            "Insufficient disk space",
            camera=camera,
            filename=filename,
            required=content_length,
        )
        return False

    # Create directory structure: /data/recordings/{uuid}/{camera}/
    segment_dir = recordings_path / uuid / camera
    segment_dir.mkdir(parents=True, exist_ok=True)

    temp_path = segment_dir / f"{filename}{config.storage.temp_suffix}"
    final_path = segment_dir / filename

    # Start tracking upload progress
    upload_key = state.start_upload(camera, uuid, filename, content_length)

    try:
        received = 0

        with open(temp_path, "wb") as f:
            async for chunk in body_stream:
                f.write(chunk)
                received += len(chunk)
                state.update_upload(upload_key, received)

        # Verify we got everything
        if received != content_length:
            log_error(
                "storage",
                f"Size mismatch: expected {content_length}, got {received}",
                camera=camera,
                filename=filename,
            )
            temp_path.unlink(missing_ok=True)
            return False

        # Atomic rename
        temp_path.rename(final_path)

        # Mark upload complete in state
        state.finish_upload(upload_key)

        # Record in database for persistence
        db.insert_segment(uuid, camera, filename, received)

        log_info(
            "storage",
            f"Received segment: {filename}",
            camera=camera,
            uuid=uuid,
            size=received,
        )

        return True

    except Exception as e:
        log_error(
            "storage",
            f"Failed to receive segment: {e}",
            camera=camera,
            filename=filename,
        )
        temp_path.unlink(missing_ok=True)
        return False


def get_session_info(uuid: str) -> dict:
    """Get storage info for a session."""
    config = get_config()
    state = get_state()

    session_dir = config.storage.recordings_path / uuid

    if not session_dir.exists():
        return {"uuid": uuid, "exists": False, "cameras": {}}

    cameras = {}
    total_size = 0

    for camera_dir in session_dir.iterdir():
        if camera_dir.is_dir():
            segments = list(camera_dir.glob("*.h264"))
            size = sum(s.stat().st_size for s in segments)
            cameras[camera_dir.name] = {
                "segments": len(segments),
                "size_mb": round(size / (1024 * 1024), 2),
            }
            total_size += size

    return {
        "uuid": uuid,
        "exists": True,
        "cameras": cameras,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
    }


def list_sessions() -> list:
    """List all stored sessions."""
    config = get_config()

    recordings_dir = config.storage.recordings_path
    if not recordings_dir.exists():
        return []

    sessions = []
    for session_dir in recordings_dir.iterdir():
        if session_dir.is_dir():
            sessions.append(get_session_info(session_dir.name))

    return sessions


def get_storage_status() -> dict:
    """Get storage health status (for monitoring external HDD)."""
    config = get_config()
    path = config.storage.recordings_path

    mounted = is_mount_ready(path)
    usage = get_disk_usage(path) if mounted else None

    status = {
        "path": str(path),
        "mounted": mounted,
        "is_external": path.is_mount() if path.exists() else False,
    }

    if usage:
        status.update(usage)
        # Warning thresholds
        status["low_space"] = usage["percent_used"] > 90
        status["critical_space"] = usage["percent_used"] > 95
    else:
        status["error"] = "Unable to read disk usage"

    return status


def export_session(uuid: str) -> Tuple[bool, str]:
    """
    Copy a session from storage to export partition for Mac access.
    Returns (success, message).
    """
    config = get_config()

    source = config.storage.recordings_path / uuid
    dest = EXPORT_MOUNT / uuid

    if not source.exists():
        return False, f"Session {uuid} not found"

    if not is_mounted(EXPORT_MOUNT):
        return False, "Export partition not mounted"

    # Check if already exported
    if dest.exists():
        return False, f"Session {uuid} already exists on export partition"

    try:
        # Get session size for space check
        session_size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

        if not check_storage_space(EXPORT_MOUNT, session_size):
            return False, "Insufficient space on export partition"

        # Copy session
        log_info("storage", f"Exporting session {uuid} to export partition...")
        shutil.copytree(source, dest)

        # Verify copy
        copied_size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        if copied_size != session_size:
            log_error("storage", f"Export size mismatch: {session_size} vs {copied_size}")
            shutil.rmtree(dest, ignore_errors=True)
            return False, "Export verification failed"

        # Mark as exported in database
        db = get_db()
        db.mark_exported(uuid)

        log_info("storage", f"Exported session {uuid} ({session_size / (1024*1024):.1f} MB)")
        return True, f"Exported {uuid} ({session_size / (1024*1024):.1f} MB)"

    except Exception as e:
        log_error("storage", f"Export failed: {e}")
        shutil.rmtree(dest, ignore_errors=True)
        return False, str(e)


def list_exported_sessions() -> list:
    """List sessions available on export partition."""
    if not is_mounted(EXPORT_MOUNT):
        return []

    sessions = []
    for session_dir in EXPORT_MOUNT.iterdir():
        if session_dir.is_dir():
            size = sum(f.stat().st_size for f in session_dir.rglob("*") if f.is_file())
            sessions.append({
                "uuid": session_dir.name,
                "size_mb": round(size / (1024 * 1024), 2),
            })

    return sessions


def delete_exported_session(uuid: str) -> Tuple[bool, str]:
    """Delete a session from export partition."""
    if not is_mounted(EXPORT_MOUNT):
        return False, "Export partition not mounted"

    dest = EXPORT_MOUNT / uuid
    if not dest.exists():
        return False, f"Session {uuid} not found on export partition"

    try:
        shutil.rmtree(dest)
        log_info("storage", f"Deleted exported session {uuid}")
        return True, f"Deleted {uuid} from export partition"
    except Exception as e:
        log_error("storage", f"Failed to delete exported session: {e}")
        return False, str(e)


async def receive_phone_data(
    uuid: str,
    filename: str,
    content_length: int,
    body_stream: AsyncGenerator[bytes, None],
) -> bool:
    """Receive phone data file (motion, location, etc.) for a session."""
    config = get_config()
    db = get_db()

    # Store in session_dir/phone/
    data_dir = config.storage.recordings_path / uuid / "phone"
    data_dir.mkdir(parents=True, exist_ok=True)

    temp_path = data_dir / f"{filename}.tmp"
    final_path = data_dir / filename

    try:
        received = 0
        with open(temp_path, "wb") as f:
            async for chunk in body_stream:
                f.write(chunk)
                received += len(chunk)

        if received != content_length:
            temp_path.unlink(missing_ok=True)
            return False

        temp_path.rename(final_path)
        db.insert_phone_data(uuid, filename, received)
        log_info("storage", f"Received phone data: {filename}", uuid=uuid, size=received)
        return True

    except Exception as e:
        log_error("storage", f"Failed to receive phone data: {e}")
        temp_path.unlink(missing_ok=True)
        return False


async def receive_watch_data(
    uuid: str,
    filename: str,
    content_length: int,
    body_stream: AsyncGenerator[bytes, None],
) -> bool:
    """Receive watch data file (heart rate, motion, etc.) for a session."""
    config = get_config()
    db = get_db()

    # Store in session_dir/watch/
    data_dir = config.storage.recordings_path / uuid / "watch"
    data_dir.mkdir(parents=True, exist_ok=True)

    temp_path = data_dir / f"{filename}.tmp"
    final_path = data_dir / filename

    try:
        received = 0
        with open(temp_path, "wb") as f:
            async for chunk in body_stream:
                f.write(chunk)
                received += len(chunk)

        if received != content_length:
            temp_path.unlink(missing_ok=True)
            return False

        temp_path.rename(final_path)
        db.insert_watch_data(uuid, filename, received)
        log_info("storage", f"Received watch data: {filename}", uuid=uuid, size=received)
        return True

    except Exception as e:
        log_error("storage", f"Failed to receive watch data: {e}")
        temp_path.unlink(missing_ok=True)
        return False
