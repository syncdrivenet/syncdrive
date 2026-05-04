# SyncDrive

Multi-camera synchronized recording system for Raspberry Pi.

## Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              SyncDrive System                                │
│                                                                              │
│   Pi Zero 2 W (x3)              Pi 4                     iOS App            │
│   ┌─────────────┐            ┌─────────────┐          ┌─────────────┐       │
│   │   Camera    │  HTTP PUT  │ Controller  │    WS    │   Discar    │       │
│   │   Node      │───────────▶│             │◀────────▶│             │       │
│   │             │  segments  │             │  status  │             │       │
│   └─────────────┘            └─────────────┘          └─────────────┘       │
│         │                           │                        │              │
│         │                           ▼                        │              │
│         │                    ┌─────────────┐                 │              │
│         │                    │ External    │                 │              │
│         └───────────────────▶│ HDD         │◀────────────────┘              │
│              upload          │ (ext4+exFAT)│     sync phone/watch           │
│                              └─────────────┘                                │
└─────────────────────────────────────────────────────────────────────────────┘
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

## Quick Start

### Deploy with Ansible (recommended)

```bash
# Clone repo
git clone https://github.com/syncdrivenet/syncdrive.git
cd syncdrive/ansible

# Edit inventory with your Pi hostnames
nano inventory.ini

# Deploy everything
ansible-playbook site.yml
```

### Update Remotely

After pushing changes to GitHub:

```bash
# Via Ansible (updates all Pis)
ansible-playbook site.yml

# Or manually on one Pi
ssh pi@melb-02-cam-01
cd ~/syncdrive && git pull
sudo systemctl restart syncdrive-recorder syncdrive-uploader syncdrive-cam-api
```

## Components

| Directory | Device | Services |
|-----------|--------|----------|
| `cam/` | Pi Zero 2 W | recorder, uploader, api |
| `ctlr/` | Pi 4 | syncdrive-ctlr |

See individual READMEs for details:
- [Camera Node](cam/README.md)
- [Controller Node](ctlr/README.md)
- [Ansible Deployment](ansible/README.md)

## Hardware

- **Controller**: Raspberry Pi 4 (4GB+) with external USB HDD
- **Cameras**: Raspberry Pi Zero 2 W with Camera Module 3
- **Storage**: External HDD with two partitions (ext4 + exFAT)
- **Network**: Local WiFi (5GHz recommended)

## License

MIT
