"""
CAN bus TCP listener for controller.

Receives CAN frames from ESP32 via TCP (port 9101) and logs them
during recording sessions.

ESP32 Format: ts,id,len,data\n
Example: 1714844321234,123,8,AABBCCDDEEFF0011
"""

import asyncio
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from logger import log_info, log_error, log_warning
from storage import is_mounted, STORAGE_MOUNT


@dataclass
class CANListenerState:
    """State for the CAN listener."""
    connected: bool = False
    ntp_synced: bool = False  # ESP32 has valid NTP time
    client_addr: Optional[str] = None
    recording: bool = False
    session_uuid: Optional[str] = None
    frames_received: int = 0
    last_frame_time: Optional[float] = None
    paused: bool = False  # Paused for SSD unmount


class CANListener:
    """
    TCP server for receiving CAN bus data from ESP32.

    - Listens on port 9101
    - Logs CAN frames to session directory during recording
    """

    def __init__(self, port: int = 9101, recordings_path: Path = None):
        self.port = port
        self.recordings_path = recordings_path or Path("/mnt/storage/sessions")
        self.state = CANListenerState()

        self._server: Optional[asyncio.Server] = None
        self._log_file: Optional[object] = None
        self._log_path: Optional[Path] = None
        self._running = False

    async def start(self):
        """Start the TCP server."""
        self._running = True
        self._server = await asyncio.start_server(
            self._handle_client,
            '0.0.0.0',
            self.port
        )
        log_info("can", f"CAN listener started on port {self.port}")

    async def stop(self):
        """Stop the TCP server."""
        self._running = False

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        self._close_log()
        log_info("can", "CAN listener stopped")

    async def start_recording(self, session_uuid: str):
        """Start logging CAN data for a recording session."""
        self.state.session_uuid = session_uuid
        self.state.recording = True
        self.state.frames_received = 0

        # Open log file
        if not self.state.paused and is_mounted(STORAGE_MOUNT):
            self._open_log(session_uuid)

        if self.state.connected:
            log_info("can", "Recording CAN data from ESP32", uuid=session_uuid)
        else:
            log_warning("can", "Recording started but ESP32 not connected", uuid=session_uuid)

    async def stop_recording(self):
        """Stop logging CAN data."""
        self.state.recording = False

        # Close log file
        frames = self.state.frames_received
        self._close_log()

        log_info("can", f"Stopped CAN recording, {frames} frames logged",
                 uuid=self.state.session_uuid)

        self.state.session_uuid = None
        self.state.frames_received = 0

    def pause(self):
        """Pause logging (for SSD unmount)."""
        self.state.paused = True
        self._close_log()
        log_info("can", "CAN logging paused (storage unmounting)")

    def resume(self):
        """Resume logging after SSD remount."""
        self.state.paused = False
        if self.state.recording and self.state.session_uuid:
            if is_mounted(STORAGE_MOUNT):
                self._open_log(self.state.session_uuid)
                log_info("can", "CAN logging resumed")

    def get_status(self) -> dict:
        """Get current status for iOS app."""
        if self.state.paused:
            status = "paused"
        elif self.state.recording:
            status = "recording"
        else:
            status = "idle"

        return {
            "connected": self.state.connected,
            "ntp_synced": self.state.ntp_synced,
            "status": status,
        }

    def _open_log(self, session_uuid: str):
        """Open log file for a session."""
        self._close_log()  # Close any existing

        can_dir = self.recordings_path / session_uuid / "can"
        can_dir.mkdir(parents=True, exist_ok=True)

        self._log_path = can_dir / "can_log.csv"
        self._log_file = open(self._log_path, "a")

        # Write header if new file
        if self._log_path.stat().st_size == 0:
            self._log_file.write("timestamp,pi_time,can_id,length,data\n")
            self._log_file.flush()

    def _close_log(self):
        """Close current log file."""
        if self._log_file:
            self._log_file.close()
            self._log_file = None
            self._log_path = None

    def _write_frame(self, timestamp: float, can_id: int, length: int, data: str):
        """Write a CAN frame to the log."""
        if not self._log_file or self.state.paused:
            return

        try:
            pi_time = time.time()
            self._log_file.write(f"{timestamp:.3f},{pi_time:.3f},0x{can_id:03X},{length},{data}\n")
            self._log_file.flush()
            self.state.frames_received += 1
            self.state.last_frame_time = timestamp
        except Exception as e:
            log_error("can", f"Failed to write frame: {e}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming TCP connection from ESP32."""
        addr = writer.get_extra_info('peername')
        self.state.connected = True
        self.state.client_addr = f"{addr[0]}:{addr[1]}"

        log_info("can", f"ESP32 connected from {self.state.client_addr}")

        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break

                # Parse: ts,id,len,data
                try:
                    line_str = line.decode('utf-8').strip()
                    if not line_str or line_str.startswith('ts,'):  # Skip empty or header
                        continue

                    parts = line_str.split(',')
                    if len(parts) >= 4:
                        raw_ts = float(parts[0])
                        # ESP32 sends milliseconds, convert to seconds if needed
                        if raw_ts > 1e12:  # Milliseconds (13+ digits)
                            timestamp = raw_ts / 1000.0
                        else:  # Already seconds
                            timestamp = raw_ts

                        can_id = int(parts[1], 16) if parts[1].startswith('0x') else int(parts[1], 16)
                        length = int(parts[2])
                        data = parts[3]

                        # Check NTP sync - timestamp should be within 5s of current time
                        time_diff = abs(timestamp - time.time())
                        self.state.ntp_synced = time_diff < 5

                        if self.state.recording:
                            self._write_frame(timestamp, can_id, length, data)

                except (ValueError, IndexError) as e:
                    log_warning("can", f"Invalid CAN frame: {line_str}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_error("can", f"Connection error: {e}")
        finally:
            writer.close()
            await writer.wait_closed()
            self.state.connected = False
            self.state.client_addr = None
            self.state.ntp_synced = False
            log_info("can", "ESP32 disconnected")


# Global instance
_can_listener: Optional[CANListener] = None


def get_can_listener() -> CANListener:
    """Get global CAN listener instance."""
    global _can_listener
    if _can_listener is None:
        _can_listener = CANListener()
    return _can_listener


async def init_can_listener(recordings_path: Path = None) -> CANListener:
    """Initialize and start the CAN listener."""
    global _can_listener
    _can_listener = CANListener(recordings_path=recordings_path)
    await _can_listener.start()
    return _can_listener
