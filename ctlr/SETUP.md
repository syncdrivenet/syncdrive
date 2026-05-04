# Controller Node Setup Guide

Complete setup guide for a new SyncDrive controller node (Pi 4).

## Prerequisites

- Raspberry Pi 4 with Raspberry Pi OS
- External HDD/SSD for storage
- Network connection (Tailscale recommended)

## 1. System Dependencies

```bash
sudo apt update
sudo apt install -y python3-fastapi python3-uvicorn python3-yaml exfatprogs
```

## 2. External Storage Setup

### 2.1 Partition the Drive

```bash
# Find the drive
lsblk

# Partition (adjust size as needed - example: 100GB ext4, rest exFAT)
echo -e 'label: dos\n,100G,L\n,,7' | sudo sfdisk /dev/sdX --force

# Format partitions
sudo mkfs.ext4 -L syncdrive /dev/sdX1
sudo mkfs.exfat -n EXPORT /dev/sdX2
```

### 2.2 Create Mount Points

```bash
sudo mkdir -p /mnt/storage /mnt/export
```

### 2.3 Get UUIDs

```bash
sudo blkid /dev/sdX1 /dev/sdX2
# Note the UUIDs for fstab
```

### 2.4 Configure fstab

```bash
sudo nano /etc/fstab
```

Add these lines (replace UUIDs with your values):

```
# SyncDrive HDD
UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx /mnt/storage ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2
UUID=XXXX-XXXX /mnt/export exfat defaults,nofail,x-systemd.device-timeout=10s,uid=1000,gid=1000 0 0
```

### 2.5 Mount and Verify

```bash
sudo mount -a
df -h | grep mnt
```

### 2.6 Create Directory Structure

```bash
sudo mkdir -p /mnt/storage/sessions
sudo chown -R pi:pi /mnt/storage /mnt/export
```

## 3. Passwordless Sudo for Storage Commands

Required for API to mount/unmount without password prompts.

```bash
sudo tee /etc/sudoers.d/syncdrive-storage << 'EOF'
pi ALL=(ALL) NOPASSWD: /usr/bin/mount /mnt/storage
pi ALL=(ALL) NOPASSWD: /usr/bin/mount /mnt/export
pi ALL=(ALL) NOPASSWD: /usr/bin/umount /mnt/storage
pi ALL=(ALL) NOPASSWD: /usr/bin/umount /mnt/export
pi ALL=(ALL) NOPASSWD: /usr/bin/sync
EOF

sudo chmod 440 /etc/sudoers.d/syncdrive-storage
sudo visudo -c  # Verify syntax
```

## 4. Controller Code

### 4.1 Create Directories

```bash
mkdir -p ~/ctlr-v2
mkdir -p /data/ctlr
mkdir -p /data/logs/ctlr
```

### 4.2 Copy Code

```bash
# From your dev machine:
scp -r /path/to/ctlr-v2/* pi@<controller-ip>:~/ctlr-v2/
```

### 4.3 Create Config

```bash
cat > /data/ctlr/config.yml << 'EOF'
node:
  name: melb-02-ctlr

cameras:
  - name: melb-02-cam-01
    host: melb-02-cam-01.local
    port: 8080

storage:
  recordings_dir: /mnt/storage/sessions
  export_dir: /mnt/export

api:
  host: 0.0.0.0
  port: 8000
EOF
```

## 5. Systemd Service

### 5.1 Create Service File

```bash
sudo tee /etc/systemd/system/syncdrive-ctlr.service << 'EOF'
[Unit]
Description=SyncDrive Controller
After=network.target mnt-storage.mount
Wants=mnt-storage.mount

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/ctlr-v2
Environment=CTLR_CONFIG=/data/ctlr/config.yml
ExecStart=/usr/bin/python3 -m uvicorn api:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

### 5.2 Enable and Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable syncdrive-ctlr
sudo systemctl start syncdrive-ctlr
sudo systemctl status syncdrive-ctlr
```

## 6. Verify Setup

### 6.1 Check Storage

```bash
curl -s http://localhost:8000/api/storage/status | python3 -m json.tool
```

Expected output:
```json
{
  "success": true,
  "data": {
    "storage": {"mounted": true, "free_gb": 92.85},
    "export": {"mounted": true, "free_gb": 14.59},
    "all_mounted": true,
    "safe_to_eject": true
  }
}
```

### 6.2 Test Eject/Remount

```bash
# Eject (before unplugging)
curl -s -X POST http://localhost:8000/api/storage/unmount

# Remount (after plugging back in)
curl -s -X POST http://localhost:8000/api/storage/remount
```

## 7. Optional: Auto-Remount on Plug-In

Create udev rule to auto-mount when drive is plugged in:

```bash
sudo tee /etc/udev/rules.d/99-syncdrive-storage.rules << 'EOF'
# Auto-mount SyncDrive storage when plugged in
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="syncdrive", RUN+="/usr/bin/mount /mnt/storage"
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="EXPORT", RUN+="/usr/bin/mount /mnt/export"
EOF

sudo udevadm control --reload-rules
```

## API Endpoints Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/api/storage/status` | GET | Storage status (mounted, space) |
| `/api/storage/remount` | POST | Mount both partitions |
| `/api/storage/unmount` | POST | Safe eject (sync + unmount) |
| `/api/segment/{cam}/{uuid}/{file}` | PUT | Receive segment upload |
| `/sessions` | GET | List all sessions |
| `/sessions/{uuid}` | GET | Session details |

## Troubleshooting

### Storage won't mount

```bash
# Check drive is detected
lsblk

# Check fstab syntax
sudo mount -a

# Check logs
journalctl -u syncdrive-ctlr -f
```

### API not responding

```bash
# Check service status
sudo systemctl status syncdrive-ctlr

# Check logs
journalctl -u syncdrive-ctlr --since "5 minutes ago"

# Restart
sudo systemctl restart syncdrive-ctlr
```

### Drive busy on unmount

```bash
# Find what's using it
sudo lsof /mnt/storage

# Force unmount (use with caution)
sudo umount -l /mnt/storage
```

## Storage Layout

```
/mnt/storage/           (ext4 - journaled, safe)
└── sessions/
    └── {uuid}/
        ├── cam-01/
        │   ├── seg_0001.h264
        │   └── seg_0002.h264
        ├── cam-02/
        │   └── ...
        ├── canbus.csv
        ├── phone/
        └── watch/

/mnt/export/            (exFAT - Mac readable)
└── {uuid}/             (copied when ready for Mac)
```

## Partition Sizing Guide

| Total Drive | ext4 (storage) | exFAT (export) |
|-------------|----------------|----------------|
| 128GB | 112GB | 16GB |
| 256GB | 224GB | 32GB |
| 512GB | 460GB | 52GB |
| 1TB | 950GB | 50GB |

Export partition only needs space for a few sessions during transfer to Mac.
