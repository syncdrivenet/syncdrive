# SyncDrive System Architecture

## Overview

A synchronized multi-camera recording system for vehicle data collection.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              MOBILE (iPhone + Watch)                             │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │  Discar App                                                              │    │
│  │  - UI for start/stop recording                                          │    │
│  │  - Phone sensors (GPS, accelerometer, gyro)                             │    │
│  │  - Watch sensors (heart rate, motion)                                   │    │
│  │  - Session management                                                    │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────┬────────────────────────────────────────────┘
                                     │ HTTP/WebSocket
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           CONTROLLER (Pi 4 - melb-02-ctlr)                       │
│                                                                                  │
│  ┌──────────────────────┐    ┌──────────────────────┐    ┌─────────────────┐   │
│  │  api.py (FastAPI)    │    │  Background Services │    │  Storage        │   │
│  │  :8000               │    │                      │    │                 │   │
│  │                      │    │  - health_monitor    │    │  /mnt/logging/  │   │
│  │  /api/record/start   │    │  - log_subscriber    │    │  /mnt/sync/     │   │
│  │  /api/record/stop    │    │  - mount_watcher     │    │                 │   │
│  │  /api/status         │    │                      │    │                 │   │
│  │  /api/sync/phone     │    └──────────┬───────────┘    └────────▲────────┘   │
│  └──────────┬───────────┘               │                         │            │
│             │                           │ MQTT                    │ rsync      │
│             │ HTTP                      ▼                         │            │
│             │              ┌──────────────────────┐               │            │
│             │              │  Mosquitto MQTT      │               │            │
│             │              │  localhost:1883      │               │            │
│             │              └──────────┬───────────┘               │            │
│             │                         │                           │            │
│             │                         ▼                           │            │
│             │              ┌──────────────────────┐               │            │
│             │              │  log_subscriber.py   │───────────────┼──▶ Loki    │
│             │              │  SQLite buffer       │               │   (remote) │
│             │              └──────────────────────┘               │            │
└─────────────┼─────────────────────────────────────────────────────┼────────────┘
              │ HTTP :8080                                          │
              ▼                                                     │
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        CAMERA NODES (Pi Zero 2 x 3)                              │
│                                                                                  │
│  ┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐     │
│  │  melb-02-cam-01     │  │  melb-02-cam-02     │  │  melb-02-cam-03     │     │
│  │                     │  │                     │  │                     │     │
│  │  FastAPI :8080      │  │  FastAPI :8080      │  │  FastAPI :8080      │     │
│  │  /preflight         │  │  /preflight         │  │  /preflight         │     │
│  │  /record/start      │  │  /record/start      │  │  /record/start      │     │
│  │  /record/stop       │  │  /record/stop       │  │  /record/stop       │     │
│  │  /status            │  │  /status            │  │  /status            │     │
│  │                     │  │                     │  │                     │     │
│  │  Event Loop:        │  │  Event Loop:        │  │  Event Loop:        │     │
│  │  - SEGMENT_FINISHED │  │  - SEGMENT_FINISHED │  │  - SEGMENT_FINISHED │     │
│  │  - triggers rsync   │  │  - triggers rsync   │  │  - triggers rsync   │     │
│  └─────────────────────┘  └─────────────────────┘  └─────────────────────┘     │
│             │                       │                       │                   │
│             └───────────────────────┼───────────────────────┘                   │
│                                     │ rsync (background)                        │
│                                     ▼                                           │
│                         melb-02-ctlr:/mnt/logging/{cam}/{uuid}/seg_*.mp4       │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Recording Flow

### 1. User Taps "Record" in Discar App

```
iPhone                          Controller                      Camera Nodes
   │                                │                                │
   │  POST /api/record/start        │                                │
   │  {uuid: "abc-123..."}          │                                │
   │ ──────────────────────────────▶│                                │
   │                                │                                │
   │                                │  GET /preflight (parallel)     │
   │                                │ ──────────────────────────────▶│
   │                                │◀────────────── {ready: true}   │
   │                                │                                │
   │                                │  POST /record/start            │
   │                                │  {uuid, start_at: now+6s}      │
   │                                │ ──────────────────────────────▶│
   │                                │                                │
   │                                │  db.insert_session(uuid)       │
   │                                │                                │
   │◀─────────────── {success: true, uuid}                           │
   │                                │                                │
   │  Start local sensors           │                                │
   │  Start watch recording         │                                │
   │                                │                                │
   │                                │            [synchronized start at start_at]
   │                                │                                │
   │                                │                          ┌─────┴─────┐
   │                                │                          │ Recording │
   │                                │                          │ 2min segs │
   │                                │                          └─────┬─────┘
   │                                │                                │
   │                                │      rsync seg_001.mp4         │
   │                                │◀───────────────────────────────│
   │                                │                                │
```

