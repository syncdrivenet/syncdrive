"""
WebSocket service for real-time iOS app updates.
Provides /ws/status endpoint matching iOS Discar app expectations.
"""

import asyncio
import json
import psutil
from typing import Set
from fastapi import WebSocket, WebSocketDisconnect

from config import get_config
from state import get_state
from storage import get_full_storage_status, is_mounted, STORAGE_MOUNT, EXPORT_MOUNT


# Connected WebSocket clients
connected_clients: Set[WebSocket] = set()


async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for iOS app real-time updates.
    Sends initial state on connect, then pushes updates.
    """
    await websocket.accept()
    connected_clients.add(websocket)

    try:
        # Send full initial state
        await websocket.send_json({
            "type": "initial",
            "data": get_full_state()
        })

        # Keep connection alive, handle pings
        while True:
            try:
                # Wait for client messages (pings, etc.)
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=60.0
                )
                # Handle ping
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # Send keepalive ping
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(websocket)


def get_full_state() -> dict:
    """Get complete state for initial WebSocket message."""
    return {
        "controller": get_controller_state(),
        "cameras": get_cameras_state(),
        "system": get_system_state(),
        "storage": get_storage_state_ios(),
        "can": get_can_state(),
    }


def get_controller_state() -> dict:
    """Get controller state in iOS-expected format."""
    state = get_state()
    config = get_config()

    # Check if all cameras are ready
    all_ready = all(
        cam.status.value in ("online", "recording")
        for cam in state.cameras.values()
    ) if state.cameras else False

    return {
        "ready": all_ready or state.is_idle,
        "recording": state.is_recording,
        "uuid": state.session_uuid,
        "duration": state.duration,
    }


def get_cameras_state() -> list:
    """Get all cameras state in iOS-expected format."""
    state = get_state()
    config = get_config()
    cameras = []

    for cam_config in config.cameras:
        name = cam_config.name
        cam_info = state.cameras.get(name)

        if cam_info:
            cameras.append({
                "name": name,
                "connected": cam_info.status.value != "offline",
                "state": cam_info.status.value,
                "segment": cam_info.segment,
                "cpu": None,  # Would need to query camera
                "ram": None,
                "disk_free_gb": None,
                "temp": None,
                "sync_status": "idle",
                "sync_segments_synced": 0,
                "sync_segments_queued": cam_info.pending_uploads,
            })
        else:
            cameras.append({
                "name": name,
                "connected": False,
                "state": "unknown",
                "segment": None,
            })

    return cameras


def get_system_state() -> dict:
    """Get controller system metrics."""
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        # Temperature on Pi
        try:
            temp = psutil.sensors_temperatures()
            if "cpu_thermal" in temp:
                temp_c = temp["cpu_thermal"][0].current
            else:
                temp_c = 0
        except Exception:
            temp_c = 0

        return {
            "cpu_percent": cpu,
            "mem_percent": mem,
            "temp_c": temp_c,
        }
    except Exception:
        return {
            "cpu_percent": 0,
            "mem_percent": 0,
            "temp_c": 0,
        }


def get_storage_state_ios() -> dict:
    """
    Get storage state in iOS-expected format.
    Maps our storage/export to iOS's logging/sync naming.
    """
    status = get_full_storage_status()

    storage_data = status.get("storage", {})
    export_data = status.get("export", {})

    return {
        "healthy": status.get("all_mounted", False),
        "logging": {
            "accessible": storage_data.get("mounted", False),
            "free_gb": storage_data.get("free_gb", 0),
        },
        "sync": {
            "accessible": export_data.get("mounted", False),
            "free_gb": export_data.get("free_gb", 0),
        },
    }


def get_can_state() -> dict:
    """Get CAN bus state (placeholder - implement when CAN service ready)."""
    return {
        "connected": False,
        "frame_count": 0,
        "file_size_bytes": 0,
    }


# Broadcast functions for state changes

async def broadcast(message: dict):
    """Send message to all connected clients."""
    if not connected_clients:
        return

    disconnected = set()
    for client in connected_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.add(client)

    # Cleanup disconnected clients
    for client in disconnected:
        connected_clients.discard(client)


async def broadcast_controller():
    """Broadcast controller state update."""
    await broadcast({
        "type": "controller",
        "data": get_controller_state()
    })


async def broadcast_cameras():
    """Broadcast all cameras state."""
    await broadcast({
        "type": "cameras",
        "data": get_cameras_state()
    })


async def broadcast_camera(name: str):
    """Broadcast single camera update."""
    state = get_state()
    cam_info = state.cameras.get(name)

    if cam_info:
        await broadcast({
            "type": "camera",
            "data": {
                "name": name,
                "connected": cam_info.status.value != "offline",
                "state": cam_info.status.value,
                "segment": cam_info.segment,
                "sync_segments_queued": cam_info.pending_uploads,
            }
        })


async def broadcast_storage():
    """Broadcast storage state update."""
    await broadcast({
        "type": "storage",
        "data": get_storage_state_ios()
    })


async def broadcast_system():
    """Broadcast system metrics."""
    await broadcast({
        "type": "system",
        "data": get_system_state()
    })


async def broadcast_sync_progress(camera: str, synced: int, queued: int, status: str = "syncing"):
    """Broadcast sync/upload progress for a camera."""
    await broadcast({
        "type": "sync",
        "data": {
            "camera": camera,
            "synced": synced,
            "queued": queued,
            "status": status,
        }
    })


async def broadcast_phone_sync(uuid: str, filename: str, status: str = "complete"):
    """Broadcast phone data sync progress."""
    await broadcast({
        "type": "phone_sync",
        "data": {
            "uuid": uuid,
            "filename": filename,
            "status": status,
        }
    })


async def broadcast_watch_sync(uuid: str, filename: str, status: str = "complete"):
    """Broadcast watch data sync progress."""
    await broadcast({
        "type": "watch_sync",
        "data": {
            "uuid": uuid,
            "filename": filename,
            "status": status,
        }
    })
