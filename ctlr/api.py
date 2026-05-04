"""
Controller API - HTTP endpoints for camera orchestration and iOS app.

All endpoints use /api/ prefix for consistency.
"""

from fastapi import FastAPI, HTTPException, Request, Query, WebSocket
from fastapi.responses import JSONResponse

from config import get_config
from state import get_state
from storage import (
    receive_segment, get_session_info, list_sessions, get_storage_status,
    get_full_storage_status, mount_storage, unmount_storage, is_mounted, STORAGE_MOUNT,
    export_session, list_exported_sessions, delete_exported_session
)
from orchestrator import get_orchestrator
from logger import log_info, log_error, log_ios
from websocket import (
    websocket_endpoint, broadcast_storage, broadcast_sync_progress,
    broadcast_phone_sync, broadcast_watch_sync
)
from database import get_db
from can_listener import get_can_listener

app = FastAPI(title="Controller API", version="2.0.0")


# Health check (no /api prefix - standard convention)


@app.get("/health")
async def health():
    return {"status": "ok"}


# WebSocket endpoint for iOS real-time updates


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    """WebSocket endpoint for iOS app real-time status updates."""
    await websocket_endpoint(websocket)


# Status and Cameras


@app.get("/api/status")
async def status():
    """Get controller status (for iOS app)."""
    state = get_state()
    config = get_config()

    return {
        "success": True,
        "data": {
            **state.to_dict(),
            "controller": config.node.name,
        },
    }


@app.get("/api/cameras")
async def cameras():
    """Get all camera statuses."""
    state = get_state()

    return {
        "success": True,
        "data": {name: cam.to_dict() for name, cam in state.cameras.items()},
    }


@app.get("/api/cameras/{camera_name}")
async def camera_status(camera_name: str):
    """Get detailed status for a specific camera."""
    orchestrator = get_orchestrator()
    status = await orchestrator.get_camera_status(camera_name)

    if status is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    return {"success": True, "data": status}


# Recording Control


@app.get("/api/preflight")
async def preflight(client_time_ms: int = Query(None, description="Phone's current time in Unix ms")):
    """Run preflight check on all cameras, storage, and CAN bus."""
    import time

    server_time_ms = int(time.time() * 1000)

    orchestrator = get_orchestrator()
    results = await orchestrator.preflight_all()

    # Check storage is ready
    storage_status = get_full_storage_status()
    storage_ready = storage_status.get("all_mounted", False)

    # Check CAN bus status
    can_listener = get_can_listener()
    can_status = can_listener.get_status()
    can_ready = can_status.get("connected", False) and can_status.get("ntp_synced", False)

    # All systems ready check
    cameras_ready = all(r.get("ready", False) for r in results.values())
    all_ready = cameras_ready and storage_ready and can_ready

    return {
        "success": True,
        "data": {
            "ready": all_ready,
            "server_time_ms": server_time_ms,
            "client_time_ms": client_time_ms,  # Echoed back for RTT calculation
            "cameras": results,
            "storage": {
                "ready": storage_ready,
                "mounted": storage_status.get("all_mounted", False),
            },
            "can": {
                "ready": can_ready,
                "connected": can_status.get("connected", False),
                "ntp_synced": can_status.get("ntp_synced", False),
            },
        },
    }


@app.post("/api/record/start")
async def start_recording(
    uuid: str = Query(None, description="Optional session UUID"),
):
    """Start synchronized recording on all cameras."""
    orchestrator = get_orchestrator()

    result = await orchestrator.start_recording(session_uuid=uuid)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to start"))

    return {"success": True, "data": result}


@app.post("/api/record/stop")
async def stop_recording():
    """Stop recording on all cameras."""
    orchestrator = get_orchestrator()

    result = await orchestrator.stop_recording()

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Not recording"))

    return {"success": True, "data": result}


# Segment Upload (from cameras)


@app.put("/api/segment/{camera}/{uuid}/{filename}")
async def upload_segment(camera: str, uuid: str, filename: str, request: Request):
    """
    Receive segment upload from camera.

    Streams to disk, tracks progress for iOS visualization.
    """
    content_length = request.headers.get("content-length")
    if not content_length:
        raise HTTPException(status_code=400, detail="Content-Length required")

    content_length = int(content_length)

    async def body_stream():
        async for chunk in request.stream():
            yield chunk

    success = await receive_segment(
        camera=camera,
        uuid=uuid,
        filename=filename,
        content_length=content_length,
        body_stream=body_stream(),
    )

    if not success:
        raise HTTPException(status_code=500, detail="Upload failed")

    # Broadcast sync progress to iOS
    state = get_state()
    segments = state.get_session_segments(uuid)
    synced = segments.get(camera, 0)
    cam_info = state.get_camera(camera)
    queued = cam_info.pending_uploads if cam_info else 0

    await broadcast_sync_progress(
        camera=camera,
        synced=synced,
        queued=max(synced, queued),  # queued is at least synced
        status="syncing" if queued > synced else "complete"
    )

    return {"success": True}


