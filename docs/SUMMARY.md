# SyncDrive Camera System - Development Summary

## Overview

Multi-camera recording system using Raspberry Pi devices:
- **Camera nodes**: Pi Zero 2 W running `cam-v2` (recorder + uploader)
- **Controller node**: Pi 4 running `ctlr-v2` (API + storage)

## Architecture

```
Pi Zero 2 (cam-v2)          Pi 4 (ctlr-v2)
+------------------+        +------------------+
| recorder.py      |        | api.py           |
| - picamera2      |        | - FastAPI        |
| - H264 segments  |        | - Receives segs  |
| - SplittableOut  |------->| storage.py       |
+------------------+  HTTP  | - Writes to disk |
| uploader.py      |  PUT   +------------------+
| - Watches segs   |
| - HTTP upload    |
| - Progress logs  |
+------------------+
```

## Camera Node (cam-v2)

### recorder.py
- Uses `picamera2` with `PyavOutput` and `SplittableOutput` for **seamless segment recording**
- No frame gaps between segments (verified with frame analysis)
- Writes to `_seg_XXXX.h264` (in-progress), renames to `seg_XXXX.h264` when complete
- Captures session timestamps (start/stop) for metadata
- Configurable: resolution, fps, bitrate, segment duration

### uploader.py
- Watches for completed segments (ignores `_*.h264` in-progress files)
- HTTP PUT upload to controller with progress tracking
- Logs progress at 25%, 50%, 75%, 100%
- File stability check before upload (prevents Content-Length mismatch)
- Exponential backoff on failures
- Polls every 5 seconds when idle, continuous when queue has files

### Key Design Decisions
- **H264 format** (not MP4) - segments will be processed into final MP4 anyway
- **PyavOutput with `format="h264"`** - proven to work with SplittableOutput
- **Underscore prefix** for in-progress files - simple, filesystem-based signaling
- **HTTP over rsync** - cleaner integration, TCP handles integrity

## Controller Node (ctlr-v2)

### api.py (FastAPI)
- `PUT /api/segment/{camera}/{uuid}/{filename}` - receive segment uploads
- `GET /health` - health check
- `GET /status` - controller status
- `GET /storage` - storage status (disk usage)
- `GET /sessions` - list recorded sessions

### storage.py
- Receives segments, writes to `.tmp` first, atomic rename on completion
- Verifies Content-Length matches received bytes
- Mount verification for external storage
- Disk space checks before writing

## Configuration

### cam-v2/config.yml
```yaml
node:
  name: melb-02-cam-01

controller:
  host: melb-02-ctlr.local
  port: 8000

recording:
  width: 1920
  height: 1080
  fps: 30
  bitrate: 4000000
  segment_duration: 120  # 2 minutes

sync:
  retry_delay: 2
  max_retry_delay: 30
```

## Test Results

### 10-Minute Recording Test (2-min segments)
| Metric | Value |
|--------|-------|
| Segments | 6 (5 full + 1 partial) |
| Total frames | 17,988 |
| Duration | 599.6 seconds (~10 min) |
| File size | 286 MB |
| Upload speed | 10-17 MB/s |
| Frame gaps | **None** |

### Frame Analysis
- Consistent ~33.3ms intervals (30fps)
- No gaps at segment boundaries
- Seamless splitting verified at 30s, 60s, 90s, 120s boundaries

## File Structure

```
/Users/drogba/sync-build/
├── cam-v2/
│   ├── config.py       # Configuration loader
│   ├── logger.py       # Logging setup (JSON for Loki)
│   ├── recorder.py     # Camera recording with seamless segments
│   ├── uploader.py     # Segment upload with progress
│   ├── api.py          # Local status API
│   └── main.py         # Entry point
│
├── ctlr-v2/
│   ├── config.py       # Configuration loader
│   ├── logger.py       # Logging setup
│   ├── storage.py      # Segment storage
│   ├── api.py          # FastAPI endpoints
│   ├── state.py        # State management
│   ├── orchestrator.py # Camera orchestration
│   └── main.py         # Entry point
│
└── grafana-dashboard.json  # Monitoring dashboard
```

## Deployment

### Pi Zero 2 (Camera)
```bash
# Code location
/home/pi/cam-v2/

# Virtual environment
/home/pi/cam/venv/

# Config
/data/cam/config.yml

# Recordings (local, before upload)
/data/recordings/{uuid}/seg_XXXX.h264
```

### Pi 4 (Controller)
```bash
# Code location
/home/pi/ctlr-v2/

# Config
/data/ctlr/config.yml

# Start API
cd /home/pi/ctlr-v2
python3 -m uvicorn api:app --host 0.0.0.0 --port 8000
```

## Monitoring

- **Prometheus**: Node metrics via node_exporter
- **Loki**: Application logs via Fluent Bit
- **Grafana**: Dashboard at `grafana-dashboard.json`
  - CPU, Memory, Disk gauges
  - Temperature monitoring
  - Network traffic
  - Application logs

## Known Issues / Future Work

1. **Race condition on segment finalization**: Occasional "Too much data for Content-Length" error when uploader reads file size before PyavOutput fully flushes. Mitigated with stability check + retry.

2. **Temperature/throttling metrics**: Should be exposed via node_exporter, not application logs.

3. **Processing pipeline**: Need to add segment concatenation and final MP4 generation on controller.

4. **Multi-camera sync**: Orchestrator exists but not fully tested with multiple cameras.
