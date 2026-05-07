# SyncDrive Installation Guide

Complete installation guide for the SyncDrive multi-camera recording system.

## Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SyncDrive System                              │
│                                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                           │
│  │ Pi Zero  │  │ Pi Zero  │  │ Pi Zero  │   Camera Nodes (x3)       │
│  │ cam-01   │  │ cam-02   │  │ cam-03   │   - Record H264 segments  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘   - Upload to controller  │
│       │             │             │                                  │
│       └─────────────┼─────────────┘                                  │
│                     │ HTTP PUT (segments)                            │
│                     ▼                                                │
│              ┌──────────────┐         ┌──────────┐                  │
│              │   Pi 4       │◄───────►│ iOS App  │                  │
│              │ Controller   │   WS    │ (Discar) │                  │
│              └──────┬───────┘         └──────────┘                  │
│                     │                                                │
│       ┌─────────────┼─────────────┐                                  │
│       │             │             │                                  │
│       ▼             ▼             ▼                                  │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐                            │
│  │ ESP32   │  │ ext4     │  │ exFAT    │                            │
│  │ CAN Bus │  │ Storage  │  │ Export   │   External HDD             │
│  └─────────┘  └──────────┘  └──────────┘   (2 partitions)           │
└─────────────────────────────────────────────────────────────────────┘
```

## Hardware Requirements

| Component | Model | Quantity | Notes |
|-----------|-------|----------|-------|
| Controller | Raspberry Pi 4 (4GB+) | 1 | Handles orchestration + storage |
| Cameras | Raspberry Pi Zero 2 W | 3 | With Camera Module 3 |
| Camera Modules | Raspberry Pi Camera Module 3 | 3 | Wide or standard |
| CAN Logger | ESP32 | 1 | With CAN transceiver (MCP2515/SN65HVD230) |
| External HDD | USB 3.0 (1TB+) | 1 | For controller storage |
| SD Cards | 16GB+ | 4 | Class 10 or better |
| Power | 5V 3A PSU | 4 | Good quality, stable power |

## Prerequisites

On your development machine:
- Ansible installed (`pip install ansible`)
- SSH key configured (`~/.ssh/id_ed25519`)
- Tailscale (optional, for remote access)

## Step 1: Prepare SD Cards

### Controller (Pi 4)

Flash Raspberry Pi OS Lite 64-bit:

```bash
# Download and flash with Raspberry Pi Imager
# Set hostname: melb-01-ctlr
# Enable SSH, set WiFi credentials
# Add your SSH key
```

### Cameras (Pi Zero 2 W)

Option A: Use pre-built worker image (recommended):

```bash
# Flash the image
gunzip -c ~/worker-template.img.gz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sudo sync

# Set unique hostname
sudo ~/prep-worker-card.sh /dev/sdX melb-01-cam-01

# Eject
sudo eject /dev/sdX
```

Option B: Fresh install:

```bash
# Flash Raspberry Pi OS Lite 32-bit
# Set hostname, WiFi, SSH via Raspberry Pi Imager
# After first boot, create /data partition manually (see Partition Layout)
```

### Camera Partition Layout (Required for Overlay FS)

For power-loss protection, cameras need a separate `/data` partition:

```
/dev/mmcblk0p1  512MB   /boot/firmware  (FAT32)
/dev/mmcblk0p2  6GB     /               (ext4, will be read-only)
/dev/mmcblk0p3  REST    /data           (ext4, stays writable)
```

To create this layout manually:

```bash
# After initial boot, shrink root and create /data
sudo fdisk /dev/mmcblk0
# Delete p2, recreate as 6GB, create p3 with remaining space

sudo mkfs.ext4 /dev/mmcblk0p3
echo '/dev/mmcblk0p3 /data ext4 defaults,noatime,nofail 0 2' | sudo tee -a /etc/fstab
sudo mkdir /data
sudo mount /data
```

## Step 2: Prepare Controller HDD

The external HDD needs two partitions:

| Partition | Filesystem | Size | Purpose |
|-----------|------------|------|---------|
| `/mnt/storage` | ext4 | 80%+ | Active recordings (journaled) |
| `/mnt/export` | exFAT | 20% | Mac-readable exports |

### Format the HDD

```bash
# On the controller Pi (after boot)
sudo fdisk /dev/sda
# Create partition 1 (ext4, ~800GB)
# Create partition 2 (exFAT, ~200GB)