# Upload Progress (for iOS)


@app.get("/api/uploads")
async def active_uploads():
    """Get active upload progress (for iOS visualization)."""
    state = get_state()

    return {
        "success": True,
        "data": state.get_active_uploads(),
    }


@app.get("/api/uploads/{uuid}")
async def session_uploads(uuid: str):
    """Get upload progress for a specific session."""
    state = get_state()

    return {
        "success": True,
        "data": {
            "uuid": uuid,
            "segments": state.get_session_segments(uuid),
            "active": [
                u for u in state.get_active_uploads()
                if u["uuid"] == uuid
            ],
        },
    }


# Storage Management


@app.get("/api/storage/status")
async def storage_status():
    """Get detailed storage status for both partitions."""
    return {
        "success": True,
        "data": get_full_storage_status(),
    }


@app.post("/api/storage/remount")
async def storage_remount():
    """Mount storage partitions and resume camera uploads and CAN logging."""
    success, message = mount_storage()

    if not success:
        raise HTTPException(status_code=500, detail=message)

    # Resume uploads on all cameras
    orchestrator = get_orchestrator()
    resume_results = await orchestrator.resume_all_uploads()
    log_info("api", "Resumed camera uploads after remount", cameras=resume_results)

    # Resume CAN logging
    can_listener = get_can_listener()
    can_listener.resume()

    # Broadcast storage change to iOS
    await broadcast_storage()

    return {
        "success": True,
        "message": message,
        "data": get_full_storage_status(),
        "uploads_resumed": True,
        "can_resumed": True,
    }


@app.post("/api/storage/unmount")
async def storage_unmount():
    """
    Safely eject storage (sync + unmount both partitions).
    Pauses camera uploads and CAN logging first, then unmounts.
    """
    # Check if already unmounted
    if not is_mounted(STORAGE_MOUNT):
        return {
            "success": True,
            "message": "Storage already unmounted",
            "data": get_full_storage_status(),
        }

    # Pause uploads on all cameras first
    orchestrator = get_orchestrator()
    pause_results = await orchestrator.pause_all_uploads()
    log_info("api", "Paused camera uploads for safe unmount", cameras=pause_results)

    # Pause CAN logging
    can_listener = get_can_listener()
    can_listener.pause()

    # Small delay to let current uploads finish
    import asyncio
    await asyncio.sleep(2)

    success, message = unmount_storage()

    if not success:
        # Resume uploads and CAN if unmount failed
        await orchestrator.resume_all_uploads()
        can_listener.resume()
        raise HTTPException(status_code=500, detail=message)

    log_info("api", "Storage safely ejected")

    # Broadcast storage change to iOS
    await broadcast_storage()

    return {
        "success": True,
        "message": message,
        "data": get_full_storage_status(),
        "uploads_paused": True,
        "can_paused": True,
    }


# Sessions


@app.get("/api/sessions")
async def db_sessions():
    """List sessions from database."""
    db = get_db()
    sessions = db.list_sessions()

    return {
        "success": True,
        "data": sessions,
    }


@app.get("/api/sessions/unexported")
async def unexported_sessions():
    """List sessions not yet exported to Mac partition."""
    db = get_db()
    sessions = db.get_unexported_sessions()

    return {
        "success": True,
        "data": sessions,
    }


@app.get("/api/sessions/{uuid}")
async def session(uuid: str):
    """Get info for a specific session."""
    info = get_session_info(uuid)

    if not info["exists"]:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"success": True, "data": info}


@app.get("/api/sessions/{uuid}/stats")
async def session_stats(uuid: str):
    """Get comprehensive stats for a session from database."""
    db = get_db()
    stats = db.get_session_stats(uuid)

    if not stats.get("exists"):
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "success": True,
        "data": stats,
    }


@app.get("/api/sessions/{uuid}/segments")
async def session_segments(uuid: str):
    """Get all segments for a session, grouped by camera."""
    db = get_db()
    segments = db.get_session_segments(uuid)

    return {
        "success": True,
        "data": {
            "uuid": uuid,
            "cameras": segments,
            "counts": db.get_segment_counts(uuid),
            "total": db.get_total_segments(uuid),
        },
    }


# Export Management


@app.get("/api/export/sessions")
async def exported_sessions():
    """List sessions on export partition."""
    return {
        "success": True,
        "data": list_exported_sessions(),
    }


