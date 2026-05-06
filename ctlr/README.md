# Controller Node

Controller for multi-camera recording orchestration on Raspberry Pi 4.

## Overview

Orchestrates synchronized recording, receives video segments, provides real-time status to iOS app.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Controller (Pi 4)                                                          │
│                                                                              │
│  api.py + websocket.py          orchestrator.py         storage.py          │
│  - HTTP/WebSocket API           - Camera sync           - HDD management    │
│  - iOS app interface            - Start/stop all        - Segment storage   │
│  - Real-time status             - Health monitoring     - Export to exFAT   │
│                                                                              │
│  can_listener.py                                                            │
│  - TCP server (port 9101)                                                   │
│  - Receives CAN frames from ESP32                                           │
│  - Logs to /mnt/storage/sessions/{uuid}/can/                                │
│                                                                              │
│  /mnt/storage (ext4)  ◀── segments ──  Cameras (Pi Zero 2)                  │
│  /mnt/export (exFAT)  ──▶ Mac-readable exports                              │
│                        ◀── CAN data ──  ESP32 (TCP:9101)                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Services

| Service | Port | Protocol | Description |
|---------|------|----------|-------------|
| `syncdrive-ctlr` | 8000 | HTTP/WS | API + WebSocket |
| CAN Listener | 9101 | TCP | ESP32 CAN data |

## Installation

**Via Ansible (recommended):**
```bash
cd syncdrive/ansible
ansible-playbook site.yml --limit controllers
```

**Manual:**
```bash
# On Pi 4
git clone https://github.com/syncdrivenet/syncdrive.git ~/syncdrive

# Create config
sudo mkdir -p /data/ctlr /data/logs/ctlr /data/logs/ios /mnt/storage /mnt/export
sudo chown -R pi:pi /data
cp ~/syncdrive/ctlr/config.example.yml /data/ctlr/config.yml
nano /data/ctlr/config.yml

# Install services
sudo cp ~/syncdrive/ctlr/systemd/*.service ~/syncdrive/ctlr/systemd/*.mount /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mnt-storage.mount mnt-export.mount syncdrive-ctlr
```

## Configuration

`/data/ctlr/config.yml`:

```yaml
node:
  name: melb-02-ctlr

cameras:
  - name: melb-02-cam-01
    host: melb-02-cam-01.local
    port: 8080
  - name: melb-02-cam-02
    host: melb-02-cam-02.local
    port: 8080

storage:
  recordings_dir: /mnt/storage/sessions
  export_dir: /mnt/export

logging:
  dir: /data/logs/ctlr
  level: INFO

api:
  port: 8000
```

## API

All endpoints use `/api/` prefix (except `/health` and `/ws/status`).

### Health & WebSocket
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/ws/status` | WS | Real-time status (cameras, storage, system) |

### Status
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Controller status |
| `/api/cameras` | GET | All camera statuses |
| `/api/cameras/{name}` | GET | Single camera status |

### Recording
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/preflight?client_time_ms=` | GET | Check cameras + storage + CAN ready |
| `/api/record/start?uuid=` | POST | Start synchronized recording |
| `/api/record/stop` | POST | Stop recording |

**Preflight Response:**
```json
{
  "ready": true,
  "server_time_ms": 1714844321050,
  "client_time_ms": 1714844321000,
  "cameras": {
    "melb-02-cam-01": {"ready": true, "ntp_synced": true, "camera": true, "disk_ok": true}
  },
  "storage": {"ready": true, "mounted": true},
  "can": {"ready": true, "connected": true, "ntp_synced": true}
}
```

**Clock Sync Check (iOS):**
```
RTT = now - client_time_ms
Server time now = server_time_ms + (RTT / 2)
Clock offset = now - server_time_now
Warn if offset > 2 seconds
```

### Segments (from cameras)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/segment/{cam}/{uuid}/{file}` | PUT | Receive segment |
| `/api/uploads` | GET | Active upload progress |

### Storage
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/storage/status` | GET | HDD mount status |
| `/api/storage/remount` | POST | Mount partitions + resume camera uploads |
| `/api/storage/unmount` | POST | Pause camera uploads + safe eject |

### Sessions
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sessions` | GET | List sessions |
| `/api/sessions/{uuid}` | GET | Session info |
| `/api/sessions/{uuid}/stats` | GET | Session stats |
| `/api/sessions/{uuid}/segments` | GET | Session segments |
| `/api/sessions/unexported` | GET | List sessions not yet exported |

### Export
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/export/{uuid}` | POST | Export session to exFAT |
| `/api/export/sessions` | GET | List exported sessions |
| `/api/export/{uuid}` | DELETE | Delete from export partition |

### Sync Overview (for iOS)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sync/overview` | GET | Overall sync progress across all sessions |

**Sync Overview Response:**
```json
{
  "syncing": true,
  "recording": false,
  "recording_info": null,
  "synced": 45,
  "pending": 12,
  "expected": 57,
  "progress_percent": 78.9,
  "eta_seconds": 120,
  "speed_bps": 15000000,
  "sessions_pending": 2,
  "sessions": [
    {
      "uuid": "87C99460-...",
      "position": 1,
      "synced": 10,
      "pending": 5,
      "expected": 15,
      "progress_percent": 66.7,
      "eta_seconds": 60,
      "segments_ahead": 0
    }
  ]
}
```

