# SyncDrive

Multi-camera synchronized recording system for Raspberry Pi.

## Overview

```
                              SyncDrive System

   Pi Zero 2 W (x3)              Pi 4                     iOS App
   +--------------+            +--------------+          +--------------+
   |   Camera     |  HTTP PUT  |  Controller  |    WS    |    Discar    |
   |   Node       |----------->|              |<-------->|              |
   |              |  segments  |              |  status  |              |
   +--------------+            +--------------+          +--------------+
         |                           ^                        |
         |                           | TCP:9101               |
         |                    +--------------+                |
         |                    |    ESP32     |                |
         |                    |   CAN Bus    |                |
         |                    +--------------+                |
         |                           |                        |
         |                           v                        |
         |                    +--------------+                |
         |                    | External     |                |
         +----upload--------->| HDD          |<---sync--------+
                              | (ext4+exFAT) |   phone/watch
                              +--------------+
```

## Repository Structure

```
syncdrive/
├── cam/                 # Camera node (Pi Zero 2 W)
│   ├── recorder.py      # Video recording with picamera2
│   ├── uploader.py      # Segment upload to controller
│   ├── api.py           # HTTP status API
│   └── systemd/         # Service files
│
├── ctlr/                # Controller node (Pi 4)
│   ├── api.py           # HTTP + WebSocket API
│   ├── orchestrator.py  # Camera coordination
│   ├── storage.py       # Segment storage
│   ├── database.py      # SQLite tracking
│   └── systemd/         # Service files
│
├── ansible/             # Deployment automation
│   ├── roles/
│   │   ├── cam/         # Camera role
│   │   └── ctlr/        # Controller role
│   ├── inventory.ini    # Pi hostnames
│   └── site.yml         # Main playbook
│
├── docs/                # Documentation
└── grafana-dashboard.json
```

## Hardware

- **Controller**: Raspberry Pi 4 (4GB+) with external USB HDD
- **Cameras**: Raspberry Pi Zero 2 W with Camera Module 3
- **CAN Logger**: ESP32 with CAN transceiver (MCP2515 or SN65HVD230)
- **Storage**: External HDD with two partitions (ext4 + exFAT)
- **Network**: Local WiFi (5GHz recommended)
- **SD Cards**: 8GB+ (image is ~6.7GB, /data auto-expands)

## Flashing Camera SD Cards

### Prerequisites

A pre-built worker image (`worker-template.img.gz`) containing:
- Raspberry Pi OS Lite 32-bit (Debian Trixie)
- Partition layout: `/boot/firmware` (512MB) + `/` (6GB) + `/data` (auto-expand)
- WiFi credentials, SSH key, timezone pre-configured
- Auto-expand service for /data partition on first boot

### Flash a New Card

```bash
# 1. Insert blank SD card and confirm device
lsblk

# 2. Flash the image (adjust /dev/sdX to your device)
gunzip -c ~/worker-template.img.gz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sudo sync

# 3. Set unique hostname (required - avoids mDNS conflicts)
sudo ~/prep-worker-card.sh /dev/sdX melb-02-cam-03

# 4. Eject and boot
sudo eject /dev/sdX
```

**Time:** ~10 minutes per card including dd.

### First Boot Sequence

1. Cloud-init applies hostname, creates user, configures WiFi
2. `expand-data-partition.service` grows /data to fill card
3. System reboots once to apply hostname
4. Ready at `<hostname>.local` for SSH (~2-3 minutes total)

### After First Boot

Deploy SyncDrive software via Ansible:

```bash
cd syncdrive/ansible
ansible-playbook site.yml --limit melb-02-cam-03
```

## Quick Start

### Deploy with Ansible

```bash
# Clone repo
git clone https://github.com/syncdrivenet/syncdrive.git
cd syncdrive/ansible

# Edit inventory with your Pi hostnames
nano inventory.ini

# Deploy to all Pis
ansible-playbook site.yml

# Or deploy to specific host
ansible-playbook site.yml --limit melb-02-cam-01
```

### Manual Deployment (alternative)

```bash
# On your machine - sync code to Pi
rsync -avz --exclude '.git' --exclude 'venv' ./ pi@melb-02-cam-01.local:~/syncdrive/

# On the Pi - set up venv and services
ssh pi@melb-02-cam-01.local
cd ~/syncdrive/cam
python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syncdrive-recorder syncdrive-uploader syncdrive-cam-api
```

## Components

| Directory | Device | Services | Port |
|-----------|--------|----------|------|
| `cam/` | Pi Zero 2 W | recorder, uploader, api | 8080 |
| `ctlr/` | Pi 4 | syncdrive-ctlr | 8000 |
| - | Pi 4 | CAN listener (TCP) | 9101 |
| - | ESP32 | CAN bus logger | connects to 9101 |

See individual READMEs:
- [Camera Node](cam/README.md)
- [Controller Node](ctlr/README.md)
- [Ansible Deployment](ansible/README.md)
- [Network Setup](docs/NETWORK.md) - mDNS, Tailscale, ports
- [Image Build](docs/IMAGE_BUILD.md) - SD card image, flashing, first boot

## API Summary

### Controller (port 8000)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/preflight?client_time_ms=` | GET | Check cameras + storage + CAN ready |
| `/api/record/start?uuid=` | POST | Start synchronized recording |
| `/api/record/stop` | POST | Stop recording |
| `/api/status` | GET | System status |
| `/api/can/status` | GET | CAN bus connection status |
| `/api/storage/unmount` | POST | Safe eject HDD (pauses uploads + CAN) |
| `/api/storage/remount` | POST | Mount HDD (resumes uploads + CAN) |
| `/api/export/{uuid}` | POST | Export session to exFAT |
| `/api/shutdown/all` | POST | Safe system shutdown |
| `/ws/status` | WS | Real-time status updates |

### Camera (port 8080)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/preflight` | GET | Ready to record? |
| `/status` | GET | Current state |
| `/record/start` | POST | Start recording |
| `/record/stop` | POST | Stop recording |
| `/upload/pause` | POST | Pause uploads |
| `/upload/resume` | POST | Resume uploads |
| `/shutdown` | POST | Shutdown node |

## Overlay Filesystem (Power-Loss Protection)

Enable read-only root to protect SD card from corruption:

```bash
# Enable via raspi-config
sudo raspi-config
# → Performance → Overlay FS → Enable
# → Write-protect boot → Yes
# Reboot

# The data-remount service keeps /data writable
```

After enabling:
- `/` is read-only (protected)
- `/data` is read-write (recordings persist)
- Safe to power-pull during recording

## Time Synchronization (NTP)

Camera nodes use chrony to sync time from the controller. This is critical for synchronized multi-camera recording.

**Configuration** (`/etc/chrony/chrony.conf`):
```
server 192.168.8.145 iburst    # Sync to controller
makestep 1 -1                   # Step clock if >1s off (always)
```

**Why `makestep 1 -1`:**
- Pi Zero has no RTC (real-time clock hardware)
- On boot, clock may be minutes/hours behind
- `makestep 1 -1` = immediately jump to correct time if >1 second off
- Without this, chrony "slews" (gradually adjusts) which can take hours/days

**Manual time sync:**
```bash
# Force immediate time step
sudo chronyc makestep

# Check sync status
chronyc tracking
```

## License

MIT
