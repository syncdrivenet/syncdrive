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
│  /mnt/storage (ext4)  ◀── segments ──  Cameras (Pi Zero 2)                  │
│  /mnt/export (exFAT)  ──▶ Mac-readable exports                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Not Yet Implemented

| Feature | Status | Description |
|---------|--------|-------------|
| **CAN Bus** | Placeholder | `/api/can/status` returns stub data |
| **External SSD** | Planned | USB SSD storage for sessions |

## Service

| Service | Port | Description |
|---------|------|-------------|
| `syncdrive-ctlr` | 8000 | HTTP API + WebSocket |

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
| `/api/preflight` | GET | Check cameras + storage ready |
| `/api/record/start?uuid=` | POST | Start synchronized recording |
| `/api/record/stop` | POST | Stop recording |

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
| `/api/export/{uuid}` | POST | Export to exFAT |

### Phone/Watch Sync (from iOS)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sync/phone/{uuid}/{file}` | PUT | Upload phone data |
| `/api/sync/watch/{uuid}/{file}` | PUT | Upload watch data |
| `/api/log` | POST | Receive iOS logs |

### CAN Bus (Not Implemented)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/can/status` | GET | CAN bus status (placeholder) |

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
/mnt/storage/sessions/     (ext4 - journaled)
└── {uuid}/
    ├── melb-02-cam-01/
    │   └── seg_*.h264
    ├── phone/
    │   └── motion.json
    └── watch/
        └── heartrate.json

/mnt/export/               (exFAT - Mac readable)
└── {uuid}/                (exported sessions)

/data/ctlr/
├── config.yml
└── sessions.db            (SQLite)
```

## Logs

```bash
journalctl -u syncdrive-ctlr -f
```