**During Recording:**
```json
{
  "recording": true,
  "recording_info": {
    "uuid": "87C99460-...",
    "captured": 7,
    "synced": 5,
    "current_upload": {
      "camera": "melb-02-cam-01",
      "filename": "seg_0006.h264",
      "percent": 45
    }
  }
}
```

### Phone/Watch Sync (from iOS)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sync/phone/{uuid}/{file}` | PUT | Upload phone data |
| `/api/sync/watch/{uuid}/{file}` | PUT | Upload watch data |
| `/api/log` | POST | Receive iOS logs |

### CAN Bus
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/can/status` | GET | CAN bus connection + NTP status |

**CAN Status Response:**
```json
{
  "connected": true,
  "ntp_synced": true,
  "status": "recording"
}
```

Status values: `idle` | `recording` | `paused`

**ESP32 Protocol:**
- TCP connection to port 9101
- CSV format: `timestamp,can_id,length,hex_data\n`
- Example: `1714844321234,7E8,8,0102030405060708`
- Timestamps in milliseconds (Unix epoch)
- Sends header `ts,id,len,data` on connect

### System Shutdown
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/shutdown/all` | POST | Safely shut down entire system |

**Shutdown sequence:**
1. Rejects if recording in progress (409 error)
2. Pauses uploads on all cameras
3. Sends shutdown to all cameras
4. Waits for cameras to go offline (max 15s)
5. Unmounts storage
6. Shuts down controller

## Storage Layout

```
/mnt/storage/sessions/     (ext4 - journaled, active recording)
└── {uuid}/
    ├── melb-02-cam-01/
    │   └── seg_*.h264
    ├── melb-02-cam-02/
    │   └── seg_*.h264
    └── can/
        └── can_log.csv    (CAN bus data)

/mnt/export/               (exFAT - Mac readable)
└── {uuid}/                (exported sessions)
    ├── melb-02-cam-01/
    ├── can/
    ├── phone/             (uploaded after "Process" in iOS)
    │   └── motion.csv
    └── watch/
        └── heartrate.csv

/data/ctlr/
├── config.yml
└── sessions.db            (SQLite)
```

**Note:** Phone/watch data goes directly to export partition (exFAT) since it's only uploaded after pressing "Process" in the iOS app.

## WebSocket Messages

The `/ws/status` endpoint provides real-time updates to the iOS app.

### Connection Flow
1. Client connects → receives `initial` message with full state
2. Server sends updates as `camera`, `controller`, `storage`, etc.
3. Client sends `ping` → server responds with `pong`

### Message Types

| Type | Direction | Description |
|------|-----------|-------------|
| `initial` | → client | Full state on connect |
| `controller` | → client | Recording state changed |
| `camera` | → client | Single camera update |
| `cameras` | → client | All cameras update |
| `storage` | → client | Storage status changed |
| `system` | → client | CPU/RAM/temp metrics |
| `upload_progress` | → client | Real-time segment upload progress |
| `sync` | → client | Camera sync progress |
| `phone_sync` | → client | Phone data upload status |
| `watch_sync` | → client | Watch data upload status |
| `ping` | ↔ | Keepalive |
| `pong` | → client | Response to ping |

### Camera Message
```json
{
  "type": "camera",
  "data": {
    "name": "melb-02-cam-01",
    "connected": true,
    "state": "recording",
    "ntp_synced": true,
    "segment": 3,
    "disk_free_gb": 7.5,
    "disk_total_gb": 8.0,
    "disk_used_gb": 0.5,
    "sync_segments_queued": 2
  }
}
```

### Upload Progress Message
```json
{
  "type": "upload_progress",
  "data": {
    "camera": "melb-02-cam-01",
    "uuid": "87C99460-...",
    "filename": "seg_0001.h264",
    "bytes_received": 12582912,
    "total_bytes": 25165824,
    "percent": 50
  }
}
```

Broadcasts at 0%, 10%, 20%... 100% during segment upload.

### Initial Message Structure
```json
{
  "type": "initial",
  "data": {
    "controller": {"ready": true, "recording": false, "uuid": null, "duration": 0},
    "cameras": [...],
    "system": {"cpu_percent": 12.5, "mem_percent": 45.2, "temp_c": 52.0},
    "storage": {"healthy": true, "logging": {...}, "sync": {...}},
    "can": {"connected": true, "ntp_synced": true, "status": "idle"}
  }
}
```

## Logs

**Local:**
```bash
journalctl -u syncdrive-ctlr -f
```

**Centralized (Grafana/Loki):**

Logs are forwarded to a central monitoring server via fluent-bit:

```
Camera (rsyslog) → Controller (fluent-bit:5514) → Loki (syncdrivev2:3100) → Grafana
```

- **Grafana URL:** `http://<syncdrivev2-tailscale-ip>:3000`
- **Dashboard:** SyncDrive Overview
- **Panels:** Camera Logs, Controller Logs, System Logs, Errors & Warnings

Log queries in Loki:
```
# Camera logs (human-readable)
{ident=~"syncdrive-recorder|syncdrive-uploader"} | json | line_format "{{.message}}"

# Controller logs
{node="melb-02-ctlr"} |~ "ctlr\\." | json | line_format "{{.MESSAGE}}"

# All errors
{job="syncdrive"} |~ "(?i)(error|fail|exception)"
```
