# Camera Node

Camera recording node for Raspberry Pi Zero 2 W.

## Overview

Records video segments using picamera2 and uploads to controller via HTTP.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Camera Node (Pi Zero 2) - Multi-Process Architecture                   │
│                                                                          │
│  recorder.py          uploader.py          api.py                       │
│  - Records segments   - Watches .h264      - HTTP endpoints             │
│  - picamera2          - HTTP PUT upload    - Status/control             │
│  - 2-min segments     - Auto-retry         - Port 8080                  │
│                                                                          │
│  /data/recordings/{uuid}/seg_*.h264 ──────▶ Controller (Pi 4)           │
└─────────────────────────────────────────────────────────────────────────┘
```

## Services

| Service | Description |
|---------|-------------|
| `syncdrive-recorder` | Records video segments |
| `syncdrive-uploader` | Uploads to controller |
| `syncdrive-cam-api` | HTTP status API |

## Installation

**Via Ansible (recommended):**
```bash
cd syncdrive/ansible
ansible-playbook site.yml --limit cameras
```

**Manual:**
```bash
# On Pi Zero 2
git clone https://github.com/syncdrivenet/syncdrive.git ~/syncdrive
cd ~/syncdrive/cam

# Create venv (--system-site-packages for picamera2)
python3 -m venv --system-site-packages venv
./venv/bin/pip install -r requirements.txt

# Create config
sudo mkdir -p /data/cam/cmd /data/logs/cam /data/recordings
sudo chown -R pi:pi /data
cp config.example.yml /data/cam/config.yml
nano /data/cam/config.yml

# Install services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syncdrive-recorder syncdrive-uploader syncdrive-cam-api
```

## Configuration

`/data/cam/config.yml`:

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
  segment_duration: 120

logging:
  dir: /data/logs/cam
  level: INFO

api:
  port: 8080
```

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/preflight` | GET | Ready to record? |
| `/status` | GET | Current state |
| `/record/start` | POST | Start recording |
| `/record/stop` | POST | Stop recording |
| `/upload/pause` | POST | Pause segment uploads (for safe HDD unmount) |
| `/upload/resume` | POST | Resume segment uploads |
| `/upload/status` | GET | Upload status including pause state |
| `/shutdown` | POST | Safely shut down this camera node |

## Data Flow

1. Controller sends `POST /record/start?uuid=xxx`
2. Recorder writes `seg_0001.h264.tmp` → renames to `.h264` when done
3. Uploader detects `.h264`, uploads via `PUT /api/segment/{cam}/{uuid}/{file}`
4. Deletes after successful upload

## Reliability

| Scenario | Behavior |
|----------|----------|
| Power loss during recording | .tmp lost, previous .h264 safe |
| Controller offline | Files queue on disk, retry forever |
| Recorder crashes | Uploader keeps uploading |
| Uploader crashes | Recorder keeps recording |

## Overlay FS (Power-Loss Protection)

Enable read-only root filesystem to protect SD card from corruption:

```bash
# 1. Create /data partition (if not exists)
sudo fdisk /dev/mmcblk0   # create partition 3
sudo mkfs.ext4 /dev/mmcblk0p3
echo 'LABEL=data /data ext4 defaults,noatime,nofail 0 2' | sudo tee -a /etc/fstab

# 2. Enable overlay via raspi-config
sudo raspi-config
# → Performance → Overlay FS → Enable
# → Write-protect boot → Yes
# Reboot when prompted

# 3. Install data-remount service (via overlayroot-chroot)
sudo overlayroot-chroot bash -c "
cp ~/syncdrive/cam/systemd/data-remount.service /etc/systemd/system/
systemctl enable data-remount.service
"
sudo reboot
```

After setup:
- `/` is read-only (protected)
- `/data` is read-write (recordings persist)
- Safe to power-pull during recording

## Logs

```bash
journalctl -u syncdrive-recorder -f
journalctl -u syncdrive-uploader -f
journalctl -u syncdrive-cam-api -f
```