@app.post("/api/export/{uuid}")
async def export_session_endpoint(uuid: str):
    """Export a session to the Mac-readable partition."""
    success, message = export_session(uuid)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {
        "success": True,
        "message": message,
    }


@app.delete("/api/export/{uuid}")
async def delete_export(uuid: str):
    """Delete a session from export partition."""
    success, message = delete_exported_session(uuid)

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {
        "success": True,
        "message": message,
    }


# Phone/Watch Data Sync


@app.put("/api/sync/phone/{uuid}/{filename}")
async def sync_phone_data(uuid: str, filename: str, request: Request):
    """
    Receive phone data file (motion, location, etc.) for a session.
    Stores in session directory alongside camera segments.
    """
    from storage import receive_phone_data

    content_length = request.headers.get("content-length")
    if not content_length:
        raise HTTPException(status_code=400, detail="Content-Length required")

    async def body_stream():
        async for chunk in request.stream():
            yield chunk

    success = await receive_phone_data(
        uuid=uuid,
        filename=filename,
        content_length=int(content_length),
        body_stream=body_stream(),
    )

    if not success:
        raise HTTPException(status_code=500, detail="Upload failed")

    # Broadcast to iOS
    await broadcast_phone_sync(uuid, filename)

    return {"success": True}


@app.put("/api/sync/watch/{uuid}/{filename}")
async def sync_watch_data(uuid: str, filename: str, request: Request):
    """
    Receive watch data file (heart rate, motion, etc.) for a session.
    Stores in session directory alongside camera segments.
    """
    from storage import receive_watch_data

    content_length = request.headers.get("content-length")
    if not content_length:
        raise HTTPException(status_code=400, detail="Content-Length required")

    async def body_stream():
        async for chunk in request.stream():
            yield chunk

    success = await receive_watch_data(
        uuid=uuid,
        filename=filename,
        content_length=int(content_length),
        body_stream=body_stream(),
    )

    if not success:
        raise HTTPException(status_code=500, detail="Upload failed")

    # Broadcast to iOS
    await broadcast_watch_sync(uuid, filename)

    return {"success": True}


# CAN Bus Data


@app.get("/api/can/status")
async def can_status():
    """
    Get CAN bus listener status.

    Returns connection status, recording state, and frame count.
    When no ESP32 is connected, mock data is generated during recording.
    """
    can_listener = get_can_listener()
    return {
        "success": True,
        "data": can_listener.get_status(),
    }


# iOS App Logging


@app.post("/api/log")
async def receive_ios_log(request: Request):
    """
    Receive logs from iOS app.
    Writes to /data/logs/ios/app.log for Fluent Bit pickup.
    """
    try:
        data = await request.json()
        log_ios(
            node=data.get("node", "iphone"),
            component=data.get("component", "app"),
            level=data.get("level", "INFO"),
            message=data.get("message", ""),
        )
        return {"success": True}
    except Exception:
        return {"success": False}


# System Shutdown


@app.post("/api/shutdown/all")
async def shutdown_all():
    """
    Safely shut down entire system (all cameras + controller).

    Requires recording to be stopped first.
    1. Pauses uploads
    2. Sends shutdown to all cameras
    3. Waits for cameras to go offline
    4. Unmounts storage
    5. Shuts down controller
    """
    import subprocess

    state = get_state()

    # Reject if recording is active
    if state.is_recording:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot shutdown: recording in progress (session: {state.session_uuid}). Stop recording first."
        )

    orchestrator = get_orchestrator()

    # Step 1: Pause uploads on all cameras
    log_info("api", "Shutdown: pausing uploads")
    await orchestrator.pause_all_uploads()

    # Step 2: Send shutdown to all cameras
    log_info("api", "Shutdown: sending shutdown to cameras")
    camera_results = await orchestrator.shutdown_all_cameras()

    # Step 3: Wait for cameras to go offline (max 15s)
    log_info("api", "Shutdown: waiting for cameras to go offline")
    offline_status = await orchestrator.wait_cameras_offline(timeout=15.0)

    # Step 4: Unmount storage
    log_info("api", "Shutdown: unmounting storage")
    unmount_success, unmount_msg = unmount_storage()

    log_info("api", "System shutdown complete, shutting down controller",
             cameras=camera_results, offline=offline_status, storage_unmounted=unmount_success)

    # Step 5: Shutdown controller
    subprocess.Popen(["sudo", "shutdown", "-h", "now"])

    return {
        "success": True,
        "message": "System shutdown initiated",
        "cameras": camera_results,
        "cameras_offline": offline_status,
        "storage_unmounted": unmount_success,
    }
