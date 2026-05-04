"""
Video recorder process for camera node.
Records H264 segments using picamera2 with seamless splitting (no gaps).
Controlled via simple file-based commands.
"""

import json
import time
import signal
from pathlib import Path
from typing import Optional

from config import get_config, init_config
from logger import setup_logging, log_info, log_error, log_warning

# picamera2 - only available on Raspberry Pi
try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import PyavOutput, SplittableOutput
    PICAMERA_AVAILABLE = True
except ImportError:
    PICAMERA_AVAILABLE = False


class Recorder:
    """
    Video recorder using picamera2 with seamless segment splitting.

    Uses SplittableOutput for gap-free recording across segments.
    Each segment:
    1. Records to _seg_XXXX.h264 (underscore = in progress)
    2. On split, renames to seg_XXXX.h264 (complete)
    3. Uploader process handles upload (separate process)

    Controlled via command files:
    - /data/cam/cmd/start:{uuid}  → start recording
    - /data/cam/cmd/stop          → stop recording
    """

    def __init__(self):
        self.config = get_config()
        self.camera: Optional[Picamera2] = None
        self.encoder: Optional[H264Encoder] = None
        self.splitter: Optional[SplittableOutput] = None
        self._running = True
        self._recording = False
        self._uuid: Optional[str] = None
        self._segment = 0
        self._segment_start: Optional[float] = None
        self._current_path: Optional[Path] = None

        # Session timestamps (start/stop only)
        self._session_start_ts: Optional[float] = None
        self._session_stop_ts: Optional[float] = None

        # Command directory
        self.cmd_dir = Path("/data/cam/cmd")
        self.cmd_dir.mkdir(parents=True, exist_ok=True)

        # State file for API to read
        self.state_file = Path("/data/cam/state.json")

    def _write_state(self):
        """Write current state for API to read."""
        state = {
            "recording": self._recording,
            "uuid": self._uuid,
            "segment": self._segment,
            "duration": int(time.time() - self._session_start_ts) if self._session_start_ts else 0,
            "camera_available": PICAMERA_AVAILABLE,
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.rename(self.state_file)

    def _write_session_metadata(self):
        """Write session metadata after recording stops."""
        if not self._uuid or not self._session_start_ts:
            return

        session_dir = self.config.recording.recordings_path / self._uuid
        meta_file = session_dir / "session.json"

        metadata = {
            "uuid": self._uuid,
            "camera": self.config.node.name,
            "start_ts": self._session_start_ts,
            "stop_ts": self._session_stop_ts,
            "duration_sec": round(self._session_stop_ts - self._session_start_ts, 3),
            "segments": self._segment,
            "segment_duration": self.config.recording.segment_duration,
            "fps": self.config.recording.fps,
            "width": self.config.recording.width,
            "height": self.config.recording.height,
        }

        try:
            meta_file.write_text(json.dumps(metadata, indent=2))
            log_info("recorder", f"Session metadata saved", uuid=self._uuid)
        except Exception as e:
            log_error("recorder", f"Failed to save metadata: {e}", uuid=self._uuid)

    def _get_segment_path(self, uuid: str, segment: int, in_progress: bool = False) -> Path:
        """Get path for a segment file."""
        session_dir = self.config.recording.recordings_path / uuid
        session_dir.mkdir(parents=True, exist_ok=True)
        # Underscore prefix for in-progress, no prefix for complete
        prefix = "_" if in_progress else ""
        return session_dir / f"{prefix}seg_{segment:04d}.h264"

    def _init_camera(self):
        """Initialize the camera (does not start recording)."""
        if not PICAMERA_AVAILABLE:
            raise RuntimeError("picamera2 not available")

        if self.camera is None:
            self.camera = Picamera2()
            video_config = self.camera.create_video_configuration(
                main={
                    "size": (self.config.recording.width, self.config.recording.height),
                    "format": "RGB888",
                },
                controls={"FrameRate": self.config.recording.fps},
            )
            self.camera.configure(video_config)
            self.encoder = H264Encoder(bitrate=self.config.recording.bitrate)
            log_info("recorder", "Camera initialized")

    def _close_camera(self):
        """Close the camera."""
        if self.camera:
            try:
                if self._recording:
                    self.camera.stop_recording()
                self.camera.stop()
                self.camera.close()
            except Exception as e:
                log_warning("recorder", f"Error closing camera: {e}")
            finally:
                self.camera = None
                self.encoder = None
                self.splitter = None

    def _check_commands(self) -> Optional[tuple]:
        """Check for command files. Returns (cmd, args) or None."""
        try:
            for cmd_file in self.cmd_dir.iterdir():
                if cmd_file.is_file():
                    name = cmd_file.name
                    cmd_file.unlink()  # Consume command

                    if name.startswith("start:"):
                        # Format: start:{uuid} or start:{uuid}:{start_at}
                        parts = name.split(":")
                        uuid = parts[1]
                        start_at = int(parts[2]) if len(parts) > 2 else None
                        return ("start", {"uuid": uuid, "start_at": start_at})
                    elif name == "stop":
                        return ("stop", None)
        except Exception:
            pass
        return None

    def _finalize_segment(self, segment: int):
        """Rename in-progress segment to complete."""
        if self._current_path and self._current_path.exists():
            final_path = self._get_segment_path(self._uuid, segment, in_progress=False)
            self._current_path.rename(final_path)
            log_info("recorder", f"Segment {segment} complete", uuid=self._uuid)

    def _start_recording(self, uuid: str):
        """Start recording session with first segment."""
        self._segment = 1
        self._current_path = self._get_segment_path(uuid, self._segment, in_progress=True)
        self._segment_start = time.time()

        # Create splittable output for seamless segment switching (PyavOutput with H264 format)
        initial_output = PyavOutput(str(self._current_path), format="h264")
        self.splitter = SplittableOutput(output=initial_output)

        # Capture session start timestamp RIGHT BEFORE recording
        self._session_start_ts = time.time()

        # Start recording (never stops until session ends)
        self.camera.start_recording(self.encoder, self.splitter)

        log_info("recorder", f"Recording segment {self._segment}", uuid=uuid)

    def _split_to_next_segment(self):
        """Seamlessly split to next segment."""
        # Finalize current segment (rename)
        self._finalize_segment(self._segment)

        # Start next segment
        self._segment += 1
        self._current_path = self._get_segment_path(self._uuid, self._segment, in_progress=True)
        self._segment_start = time.time()

        # Seamless split - no recording interruption (PyavOutput with H264 format)
        new_output = PyavOutput(str(self._current_path), format="h264")
        self.splitter.split_output(new_output)

        log_info("recorder", f"Recording segment {self._segment}", uuid=self._uuid)

    def _stop_recording(self):
        """Stop recording and finalize last segment."""
        # Capture session stop timestamp RIGHT AFTER stopping
        self.camera.stop_recording()
        self._session_stop_ts = time.time()

        # Finalize the last segment
        if self._current_path and self._current_path.exists():
            final_path = self._get_segment_path(self._uuid, self._segment, in_progress=False)
            self._current_path.rename(final_path)
            log_info("recorder", f"Segment {self._segment} saved (final)")

        # Write session metadata
        self._write_session_metadata()

        self.splitter = None

    def run(self):
        """Main recorder loop with seamless segment splitting."""
        log_info("recorder", "Recorder process started")
        self._write_state()

        segment_duration = self.config.recording.segment_duration

        while self._running:
            cmd = self._check_commands()

            # Handle stop command (check first - higher priority)
            if cmd and cmd[0] == "stop":
                if self._recording:
                    log_info("recorder", "Stop command received")
                    self._stop_recording()
                    self._recording = False
                    log_info("recorder", f"Recording stopped: {self._segment} segments", uuid=self._uuid)
                    self._uuid = None
                    self._session_start_ts = None
                    self._session_stop_ts = None
                    self._close_camera()
                    self._write_state()
                else:
                    log_warning("recorder", "Stop command received but not recording")
                time.sleep(0.1)
                continue

            # Handle start command
            if cmd and cmd[0] == "start" and not self._recording:
                args = cmd[1]
                uuid = args["uuid"]
                start_at = args.get("start_at")

                log_info("recorder", "Starting recording", uuid=uuid)

                # Wait for synchronized start time if specified
                if start_at:
                    now_ms = int(time.time() * 1000)
                    wait_ms = start_at - now_ms
                    if wait_ms > 0:
                        log_info("recorder", f"Waiting {wait_ms}ms for sync start", uuid=uuid)
                        time.sleep(wait_ms / 1000)
                    else:
                        log_warning("recorder", f"start_at already passed by {-wait_ms}ms", uuid=uuid)

                try:
                    self._init_camera()
                    self._uuid = uuid
                    self._start_recording(uuid)
                    self._recording = True
                    self._write_state()

                except Exception as e:
                    log_error("recorder", f"Failed to start recording: {e}", uuid=uuid)
                    self._close_camera()
                    continue

            # While recording: check for segment splits
            if self._recording:
                # Check if segment duration reached - seamless split
                if time.time() - self._segment_start >= segment_duration:
                    self._split_to_next_segment()
                    self._write_state()

            time.sleep(0.1)  # Fast polling for responsive segment splits

    def shutdown(self, signum, frame):
        """Handle shutdown signal."""
        log_info("recorder", "Shutdown signal received")
        self._running = False
        if self._recording:
            self._stop_recording()
            self._recording = False


def main():
    init_config()
    setup_logging()

    recorder = Recorder()

    signal.signal(signal.SIGTERM, recorder.shutdown)
    signal.signal(signal.SIGINT, recorder.shutdown)

    recorder.run()
    log_info("recorder", "Recorder process stopped")


if __name__ == "__main__":
    main()
