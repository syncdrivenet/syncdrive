"""
Camera orchestrator for controller.
Coordinates multi-camera recording sessions.
"""

import asyncio
import time
import uuid as uuid_lib
from typing import Optional, List, Dict

import httpx

from config import get_config, CameraConfig
from state import get_state, CameraStatus
from logger import log_info, log_error, log_warning
from database import get_db
from can_listener import get_can_listener
from websocket import broadcast_camera, broadcast_can


class Orchestrator:
    """
    Orchestrates multi-camera recording.

    - Checks camera health/preflight
    - Sends synchronized start commands
    - Monitors camera status
    - Handles stop commands
    """

    def __init__(self):
        self.config = get_config()
        self.state = get_state()
        self.client: Optional[httpx.AsyncClient] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._can_monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_can_status: Optional[dict] = None

    async def start(self):
        """Start the orchestrator."""
        if self._running:
            return

        self._running = True
        self.client = httpx.AsyncClient(timeout=10.0)

        # Start camera monitor
        self._monitor_task = asyncio.create_task(self._monitor_cameras())
        # Start CAN monitor
        self._can_monitor_task = asyncio.create_task(self._monitor_can())

        log_info("orchestrator", "Orchestrator started")

    async def stop(self):
        """Stop the orchestrator."""
        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._can_monitor_task:
            self._can_monitor_task.cancel()
            try:
                await self._can_monitor_task
            except asyncio.CancelledError:
                pass

        if self.client:
            await self.client.aclose()

        log_info("orchestrator", "Orchestrator stopped")

    async def _monitor_cameras(self):
        """Background task to monitor camera health."""
        while self._running:
            for cam in self.config.cameras:
                try:
                    await self._check_camera(cam)
                except Exception as e:
                    log_warning("orchestrator", f"Error checking {cam.name}: {e}")

            await asyncio.sleep(1)  # Poll cameras every 1s for responsive UI

    async def _check_camera(self, cam: CameraConfig):
        """Check single camera health and status."""
        # Get old state for comparison
        old_cam = self.state.get_camera(cam.name)
        old_segment = old_cam.segment if old_cam else 0
        old_status = old_cam.status if old_cam else None
        old_ntp_synced = old_cam.ntp_synced if old_cam else None

        try:
            response = await self.client.get(f"{cam.base_url}/status")
            if response.status_code == 200:
                data = response.json().get("data", {})
                state = data.get("state", "idle")
                new_segment = data.get("segment", 0)
                new_ntp_synced = data.get("ntp_synced", True)

                if state == "recording":
                    status = CameraStatus.RECORDING
                else:
                    status = CameraStatus.ONLINE

                self.state.update_camera(
                    cam.name,
                    status,
                    ntp_synced=new_ntp_synced,
                    segment=new_segment,
                    pending_uploads=data.get("pending_uploads", 0),
                    disk_free_gb=data.get("disk_free_gb"),
                    disk_total_gb=data.get("disk_total_gb"),
                    disk_used_gb=data.get("disk_used_gb"),
                    upload_queue=data.get("upload_queue", []),
                )

                # Broadcast if segment, status, or NTP sync changed
                if new_segment != old_segment or status != old_status or new_ntp_synced != old_ntp_synced:
                    await broadcast_camera(cam.name)

            else:
                self.state.update_camera(cam.name, CameraStatus.ERROR)
                if old_status != CameraStatus.ERROR:
                    await broadcast_camera(cam.name)

        except httpx.ConnectError:
            self.state.update_camera(cam.name, CameraStatus.OFFLINE, ntp_synced=False)
            if old_status != CameraStatus.OFFLINE:
                await broadcast_camera(cam.name)
        except Exception as e:
            self.state.update_camera(cam.name, CameraStatus.ERROR, error=str(e))
            if old_status != CameraStatus.ERROR:
                await broadcast_camera(cam.name)

    async def _monitor_can(self):
        """Background task to monitor CAN bus status and broadcast on change."""
        while self._running:
            try:
                can_listener = get_can_listener()
                current_status = can_listener.get_status()

                # Broadcast if status changed
                if current_status != self._last_can_status:
                    self._last_can_status = current_status
                    await broadcast_can()

            except Exception as e:
                log_warning("orchestrator", f"Error checking CAN status: {e}")

            await asyncio.sleep(1)  # Poll CAN status every 1s

    async def preflight_all(self) -> Dict[str, dict]:
        """Run preflight check on all cameras."""
        results = {}

        async def check_one(cam: CameraConfig):
            try:
                response = await self.client.get(f"{cam.base_url}/preflight")
                if response.status_code == 200:
                    data = response.json()
                    results[cam.name] = data.get("data", {})
                else:
                    results[cam.name] = {"ready": False, "error": f"HTTP {response.status_code}"}
            except Exception as e:
                results[cam.name] = {"ready": False, "error": str(e)}

        await asyncio.gather(*[check_one(cam) for cam in self.config.cameras])

        return results

    async def start_recording(self, session_uuid: Optional[str] = None, sync_delay_ms: int = 2000) -> dict:
        """
        Start synchronized recording on all cameras.

        Args:
            session_uuid: Optional UUID (generated if not provided)
            sync_delay_ms: Delay before synchronized start (ms)

        Returns:
            Result dict with uuid and camera results
        """
        if self.state.is_recording:
            return {"success": False, "error": "Already recording"}

        # Generate UUID if not provided
        if session_uuid is None:
            session_uuid = str(uuid_lib.uuid4())

        # Calculate synchronized start time
        start_at = int(time.time() * 1000) + sync_delay_ms

        log_info("orchestrator", f"Starting recording", uuid=session_uuid, start_at=start_at)

        # Send start command to all cameras
        results = {}

        async def start_one(cam: CameraConfig):
            try:
                response = await self.client.post(
                    f"{cam.base_url}/record/start",
                    params={"uuid": session_uuid, "start_at": start_at},
                )
                if response.status_code == 200:
                    results[cam.name] = {"success": True}
                    # Reset segment to 1 for new recording
                    self.state.update_camera(cam.name, CameraStatus.RECORDING, segment=1)
                else:
                    results[cam.name] = {"success": False, "error": f"HTTP {response.status_code}"}
            except Exception as e:
                results[cam.name] = {"success": False, "error": str(e)}

        await asyncio.gather(*[start_one(cam) for cam in self.config.cameras])

        # Check if any camera started successfully
        any_success = any(r.get("success") for r in results.values())

        if any_success:
            self.state.set_recording(session_uuid)
            # Create session in database
            db = get_db()
            db.create_session(session_uuid, cameras_count=len(self.config.cameras))

            # Start CAN listener recording
            can_listener = get_can_listener()
            await can_listener.start_recording(session_uuid)

            log_info("orchestrator", f"Recording started", uuid=session_uuid)
        else:
            log_error("orchestrator", "All cameras failed to start")

        return {
            "success": any_success,
            "uuid": session_uuid,
            "cameras": results,
        }

    async def stop_recording(self) -> dict:
        """Stop recording on all cameras."""
        if not self.state.is_recording:
            return {"success": False, "error": "Not recording"}

        session_uuid = self.state.session_uuid

        log_info("orchestrator", f"Stopping recording", uuid=session_uuid)

        results = {}

        async def stop_one(cam: CameraConfig):
            try:
                response = await self.client.post(f"{cam.base_url}/record/stop")
                if response.status_code == 200:
                    data = response.json()
                    results[cam.name] = data
                    # Reset segment to 0 when stopped
                    self.state.update_camera(cam.name, CameraStatus.ONLINE, segment=0)
                else:
                    results[cam.name] = {"success": False, "error": f"HTTP {response.status_code}"}
            except Exception as e:
                results[cam.name] = {"success": False, "error": str(e)}

        await asyncio.gather(*[stop_one(cam) for cam in self.config.cameras])

        # Stop CAN listener recording
        can_listener = get_can_listener()
        await can_listener.stop_recording()

        # Mark session stopped in database
        db = get_db()
        db.stop_session(session_uuid)

        self.state.set_idle()

        log_info("orchestrator", f"Recording stopped", uuid=session_uuid)

        return {
            "success": True,
            "uuid": session_uuid,
            "cameras": results,
        }

    async def get_camera_status(self, camera_name: str) -> Optional[dict]:
        """Get detailed status from a specific camera."""
        cam = next((c for c in self.config.cameras if c.name == camera_name), None)
        if not cam:
            return None

        try:
            response = await self.client.get(f"{cam.base_url}/status")
            if response.status_code == 200:
                return response.json().get("data")
        except Exception:
            pass

        return None

    async def pause_all_uploads(self) -> Dict[str, dict]:
        """Pause uploads on all cameras (for safe HDD unmount)."""
        results = {}

        async def pause_one(cam: CameraConfig):
            try:
                response = await self.client.post(f"{cam.base_url}/upload/pause")
                if response.status_code == 200:
                    results[cam.name] = {"success": True, "paused": True}
                else:
                    results[cam.name] = {"success": False, "error": f"HTTP {response.status_code}"}
            except Exception as e:
                results[cam.name] = {"success": False, "error": str(e)}

        await asyncio.gather(*[pause_one(cam) for cam in self.config.cameras])

        log_info("orchestrator", "Paused uploads on all cameras")
        return results

    async def resume_all_uploads(self) -> Dict[str, dict]:
        """Resume uploads on all cameras (after HDD remount)."""
        results = {}

        async def resume_one(cam: CameraConfig):
            try:
                response = await self.client.post(f"{cam.base_url}/upload/resume")
                if response.status_code == 200:
                    results[cam.name] = {"success": True, "paused": False}
                else:
                    results[cam.name] = {"success": False, "error": f"HTTP {response.status_code}"}
            except Exception as e:
                results[cam.name] = {"success": False, "error": str(e)}

        await asyncio.gather(*[resume_one(cam) for cam in self.config.cameras])

        log_info("orchestrator", "Resumed uploads on all cameras")
        return results

    async def shutdown_all_cameras(self) -> Dict[str, dict]:
        """Send shutdown command to all cameras."""
        results = {}

        async def shutdown_one(cam: CameraConfig):
            try:
                response = await self.client.post(f"{cam.base_url}/shutdown", timeout=5.0)
                if response.status_code == 200:
                    results[cam.name] = {"success": True, "message": "Shutting down"}
                else:
                    results[cam.name] = {"success": False, "error": f"HTTP {response.status_code}"}
            except Exception as e:
                # Connection error is expected - camera may shutdown before response
                results[cam.name] = {"success": True, "message": "Shutdown initiated"}

        await asyncio.gather(*[shutdown_one(cam) for cam in self.config.cameras])

        log_info("orchestrator", "Shutdown command sent to all cameras")
        return results

    async def wait_cameras_offline(self, timeout: float = 15.0) -> Dict[str, bool]:
        """Wait for all cameras to go offline (confirm shutdown)."""
        import time
        start = time.time()
        offline_status = {cam.name: False for cam in self.config.cameras}

        while time.time() - start < timeout:
            all_offline = True
            for cam in self.config.cameras:
                if offline_status[cam.name]:
                    continue
                try:
                    response = await self.client.get(f"{cam.base_url}/health", timeout=2.0)
                    # Still responding - not offline yet
                    all_offline = False
                except Exception:
                    # Connection failed - camera is offline
                    offline_status[cam.name] = True
                    log_info("orchestrator", f"Camera offline: {cam.name}")

            if all_offline:
                break
            await asyncio.sleep(1)

        return offline_status


# Global orchestrator
_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    """Get global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


async def start_orchestrator():
    """Start the global orchestrator."""
    orchestrator = get_orchestrator()
    await orchestrator.start()


async def stop_orchestrator():
    """Stop the global orchestrator."""
    global _orchestrator
    if _orchestrator:
        await _orchestrator.stop()