sudo mkfs.ext4 -L storage /dev/sda1
sudo mkfs.exfat -L export /dev/sda2
```

### Create Mount Points

```bash
sudo mkdir -p /mnt/storage /mnt/export
sudo chown pi:pi /mnt/storage /mnt/export
```

The Ansible playbook will configure automount via systemd.

## Step 3: Network Setup

### Hostname Convention

```
{location}-{unit}-{role}-{number}

Examples:
- melb-01-ctlr      (Melbourne unit 1, controller)
- melb-01-cam-01    (Melbourne unit 1, camera 1)
- melb-01-cam-02    (Melbourne unit 1, camera 2)
```

### Verify mDNS Resolution

After booting all Pis:

```bash
# From your Mac
ping melb-01-ctlr.local
ping melb-01-cam-01.local
```

If mDNS doesn't work, use IP addresses in `group_vars/all.yml`.

### Tailscale (Optional)

For remote access:

```bash
# On each Pi
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Authenticate via the URL provided
```

## Step 4: Configure Ansible

### Edit Inventory

Edit `ansible/inventory.ini`:

```ini
[cameras]
melb-01-cam-01 ansible_host=melb-01-cam-01
melb-01-cam-02 ansible_host=melb-01-cam-02
melb-01-cam-03 ansible_host=melb-01-cam-03

[controllers]
melb-01-ctlr ansible_host=melb-01-ctlr

[pis:children]
cameras
controllers

[pis:vars]
ansible_user=pi
ansible_python_interpreter=/usr/bin/python3
```

### Edit Configuration

Edit `ansible/group_vars/all.yml`:

```yaml
# Controller IP (use IP if mDNS unreliable)
controller_host: 192.168.x.x  # Or melb-01-ctlr.local

# Recording settings
recording:
  width: 1920
  height: 1080
  fps: 30
  bitrate: 4000000        # 4Mbps
  segment_duration: 120   # 2 minutes
```

### Test Connectivity

```bash
cd ansible
ansible all -m ping
```

## Step 5: Deploy

### Deploy Everything

```bash
cd ansible
ansible-playbook site.yml
```

### Deploy Specific Hosts

```bash
# Just cameras
ansible-playbook site.yml --limit cameras

# Just controller
ansible-playbook site.yml --limit controllers

# Single host
ansible-playbook site.yml --limit melb-01-cam-01
```

### What Ansible Does

**On Cameras:**
- Installs picamera2, python3-venv
- Clones syncdrive repo
- Creates Python venv with dependencies
- Creates config at `/data/cam/config.yml`
- Installs and starts systemd services:
  - `syncdrive-recorder` (records video)
  - `syncdrive-uploader` (uploads segments)
  - `syncdrive-cam-api` (HTTP API)

**On Controller:**
- Installs python3-venv
- Clones syncdrive repo
- Creates Python venv with dependencies
- Creates config at `/data/ctlr/config.yml`
- Configures HDD mounts
- Installs and starts:
  - `syncdrive-ctlr` (API + orchestrator + CAN listener)

## Step 6: Verify Installation

### Check Services

```bash
# On cameras
ssh pi@melb-01-cam-01 "systemctl status syncdrive-recorder syncdrive-uploader syncdrive-cam-api"