### 2. Cameras Record Segments

Each camera:
1. Waits until `start_at` timestamp (synchronized start)
2. Records 120-second H.264 segments via `rpicam-vid`
3. On `SEGMENT_FINISHED` event, triggers background rsync to controller
4. Controller receives segments at `/mnt/logging/{camera_name}/{uuid}/seg_*.mp4`

### 3. User Taps "Stop"

```
iPhone                          Controller                      Camera Nodes
   │                                │                                │
   │  POST /api/record/stop         │                                │
   │ ──────────────────────────────▶│                                │
   │                                │  POST /record/stop (parallel)  │
   │                                │ ──────────────────────────────▶│
   │                                │                                │
   │                                │  db.update_session_stop(uuid)  │
   │                                │                                │
   │◀─────────────── {success, duration}                             │
   │                                │                                │
   │  Stop local sensors            │                                │
   │  Stop watch recording          │                                │
```

### 4. Phone Uploads Sensor Data

```
iPhone                          Controller
   │                                │
   │  Wait for camera sync...       │
   │  (poll /api/sync/status)       │
   │                                │
   │  POST /api/sync/phone          │
   │  Form: uuid + CSV files        │
   │ ──────────────────────────────▶│
   │                                │
   │                                │  Save to /mnt/logging/phone/{uuid}/
   │                                │  Trigger postprocess.py
   │                                │
   │◀─────────────── {success, processing: true}
```

### 5. Post-Processing

```
postprocess.py --uuid {uuid}
    │
    ├── Find all sources:
    │   - /mnt/logging/melb-02-cam-01/{uuid}/seg_*.mp4
    │   - /mnt/logging/melb-02-cam-02/{uuid}/seg_*.mp4
    │   - /mnt/logging/melb-02-cam-03/{uuid}/seg_*.mp4
    │   - /mnt/logging/phone/{uuid}/*.csv
    │   - /mnt/logging/phone/{uuid}/watch/*.csv
    │   - /mnt/logging/can/raw.csv (extract time range)
    │
    ├── Concatenate video segments per camera:
    │   ffmpeg -f concat → {camera}.mp4
    │
    ├── Extract CAN data for session time range
    │
    └── Output to /mnt/sync/{date}_{uuid[:6]}/
        ├── melb-02-cam-01.mp4
        ├── melb-02-cam-02.mp4
        ├── melb-02-cam-03.mp4
        ├── phone/*.csv
        ├── watch/*.csv
        ├── can_raw.csv
        └── manifest.json
```

---

## Background Services

### Current State (Fragile)

| Service | Purpose | Problems |
|---------|---------|----------|
| `health_monitor.py` | System metrics → MQTT | Runs via cron? No restart on fail |
| `log_subscriber.py` | MQTT → SQLite → Loki | No MQTT reconnect, watchdog exits, nothing restarts it |
| `mount_watcher.py` | Monitor storage mounts | Uses subprocess for MQTT |
| `api.py` threads | Health + camera polling | Daemon threads, no supervision |

### Logging Pipeline

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│  Any Service    │      │  log_subscriber │      │    Remote       │
│                 │      │                 │      │                 │
│  log("x", msg)  │─────▶│  MQTT subscribe │      │                 │
│       │         │ MQTT │       │         │      │                 │
│       ▼         │      │       ▼         │      │                 │
│  mosquitto_pub  │      │  SQLite buffer  │─────▶│      Loki       │
│                 │      │  (pending/sent) │ HTTP │                 │
└─────────────────┘      │       │         │      │                 │
                         │  [offline: queue]│      │                 │
                         │  [online: flush] │      │                 │
                         └─────────────────┘      └─────────────────┘
