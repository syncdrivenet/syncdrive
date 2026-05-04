# Ansible Deployment

Automated deployment for SyncDrive camera system.

## Overview

Deploys camera nodes and controller from GitHub repo to Raspberry Pis.

```
┌─────────────┐     git clone      ┌─────────────┐
│   GitHub    │ ◀────────────────  │    Pis      │
│  syncdrive  │                    │             │
└─────────────┘                    └─────────────┘
       ▲                                  ▲
       │ git push                         │ ansible-playbook
       │                                  │
┌─────────────┐                    ┌─────────────┐
│    You      │ ─────────────────▶ │   Laptop    │
│  (anywhere) │      Tailscale     │             │
└─────────────┘                    └─────────────┘
```

## Quick Start

```bash
# 1. Edit inventory with your Pi hostnames
nano inventory.ini

# 2. Deploy everything
ansible-playbook site.yml

# 3. Deploy only cameras
ansible-playbook site.yml --limit cameras

# 4. Deploy only controller
ansible-playbook site.yml --limit controllers
```

## Files

```
ansible/
├── inventory.ini        # Pi hostnames
├── site.yml             # Main playbook
├── group_vars/
│   └── all.yml          # Shared variables
└── roles/
    ├── cam/             # Camera node role
    │   ├── tasks/
    │   ├── templates/
    │   └── handlers/
    └── ctlr/            # Controller role
        ├── tasks/
        ├── templates/
        └── handlers/
```

## Inventory

Edit `inventory.ini` with your Pi hostnames:

```ini
[cameras]
melb-02-cam-01 ansible_host=melb-02-cam-01.local
melb-02-cam-02 ansible_host=melb-02-cam-02.local
melb-02-cam-03 ansible_host=melb-02-cam-03.local

[controllers]
melb-02-ctlr ansible_host=melb-02-ctlr.local

[pis:children]
cameras
controllers

[pis:vars]
ansible_user=pi
```

## Configuration

Edit `group_vars/all.yml`:

```yaml
# GitHub repo
syncdrive_repo: https://github.com/syncdrivenet/syncdrive.git
syncdrive_version: main

# Controller
controller_host: melb-02-ctlr.local
controller_port: 8000

# Recording
recording:
  width: 1920
  height: 1080
  fps: 30
  segment_duration: 120
```

## Remote Updates

After pushing changes to GitHub:

```bash
# Update all Pis
ansible-playbook site.yml

# Update only code (skip apt install)
ansible-playbook site.yml --tags code

# Check what would change (dry run)
ansible-playbook site.yml --check --diff
```

## Manual Update (single Pi)

```bash
ssh pi@melb-02-cam-01
cd ~/syncdrive && git pull
sudo systemctl restart syncdrive-recorder syncdrive-uploader syncdrive-cam-api
```

## Troubleshooting

```bash
# Test connectivity
ansible all -m ping

# Run with verbose output
ansible-playbook site.yml -vvv

# Check specific host
ansible-playbook site.yml --limit melb-02-cam-01
```
