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
    export_session, list_exported_sessions, delete_exported_session, validate_session
)
from orchestrator import get_orchestrator
from logger import log_info, log_error, log_ios, log_event
from websocket import (
    websocket_endpoint, broadcast_storage, broadcast_sync_progress,
    broadcast_phone_sync, broadcast_watch_sync, broadcast_controller,
    broadcast_cameras
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

    # Build camera summary for logging
    camera_summary = {}
    cameras_not_ready = []
    for name, r in results.items():
        camera_summary[name] = {
            "ready": r.get("ready", False),
            "ntp": r.get("ntp_synced", False),
            "disk_ok": r.get("disk_ok", False),
            "camera": r.get("camera", False),
        }
        if not r.get("ready", False):
            reasons = []
            if not r.get("ntp_synced", True):
                reasons.append("ntp")
            if not r.get("disk_ok", True):
                reasons.append("disk")
            if not r.get("camera", True):
                reasons.append("camera")
            if not r.get("recorder_alive", True):
                reasons.append("recorder")
            cameras_not_ready.append(f"{name}({','.join(reasons) or 'unknown'})")

    # Log the preflight event
    if all_ready:
        log_event(
            "preflight", "success",
            f"Preflight passed: {len(results)} cameras ready",
            cameras=camera_summary,
            storage_ready=storage_ready,
            can_connected=can_status.get("connected", False),
        )
    else:
        issues = []
        if cameras_not_ready:
            issues.append(f"cameras: {', '.join(cameras_not_ready)}")
        if not storage_ready:
            issues.append("storage not mounted")
        if not can_status.get("connected", False):
            issues.append("CAN not connected")
        elif not can_status.get("ntp_synced", False):
            issues.append("CAN NTP not synced")

        log_event(
            "preflight", "failure",
            f"Preflight failed: {'; '.join(issues)}",
            cameras=camera_summary,
            storage_ready=storage_ready,
            can_connected=can_status.get("connected", False),
            issues=issues,
        )

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
        # Log failure event
        log_event(
            "record_start", "failure",
            f"Recording start failed: {result.get('error', 'Unknown error')}",
            uuid=uuid,
            error=result.get("error"),
            camera_results=result.get("cameras", {}),
        )
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to start"))

    # Log success event
    log_event(
        "record_start", "success",
        f"Recording started: {result.get('uuid', uuid)}",
        uuid=result.get("uuid", uuid),
        cameras_started=list(result.get("cameras", {}).keys()),
    )

    # Broadcast state change to iOS
    await broadcast_controller()
    await broadcast_cameras()

    return {"success": True, "data": result}


@app.post("/api/record/stop")
async def stop_recording():
    """Stop recording on all cameras."""
    orchestrator = get_orchestrator()
    state = get_state()
    current_uuid = state.uuid

    result = await orchestrator.stop_recording()

    if not result["success"]:
        log_event(
            "record_stop", "failure",
            f"Recording stop failed: {result.get('error', 'Unknown error')}",
            uuid=current_uuid,
            error=result.get("error"),
        )
        raise HTTPException(status_code=400, detail=result.get("error", "Not recording"))

    # Log success event
    log_event(
        "record_stop", "success",
        f"Recording stopped: {current_uuid}",
        uuid=current_uuid,
        duration=result.get("duration"),
        cameras_stopped=list(result.get("cameras", {}).keys()),
    )

    # Broadcast state change to iOS
    await broadcast_controller()
    await broadcast_cameras()

    return {"success": True, "data": result}


@app.post("/api/reset")
async def reset_cameras():
    """
    Force reset all cameras to idle state.

    Use when state is out of sync (e.g., controller restarted while cameras were recording).
    Sends stop command to any camera that's recording and resets controller state.
    """
    import asyncio

    orchestrator = get_orchestrator()
    state = get_state()
    results = {}

    # Stop any cameras that are recording
    for cam in orchestrator.config.cameras:
        try:
            # Check camera status
            response = await orchestrator.client.get(f"{cam.base_url}/status", timeout=5.0)
            if response.status_code == 200:
                data = response.json().get("data", {})
                cam_state = data.get("state", "idle")

                if cam_state == "recording":
                    # Send stop command
                    stop_response = await orchestrator.client.post(f"{cam.base_url}/record/stop", timeout=5.0)
                    if stop_response.status_code == 200:
                        # Wait for camera to actually stop (poll up to 5 seconds)
                        stopped = False
                        for _ in range(10):
                            await asyncio.sleep(0.5)
                            check = await orchestrator.client.get(f"{cam.base_url}/status", timeout=5.0)
                            if check.status_code == 200:
                                if check.json().get("data", {}).get("state") == "idle":
                                    stopped = True
                                    break
                        results[cam.name] = {"was_recording": True, "stopped": stopped}
                    else:
                        results[cam.name] = {"was_recording": True, "stopped": False, "error": f"HTTP {stop_response.status_code}"}
                else:
                    results[cam.name] = {"was_recording": False, "state": cam_state}
        except Exception as e:
            results[cam.name] = {"error": str(e)}

    # Reset controller state to idle
    state.set_idle()

    # Stop CAN listener if recording
    can_listener = get_can_listener()
    if can_listener.state.recording:
        await can_listener.stop_recording()

    log_info("api", "System reset completed", cameras=results)

    # Broadcast state change to iOS
    await broadcast_controller()
    await broadcast_cameras()

    return {
        "success": True,
        "message": "All cameras reset to idle",
        "data": results,
    }


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
    pending = cam_info.pending_uploads if cam_info else 0
    total = synced + pending  # total = received + still pending

    await broadcast_sync_progress(
        camera=camera,
        synced=synced,
        queued=total,
        status="syncing" if pending > 0 else "complete"
    )

    return {"success": True}


@app.post("/api/session/{uuid}/camera-complete")
async def camera_complete(
    uuid: str,
    camera: str = Query(..., description="Camera name"),
    total_segments: int = Query(..., description="Total segments recorded"),
):
    """
    Camera reports recording complete with total segment count.
    Called by camera when recording stops.
    """
    db = get_db()
    db.set_expected_segments(uuid, camera, total_segments)

    log_info("api", f"Camera reported complete: {total_segments} segments",
             uuid=uuid, camera=camera)

    return {"success": True, "uuid": uuid, "camera": camera, "total_segments": total_segments}


# Upload Progress (for iOS)


@app.get("/api/uploads")
async def active_uploads():
    """Get active upload progress (for iOS visualization)."""
    state = get_state()

    return {
        "success": True,
        "data": state.get_active_uploads(),
    }


@app.get("/api/sync/overview")
async def sync_overview():
    """
    Get overall sync status across all sessions (for iOS sync card).

    Returns aggregate progress, ETA, and per-session queue info.
    During recording, includes live capture/sync counts.
    """
    db = get_db()
    state = get_state()

    # Check if currently recording
    is_recording = state.is_recording
    recording_uuid = state.session_uuid
    recording_info = None

    if is_recording and recording_uuid:
        # Get live segment counts for current recording
        captured = 0
        synced = 0
        for cam_name, cam_info in state.cameras.items():
            if cam_info.segment > 0:
                captured += cam_info.segment
        # Synced from database
        synced_counts = db.get_segment_counts(recording_uuid)
        synced = sum(synced_counts.values())

        # Get active upload info
        active_uploads = state.get_active_uploads()
        current_upload = None
        for upload in active_uploads:
            if upload["uuid"] == recording_uuid:
                current_upload = {
                    "camera": upload["camera"],
                    "filename": upload["filename"],
                    "percent": upload["percent"],
                }
                break

        recording_info = {
            "uuid": recording_uuid,
            "captured": captured,
            "synced": synced,
            "current_upload": current_upload,
        }

    # Get all sessions with pending uploads
    sessions_data = []
    total_synced = 0
    total_pending = 0
    total_expected = 0

    # Get upload speed stats
    upload_stats = state.get_upload_stats()
    avg_segment_time = upload_stats["avg_segment_size"] / upload_stats["avg_speed_bps"] if upload_stats["avg_speed_bps"] > 0 else 0

    # Build queue from camera upload queues (already sorted by position)
    # Aggregate across all cameras for each session
    session_pending: dict[str, dict] = {}  # uuid -> {pending, position, cameras}

    for cam_name, cam_info in state.cameras.items():
        for item in cam_info.upload_queue:
            uuid = item["uuid"]
            if uuid not in session_pending:
                session_pending[uuid] = {
                    "uuid": uuid,
                    "pending": 0,
                    "position": item.get("position", 0),  # Use first camera's position
                    "cameras": [],
                }
            session_pending[uuid]["pending"] += item.get("pending", 0)
            session_pending[uuid]["cameras"].append({
                "name": cam_name,
                "pending": item.get("pending", 0),
            })

    # Track which sessions we've processed
    processed_uuids = set()

    # Get synced counts and expected from database for sessions with pending uploads
    for uuid, info in session_pending.items():
        processed_uuids.add(uuid)
        synced_counts = db.get_segment_counts(uuid)
        expected_counts = db.get_expected_segments(uuid)

        session_synced = sum(synced_counts.values())
        session_expected = sum(expected_counts.values()) if expected_counts else session_synced + info["pending"]

        # Calculate segments ahead (sum of pending from earlier positions)
        segments_ahead = sum(
            s["pending"] for s in session_pending.values()
            if s["position"] < info["position"]
        )

        session_eta = int((segments_ahead + info["pending"]) * avg_segment_time) if avg_segment_time > 0 else None
        session_progress = round((session_synced / session_expected) * 100, 1) if session_expected > 0 else 0

        sessions_data.append({
            "uuid": uuid,
            "position": info["position"],
            "synced": session_synced,
            "pending": info["pending"],
            "expected": session_expected,
            "progress_percent": session_progress,
            "eta_seconds": session_eta,
            "segments_ahead": segments_ahead,
        })

        total_synced += session_synced
        total_pending += info["pending"]
        total_expected += session_expected

    # Also include recent sessions from database that are fully synced (no pending)
    recent_sessions = db.list_sessions(limit=20)
    for session in recent_sessions:
        uuid = session["uuid"]
        if uuid in processed_uuids:
            continue

        synced_counts = db.get_segment_counts(uuid)
        session_synced = sum(synced_counts.values())

        # Only include if session has segments
        if session_synced > 0:
            expected_counts = db.get_expected_segments(uuid)
            session_expected = sum(expected_counts.values()) if expected_counts else session_synced

            sessions_data.append({
                "uuid": uuid,
                "position": 0,  # Not in queue
                "synced": session_synced,
                "pending": 0,
                "expected": session_expected,
                "progress_percent": 100.0,
                "eta_seconds": None,
                "segments_ahead": 0,
            })

            total_synced += session_synced
            total_expected += session_expected

    # Sort: pending sessions first (by position), then synced sessions
    sessions_data.sort(key=lambda x: (x["pending"] == 0, x["position"]))

    # Overall progress
    overall_progress = round((total_synced / total_expected) * 100, 1) if total_expected > 0 else 100
    overall_eta = int(total_pending * avg_segment_time) if avg_segment_time > 0 and total_pending > 0 else None

    return {
        "success": True,
        "data": {
            "syncing": total_pending > 0,
            "recording": is_recording,
            "recording_info": recording_info,
            "synced": total_synced,
            "pending": total_pending,
            "expected": total_expected,
            "progress_percent": overall_progress,
            "eta_seconds": overall_eta,
            "speed_bps": upload_stats["avg_speed_bps"],
            "sessions": sessions_data,
            "sessions_pending": len([s for s in sessions_data if s["pending"] > 0]),
        },
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
    """Get comprehensive stats for a session from database + live state."""
    db = get_db()
    stats = db.get_session_stats(uuid)

    if not stats.get("exists"):
        raise HTTPException(status_code=404, detail="Session not found")

    # Get per-camera synced counts from database
    synced_counts = db.get_segment_counts(uuid)

    # Get expected segments from database (reported by cameras on stop)
    expected_counts = db.get_expected_segments(uuid)

    # Get pending from live camera state + queue info
    state = get_state()
    cameras_detail = []
    total_synced = 0
    total_pending = 0
    total_expected = 0
    total_segments_ahead = 0  # Segments from OTHER sessions ahead in queue

    for cam_name, cam_info in state.cameras.items():
        synced = synced_counts.get(cam_name, 0)
        expected = expected_counts.get(cam_name, 0)

        # Find this session's queue info for this camera
        session_queue_info = None
        segments_ahead = 0
        queue_position = 0

        for item in cam_info.upload_queue:
            if item["uuid"] == uuid:
                session_queue_info = item
                queue_position = item.get("position", 0)
                break
            else:
                # This session is ahead of ours
                segments_ahead += item.get("pending", 0)

        # Pending for this session from queue, or fallback to expected - synced
        if session_queue_info:
            pending = session_queue_info.get("pending", 0)
        elif expected > 0:
            pending = max(0, expected - synced)
        else:
            pending = 0

        if synced > 0 or pending > 0 or expected > 0:
            cameras_detail.append({
                "name": cam_name,
                "synced": synced,
                "pending": pending,
                "expected": expected if expected > 0 else synced + pending,
                "queue_position": queue_position,
                "segments_ahead": segments_ahead,
                "complete": pending == 0 and (expected == 0 or synced >= expected),
            })

            total_synced += synced
            total_pending += pending
            total_expected += expected if expected > 0 else synced + pending
            total_segments_ahead += segments_ahead

    # Determine sync status
    is_stopped = stats.get("status") == "stopped"
    total = total_expected if total_expected > 0 else total_synced + total_pending
    sync_complete = is_stopped and total_pending == 0 and total_synced > 0

    # Calculate progress percentage
    progress_percent = round((total_synced / total) * 100, 1) if total > 0 else 0

    # Get upload speed and calculate queue-aware ETA
    upload_stats = state.get_upload_stats()
    avg_segment_time = upload_stats["avg_segment_size"] / upload_stats["avg_speed_bps"] if upload_stats["avg_speed_bps"] > 0 else 0

    # ETA includes segments from other sessions ahead in queue
    total_to_upload = total_segments_ahead + total_pending
    eta_seconds = int(total_to_upload * avg_segment_time) if avg_segment_time > 0 else None

    # Get active uploads for this session
    active_uploads = [u for u in state.get_active_uploads() if u["uuid"] == uuid]

    # Add sync info to stats
    stats["sync"] = {
        "synced": total_synced,
        "pending": total_pending,
        "expected": total_expected,
        "progress_percent": progress_percent,
        "complete": sync_complete,
        "segments_ahead": total_segments_ahead,
        "eta_seconds": eta_seconds,
        "speed_bps": upload_stats["avg_speed_bps"],
    }
    stats["cameras_detail"] = cameras_detail
    stats["active_uploads"] = active_uploads

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


@app.get("/api/sessions/{uuid}/validate")
async def validate_session_endpoint(uuid: str):
    """
    Validate session completeness before export.

    Checks:
    - All expected segments received from all cameras
    - CAN bus log exists with data
    - Phone/watch data sync status

    Returns validation result for iOS app to display.
    """
    result = validate_session(uuid)

    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])

    return {
        "success": True,
        "data": result,
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
    # Use full paths for systemd service (PATH may not include /usr/bin)
    subprocess.Popen(["/usr/bin/sudo", "/usr/sbin/shutdown", "-h", "now"])

    return {
        "success": True,
        "message": "System shutdown initiated",
        "cameras": camera_results,
        "cameras_offline": offline_status,
        "storage_unmounted": unmount_success,
    }