```

**Why it crashes:**
1. `client.connect()` is one-shot - MQTT broker restart = subscriber dies
2. Watchdog calls `sys.exit(1)` after 2min silence, but no systemd to restart
3. Blocking Loki push in main loop - if Loki slow, MQTT backs up

---

## Data Storage

```
/mnt/logging/                          # Main recording drive (ext4)
├── melb-02-cam-01/
│   └── {uuid}/
│       ├── seg_001.mp4
│       ├── seg_002.mp4
│       └── ...
├── melb-02-cam-02/
│   └── {uuid}/...
├── melb-02-cam-03/
│   └── {uuid}/...
├── phone/
│   └── {uuid}/
│       ├── gps.csv
│       ├── accelerometer.csv
│       └── watch/
│           ├── heart_rate.csv
│           └── motion.csv
├── can/
│   └── raw.csv                        # Continuous CAN log
└── logs/
    └── logs.db                        # SQLite log buffer

/mnt/sync/                             # Export drive (exfat, removable)
└── 2026-04-30_abc123/                 # Processed session
    ├── melb-02-cam-01.mp4
    ├── melb-02-cam-02.mp4
    ├── melb-02-cam-03.mp4
    ├── phone/
    ├── watch/
    ├── can_raw.csv
    └── manifest.json
```

---

## API Endpoints

### Controller (api.py :8000)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/status` | GET | System status, camera states, storage |
| `/api/record/start?uuid=` | POST | Start synchronized recording |
| `/api/record/stop` | POST | Stop recording |
| `/api/sync/phone` | POST | Upload phone sensor data |
| `/api/sync/status` | GET | Camera sync progress |
| `/api/sync/report` | POST | Camera reports sync status |
| `/api/storage/status` | GET | Mount health |
| `/api/storage/eject` | POST | Safe drive removal |
| `/api/storage/mount` | POST | Re-enable after insert |
| `/api/sessions` | GET | List recorded sessions |
| `/api/log` | POST | Receive logs from iOS |
| `/health` | GET | Health check |

### Camera Nodes (cam :8080)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/preflight` | GET | Check camera ready |
| `/record/start` | POST | Start recording {uuid, start_at} |
| `/record/stop` | POST | Stop recording |
| `/status` | GET | Current state, segment count, system stats |

---

## Code Issues Summary

### 1. Hardcoded Config (Multiple Files)

```python
# lib/logger.py
NODE = "melb-01-ctlr"          # Wrong! Now melb-02

# config.py
NODES = ["melb-01-cam-01:8080"...]  # Old nodes

# mount_watcher.py
NODE = "melb-01-ctlr"          # Duplicated
MOUNTS = {...}                 # Hardcoded devices

# log_subscriber.py
LOKI_URL = "http://100.71.5.101:3100/..."  # Hardcoded IP
```

### 2. Inconsistent Response Handling

```python
# orchestrator.py checks multiple formats:
r.get("ok")                    # Some use this
r.get("success")               # Others use this
r.get("data", {}).get("ready") # And this
```

### 3. No Process Supervision

- Scripts run manually or basic cron
- No systemd = no auto-restart
- No dependency ordering

### 4. Fragile MQTT

```python
# log_subscriber.py
client.connect("localhost", 1883)  # One-shot, no reconnect
# If broker restarts → dies

# lib/logger.py
subprocess.run(["mosquitto_pub"...])  # Spawns process per log
```

---

## Refactor Priorities

### 1. Single Config File
```yaml
# config.yml
node_name: melb-02-ctlr
cameras:
  - host: melb-02-cam-01.local
    port: 8080
mqtt:
  broker: localhost
  port: 1883
loki:
  url: http://100.71.5.101:3100/loki/api/v1/push
storage:
  logging: /mnt/logging
  sync: /mnt/sync
```

### 2. Robust Log Collector
- MQTT auto-reconnect
- Non-blocking Loki push
- Systemd service with restart

### 3. Systemd Services
```
ctlr-api.service        # FastAPI server
ctlr-logs.service       # Log collector (MQTT→SQLite→Loki)
ctlr-health.timer       # Health metrics every 30s
```

### 4. Consistent API Responses
```python
# Always return:
{"success": bool, "data": {...}, "error": str|None}
```
