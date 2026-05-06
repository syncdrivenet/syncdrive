"""
Segment uploader process for camera node.
Watches for completed .h264 files and uploads to controller.
Runs independently of recorder.
"""

import json
import time
import signal
from pathlib import Path
from typing import Optional

import httpx

from config import get_config, init_config
from logger import setup_logging, log_info, log_error, log_warning


class ProgressTracker:
    """Track upload progress for metrics/UI."""

    def __init__(self, total_bytes: int, filename: str, uuid: str):
        self.total_bytes = total_bytes
        self.filename = filename
        self.uuid = uuid
        self.bytes_sent = 0
        self.start_time = time.time()
        self.last_logged_percent = 0

    def update(self, bytes_sent: int):
        self.bytes_sent = bytes_sent

    @property
    def percent(self) -> int:
        if self.total_bytes == 0:
            return 100
        return int((self.bytes_sent / self.total_bytes) * 100)

    @property
    def speed_bps(self) -> float:
        elapsed = time.time() - self.start_time
        if elapsed == 0:
            return 0
        return self.bytes_sent / elapsed

    @property
    def eta_seconds(self) -> float:
        if self.speed_bps == 0:
            return 0
        remaining = self.total_bytes - self.bytes_sent
        return remaining / self.speed_bps

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "uuid": self.uuid,
            "total_bytes": self.total_bytes,
            "bytes_sent": self.bytes_sent,
            "percent": self.percent,
            "speed_bps": int(self.speed_bps),
            "eta_seconds": int(self.eta_seconds),
        }

    def should_log(self) -> bool:
        """Check if we should log progress (every 25%)."""
        current = (self.percent // 25) * 25
        if current > self.last_logged_percent and current < 100:
            self.last_logged_percent = current
            return True
        return False


def format_bytes(b: int) -> str:
    """Format bytes as human readable."""
    if b < 1024:
        return f"{b}B"
    elif b < 1024 * 1024:
        return f"{b/1024:.1f}KB"
    else:
        return f"{b/1024/1024:.1f}MB"


def format_speed(bps: float) -> str:
    """Format bytes per second as human readable."""
    if bps < 1024:
        return f"{bps:.0f}B/s"
    elif bps < 1024 * 1024:
        return f"{bps/1024:.1f}KB/s"
    else:
        return f"{bps/1024/1024:.1f}MB/s"


class Uploader:
    """
    Background segment uploader.

    - Scans for completed *.h264 files (ignores _*.h264 in-progress)
    - Uploads via HTTP PUT with progress tracking
    - Logs progress at 25%, 50%, 75%
    - Deletes local file after confirmed upload
    - Retries with exponential backoff
    - Respects pause flag for safe HDD unmount
    """

    def __init__(self):
        self.config = get_config()
        self.client = httpx.Client(timeout=120.0)
        self._running = True
        self._backoff = self.config.sync.retry_delay
        self._progress: Optional[ProgressTracker] = None

        # State file for API to read
        self.state_file = Path("/data/cam/uploader_state.json")
        # Pause flag file - when present, uploads are paused
        self.pause_file = Path("/data/cam/upload_paused")

    @property
    def upload_url(self) -> str:
        return f"{self.config.controller.base_url}/api/segment"

    @property
    def is_paused(self) -> bool:
        """Check if uploads are paused."""
        return self.pause_file.exists()

    def _write_state(self, pending: int, uploading: str = None, progress: dict = None):
        """Write current state for API to read."""
        state = {
            "pending": pending,
            "uploading": uploading,
            "progress": progress,
            "paused": self.is_paused,
            "queue": self._get_queue_info(),
        }
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.rename(self.state_file)
        except Exception:
            pass  # Non-critical

    def _get_pending_segments(self) -> list:
        """Get all completed .h264 segments (not in-progress _*.h264)."""
        recordings_dir = self.config.recording.recordings_path
        if not recordings_dir.exists():
            return []

        # Find all .h264 files that don't start with underscore
        segments = []
        for f in recordings_dir.glob("**/seg_*.h264"):
            # Skip in-progress files (start with underscore)
            if not f.name.startswith("_"):
                segments.append(f)

        return sorted(segments)

    def _get_queue_info(self) -> list:
        """Get pending segments grouped by session with queue position."""
        from collections import defaultdict

        recordings_dir = self.config.recording.recordings_path
        if not recordings_dir.exists():
            return []

        # Group segments by session UUID (parent directory)
        pending = defaultdict(list)
        for f in recordings_dir.glob("**/seg_*.h264"):
            if not f.name.startswith("_"):
                session_uuid = f.parent.name
                pending[session_uuid].append(f.name)

        # Sort by UUID (matches current upload order)
        queue = []
        for position, uuid in enumerate(sorted(pending.keys()), 1):
            queue.append({
                "uuid": uuid,
                "pending": len(pending[uuid]),
                "position": position,
            })

        return queue

    def _cleanup_orphaned(self):
        """
        Orphaned _seg_* files are left intact for potential recovery.
        They contain partial video data from interrupted recordings.
        """
        pass  # Intentionally disabled - keep orphaned files for review

    def _cleanup_empty_dirs(self, path: Path):
        """Remove empty session directories."""
        try:
            parent = path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                log_info("uploader", f"Cleaned empty dir: {parent.name}")
        except Exception:
            pass

    def _wait_for_stable(self, filepath: Path, timeout: float = 2.0) -> bool:
        """
        Wait for file size to stabilize (no writes in progress).
        Prevents 'Too much data for Content-Length' errors.
        """
        last_size = -1
        checks = int(timeout / 0.1)
        for _ in range(checks):
            try:
                size = filepath.stat().st_size
                if size == last_size:
                    return True
                last_size = size
                time.sleep(0.1)
            except OSError:
                return False
        return False

    def _upload_segment(self, filepath: Path) -> bool:
        """
        Upload a single segment with progress tracking.
        Returns True on success.
        """
        uuid = filepath.parent.name
        filename = filepath.name
        file_size = filepath.stat().st_size

        url = f"{self.upload_url}/{self.config.node.name}/{uuid}/{filename}"

        # Initialize progress tracker
        self._progress = ProgressTracker(file_size, filename, uuid)

        log_info("uploader", f"Uploading {filename} ({format_bytes(file_size)})", uuid=uuid)

        try:
            # Read file in chunks and track progress
            def generate_with_progress():
                chunk_size = 65536  # 64KB chunks
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        self._progress.update(self._progress.bytes_sent + len(chunk))

                        # Log progress at 25% intervals
                        if self._progress.should_log():
                            log_info(
                                "uploader",
                                f"Progress {self._progress.percent}% "
                                f"({format_bytes(self._progress.bytes_sent)}) "
                                f"@ {format_speed(self._progress.speed_bps)}, "
                                f"ETA {self._progress.eta_seconds:.0f}s",
                                uuid=uuid,
                                filename=filename
                            )

                        # Update state file
                        self._write_state(
                            pending=0,  # Will be updated after
                            uploading=filename,
                            progress=self._progress.to_dict()
                        )

                        yield chunk

            response = self.client.put(
                url,
                content=generate_with_progress(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(file_size),
                },
            )

            if response.status_code == 200:
                elapsed = self._progress.elapsed_seconds
                speed = file_size / elapsed if elapsed > 0 else 0
                log_info(
                    "uploader",
                    f"Uploaded {filename} ({format_bytes(file_size)}) "
                    f"in {elapsed:.1f}s @ {format_speed(speed)}",
                    uuid=uuid
                )
                return True
            else:
                log_error(
                    "uploader",
                    f"Upload failed: HTTP {response.status_code}",
                    filename=filename,
                    uuid=uuid
                )
                return False

        except httpx.TimeoutException:
            log_error("uploader", "Upload timeout", filename=filename, uuid=uuid)
            return False
        except httpx.ConnectError:
            log_error("uploader", "Controller unreachable")
            return False
        except Exception as e:
            log_error("uploader", f"Upload error: {e}", filename=filename, uuid=uuid)
            return False
        finally:
            self._progress = None

    def run(self):
        """Main uploader loop."""
        log_info("uploader", "Uploader process started")

        cleanup_interval = 300  # Cleanup every 5 minutes
        last_cleanup = 0

        while self._running:
            # Check if paused (for safe HDD unmount)
            if self.is_paused:
                segments = self._get_pending_segments()
                self._write_state(pending=len(segments))
                time.sleep(2)  # Check every 2s while paused
                continue

            # Periodic cleanup
            if time.time() - last_cleanup > cleanup_interval:
                self._cleanup_orphaned()
                last_cleanup = time.time()

            # Get pending segments
            segments = self._get_pending_segments()
            self._write_state(pending=len(segments))

            if not segments:
                time.sleep(5)  # Poll every 5s when idle (segments are 2min)
                continue

            # Upload first segment (FIFO order)
            segment = segments[0]
            self._write_state(pending=len(segments), uploading=segment.name)

            # Wait for file to stabilize (ensures no writes in progress)
            if not self._wait_for_stable(segment):
                log_warning("uploader", f"File not stable, skipping: {segment.name}")
                time.sleep(1)
                continue

            if self._upload_segment(segment):
                # Success - delete local file
                try:
                    segment.unlink()
                    self._cleanup_empty_dirs(segment)
                except Exception as e:
                    log_warning("uploader", f"Failed to delete {segment}: {e}")

                # Reset backoff
                self._backoff = self.config.sync.retry_delay

            else:
                # Failed - exponential backoff
                log_warning("uploader", f"Retrying in {self._backoff}s")
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self.config.sync.max_retry_delay)

        self.client.close()

    def shutdown(self, signum, frame):
        """Handle shutdown signal."""
        log_info("uploader", "Shutdown signal received")
        self._running = False


def main():
    init_config()
    setup_logging()

    uploader = Uploader()

    signal.signal(signal.SIGTERM, uploader.shutdown)
    signal.signal(signal.SIGINT, uploader.shutdown)

    uploader.run()
    log_info("uploader", "Uploader process stopped")


if __name__ == "__main__":
    main()
