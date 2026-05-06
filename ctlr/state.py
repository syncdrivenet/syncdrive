"""
Shared state for controller node.
Tracks sessions, camera status, and upload progress.
"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List


class SessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"


class CameraStatus(str, Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    RECORDING = "recording"
    ERROR = "error"


@dataclass
class CameraInfo:
    """Status info for a single camera."""
    name: str
    status: CameraStatus = CameraStatus.UNKNOWN
    last_seen: Optional[float] = None
    segment: int = 0
    pending_uploads: int = 0
    ntp_synced: bool = True  # Assume synced until proven otherwise
    disk_free_gb: Optional[float] = None
    disk_total_gb: Optional[float] = None
    disk_used_gb: Optional[float] = None
    error: Optional[str] = None
    upload_queue: List[dict] = field(default_factory=list)  # Per-session upload queue

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "last_seen": self.last_seen,
            "segment": self.segment,
            "pending_uploads": self.pending_uploads,
            "ntp_synced": self.ntp_synced,
            "disk_free_gb": self.disk_free_gb,
            "disk_total_gb": self.disk_total_gb,
            "disk_used_gb": self.disk_used_gb,
            "error": self.error,
            "upload_queue": self.upload_queue,
        }


@dataclass
class UploadProgress:
    """In-memory upload progress for a segment (for iOS visualization)."""
    camera: str
    uuid: str
    filename: str
    total_bytes: int
    received_bytes: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 0
        return (self.received_bytes / self.total_bytes) * 100

    @property
    def speed_bps(self) -> float:
        """Current upload speed in bytes per second."""
        elapsed = time.time() - self.started_at
        if elapsed <= 0:
            return 0
        return self.received_bytes / elapsed

    def to_dict(self) -> dict:
        return {
            "camera": self.camera,
            "uuid": self.uuid,
            "filename": self.filename,
            "total_bytes": self.total_bytes,
            "received_bytes": self.received_bytes,
            "percent": round(self.percent, 1),
            "speed_bps": round(self.speed_bps),
        }


@dataclass
class UploadStats:
    """Tracks upload speed for ETA calculation."""
    total_bytes: int = 0
    total_time: float = 0
    segment_count: int = 0

    @property
    def avg_speed_bps(self) -> float:
        """Average upload speed in bytes per second."""
        if self.total_time <= 0:
            return 0
        return self.total_bytes / self.total_time

    @property
    def avg_segment_size(self) -> int:
        """Average segment size in bytes."""
        if self.segment_count <= 0:
            return 0
        return self.total_bytes // self.segment_count

    def record_upload(self, size_bytes: int, duration_seconds: float):
        """Record a completed upload for speed calculation."""
        self.total_bytes += size_bytes
        self.total_time += duration_seconds
        self.segment_count += 1


@dataclass
class State:
    """Controller state - tracks sessions and cameras."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Session state
    session_state: SessionState = SessionState.IDLE
    session_uuid: Optional[str] = None
    session_start: Optional[float] = None

    # Camera tracking
    cameras: Dict[str, CameraInfo] = field(default_factory=dict)

    # Active uploads (in-memory for progress visualization)
    _uploads: Dict[str, UploadProgress] = field(default_factory=dict)

    # Segment counts per session
    _segments_received: Dict[str, Dict[str, int]] = field(default_factory=dict)

    # Upload speed stats (for ETA calculation)
    _upload_stats: UploadStats = field(default_factory=UploadStats)

    @property
    def is_recording(self) -> bool:
        return self.session_state == SessionState.RECORDING

    @property
    def is_idle(self) -> bool:
        return self.session_state == SessionState.IDLE

    @property
    def duration(self) -> int:
        if self.session_start and self.is_recording:
            return int(time.time() - self.session_start)
        return 0

    def set_recording(self, uuid: str):
        """Start a recording session."""
        with self._lock:
            self.session_state = SessionState.RECORDING
            self.session_uuid = uuid
            self.session_start = time.time()
            self._segments_received[uuid] = {}

    def set_idle(self):
        """Return to idle state."""
        with self._lock:
            self.session_state = SessionState.IDLE
            self.session_uuid = None
            self.session_start = None

    def set_error(self, error: str):
        """Set error state."""
        with self._lock:
            self.session_state = SessionState.ERROR

    # Camera tracking

    def update_camera(self, name: str, status: CameraStatus, **kwargs):
        """Update camera status."""
        with self._lock:
            if name not in self.cameras:
                self.cameras[name] = CameraInfo(name=name)

            cam = self.cameras[name]
            cam.status = status
            cam.last_seen = time.time()

            for key, value in kwargs.items():
                if hasattr(cam, key):
                    setattr(cam, key, value)

    def get_camera(self, name: str) -> Optional[CameraInfo]:
        """Get camera info."""
        return self.cameras.get(name)

    # Upload progress tracking (in-memory for iOS)

    def start_upload(self, camera: str, uuid: str, filename: str, total_bytes: int) -> str:
        """Track a new upload. Returns upload key."""
        key = f"{camera}/{uuid}/{filename}"
        with self._lock:
            self._uploads[key] = UploadProgress(
                camera=camera,
                uuid=uuid,
                filename=filename,
                total_bytes=total_bytes,
            )
        return key

    def update_upload(self, key: str, received_bytes: int):
        """Update upload progress."""
        with self._lock:
            if key in self._uploads:
                self._uploads[key].received_bytes = received_bytes

    def finish_upload(self, key: str):
        """Mark upload complete and record segment."""
        with self._lock:
            if key in self._uploads:
                upload = self._uploads.pop(key)

                # Record upload speed stats
                duration = time.time() - upload.started_at
                if duration > 0:
                    self._upload_stats.record_upload(upload.total_bytes, duration)

                # Track segment count
                if upload.uuid not in self._segments_received:
                    self._segments_received[upload.uuid] = {}
                segments = self._segments_received[upload.uuid]
                segments[upload.camera] = segments.get(upload.camera, 0) + 1

    def get_active_uploads(self) -> List[dict]:
        """Get all active upload progress for iOS."""
        with self._lock:
            return [u.to_dict() for u in self._uploads.values()]

    def get_session_segments(self, uuid: str) -> Dict[str, int]:
        """Get segment counts per camera for a session."""
        with self._lock:
            return dict(self._segments_received.get(uuid, {}))

    def get_upload_stats(self) -> dict:
        """Get upload speed stats for ETA calculation."""
        with self._lock:
            return {
                "avg_speed_bps": round(self._upload_stats.avg_speed_bps),
                "avg_segment_size": self._upload_stats.avg_segment_size,
                "segments_tracked": self._upload_stats.segment_count,
            }

    def estimate_eta_seconds(self, pending_segments: int) -> Optional[int]:
        """Estimate time to upload remaining segments."""
        with self._lock:
            if self._upload_stats.avg_speed_bps <= 0:
                return None
            if self._upload_stats.avg_segment_size <= 0:
                return None

            pending_bytes = pending_segments * self._upload_stats.avg_segment_size
            return int(pending_bytes / self._upload_stats.avg_speed_bps)

    # Export

    def to_dict(self) -> dict:
        """Export state as dictionary."""
        with self._lock:
            return {
                "state": self.session_state.value,
                "uuid": self.session_uuid,
                "duration": self.duration,
                "cameras": {name: cam.to_dict() for name, cam in self.cameras.items()},
            }


# Global state
_state: Optional[State] = None


def get_state() -> State:
    """Get global state instance."""
    global _state
    if _state is None:
        _state = State()
    return _state
