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
| `/api/export/{uuid}` | POST | Export to exFAT |

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

## Logs

```bash
journalctl -u syncdrive-ctlr -f
```