# On controller
ssh pi@melb-01-ctlr "systemctl status syncdrive-ctlr"
```

### Check Camera API

```bash
curl http://melb-01-cam-01.local:8080/status
```

Expected:
```json
{"recording": false, "uuid": null, "segment": 0, "camera_available": true}
```

### Check Controller API

```bash
curl http://melb-01-ctlr.local:8000/health
curl http://melb-01-ctlr.local:8000/api/cameras
```

### Check Storage

```bash
ssh pi@melb-01-ctlr "df -h /mnt/storage /mnt/export"
```

## Step 7: Test Recording

### Preflight Check

```bash
curl "http://melb-01-ctlr.local:8000/api/preflight?client_time_ms=$(date +%s)000"
```

All should show `ready: true`.

### Start Recording

```bash
UUID=$(uuidgen)
curl -X POST "http://melb-01-ctlr.local:8000/api/record/start?uuid=$UUID"
```

### Monitor Status

```bash
# Watch controller logs
ssh pi@melb-01-ctlr "journalctl -u syncdrive-ctlr -f"

# Check recording status
curl http://melb-01-ctlr.local:8000/api/status
```

### Stop Recording

```bash
curl -X POST http://melb-01-ctlr.local:8000/api/record/stop
```

### Verify Files

```bash
ssh pi@melb-01-ctlr "ls -la /mnt/storage/sessions/$UUID/"
```

## Step 8: Shutdown Procedure

**Always shut down properly to prevent SD card corruption.**

### Via iOS App

Tap "Shutdown" in the app - this triggers the full sequence.

### Via API

```bash
curl -X POST http://melb-01-ctlr.local:8000/api/shutdown/all
```

### Shutdown Sequence

1. Pauses uploads on all cameras
2. Sends shutdown command to all cameras
3. Waits for cameras to go offline
4. Unmounts storage (syncs filesystem)
5. Shuts down controller

### Manual Shutdown

If API unavailable:

```bash
# On each camera
sudo shutdown now

# On controller (after cameras are off)
sudo shutdown now
```

## ESP32 CAN Logger Setup

The ESP32 connects to the controller over WiFi and sends CAN data via TCP.

### Wiring

```
ESP32          CAN Transceiver
GPIO 21  -->   TX (CAN TX)
GPIO 22  -->   RX (CAN RX)
3.3V     -->   VCC
GND      -->   GND
```

### Configuration

Update the ESP32 firmware with:
- WiFi credentials
- Controller IP: `192.168.x.x`
- Controller port: `9101`

### Verify Connection

```bash
curl http://melb-01-ctlr.local:8000/api/can/status
```

Expected:
```json
{"connected": true, "ntp_synced": true, "status": "idle"}
```

## Troubleshooting

### Camera Not Connecting

```bash
# Check service status
ssh pi@melb-01-cam-01 "systemctl status syncdrive-*"

# Check logs
ssh pi@melb-01-cam-01 "journalctl -u syncdrive-recorder -n 50"

# Test camera hardware
ssh pi@melb-01-cam-01 "libcamera-hello --list-cameras"
```

### Upload Failing

```bash
# Check uploader logs
ssh pi@melb-01-cam-01 "journalctl -u syncdrive-uploader -f"

# Verify controller reachable
ssh pi@melb-01-cam-01 "curl http://192.168.x.x:8000/health"
```

### Storage Not Mounted

```bash
ssh pi@melb-01-ctlr "mount | grep /mnt"
ssh pi@melb-01-ctlr "sudo mount -a"
ssh pi@melb-01-ctlr "journalctl -u mnt-storage.mount"
```

### CAN Not Connected

```bash
# Check port is listening
ssh pi@melb-01-ctlr "ss -tlnp | grep 9101"

# Check for ESP32 connections
ssh pi@melb-01-ctlr "journalctl -u syncdrive-ctlr | grep ESP32"
```

## Updating

After pushing changes to the repo:

```bash
cd ansible
ansible-playbook site.yml

# Or update only code (faster)
ansible-playbook site.yml --tags code
```

## Monitoring (Optional)

### Grafana + Loki + Prometheus

Set up on a VPS with Tailscale access:

1. Install Prometheus, Loki, Grafana on VPS
2. Install node_exporter on all Pis
3. Install fluent-bit on controller for log forwarding
4. Configure rsyslog on cameras to forward to controller
5. Import `grafana-dashboard.json`

See `docs/MONITORING.md` for detailed setup.
