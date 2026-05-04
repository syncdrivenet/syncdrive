# Worker Image Build

Pre-built SD card image for SyncDrive camera nodes (Pi Zero 2 W).

## Image Contents

### Base
- Raspberry Pi OS Lite 32-bit (Debian Trixie, kernel 6.12.75)
- Cloud-init for first-boot configuration

### Partition Layout

| Partition | Filesystem | Size | Purpose |
|-----------|------------|------|---------|
| `/boot/firmware` | FAT32 | 512 MB | Bootloader, kernel, cloud-init configs |
| `/` | ext4 | 6 GB | OS (will be read-only with overlay later) |
| `/data` | ext4 | 142 MB → auto-expands | Video files, app state, logs |

### Pre-configured

- **Hostname**: `melb-02-cam-01` (override per card with prep script)
- **Timezone**: Australia/Sydney
- **WiFi**: Credentials + AU regulatory domain
- **SSH**: Key-based auth (your ed25519 key)
- **User**: `pi` with passwordless sudo
- **Avahi**: mDNS for `.local` hostname resolution
- **Serial**: Interface enabled for debugging
- **Fstab**: `/data` partition with `nofail`
- **Auto-expand**: One-shot service grows `/data` on first boot

### File Size

- Compressed: ~845 MB (`worker-template.img.gz`)
- Decompressed: ~6.7 GB

## SD Card Requirements

| Card Size | Compatible | /data Space |
|-----------|------------|-------------|
| 4GB | NO | Image is 6.7GB |
| 8GB | Marginal | ~1.3GB |
| **16GB** | **Recommended** | ~9GB |
| 32GB | Yes | ~25GB |
| 64GB+ | Yes | Plenty |

**Minimum: 8GB / Recommended: 16GB+**

Note: Some cheap "8GB" cards are actually 7.4GB and won't fit.

## Flashing a New Card

### 1. Flash the Image

```bash
# Insert SD card and confirm device
lsblk

# Flash (adjust /dev/sdX to your device - BE CAREFUL!)
gunzip -c ~/worker-template.img.gz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
sudo sync
```

### 2. Set Unique Hostname

```bash
# Required to avoid mDNS conflicts
sudo ~/prep-worker-card.sh /dev/sdX melb-02-cam-03
```

### 3. Eject and Boot

```bash
sudo eject /dev/sdX
```

Insert into Pi and power on.

**Total time:** ~10 minutes per card

## First Boot Sequence

1. **Cloud-init** reads `user-data` → applies hostname, creates user, installs SSH key, configures WiFi
2. **expand-data-partition.service** runs early in boot → grows partition 3 to fill card
3. Service marks itself complete (`/var/lib/expand-data-partition.done`) and disables itself
4. `/data` mounts at full size
5. Cloud-init reboots once to apply hostname cleanly
6. System comes up at `<hostname>.local`, ready for SSH

**First boot time:** ~2-3 minutes

## After First Boot

### Verify SSH Access

```bash
ssh pi@melb-02-cam-03.local
```

### Deploy SyncDrive Software

```bash
# Add to ansible inventory (if not already there)
echo "melb-02-cam-03 ansible_host=melb-02-cam-03.local" >> ansible/inventory.ini

# Run ansible
cd syncdrive/ansible
ansible-playbook site.yml --limit melb-02-cam-03
```

### Enable Overlay Filesystem (Optional)

After ansible completes and testing passes:

```bash
ssh pi@melb-02-cam-03.local
sudo raspi-config
# → Performance → Overlay FS → Enable
# → Write-protect boot → Yes
# Reboot
```

## What's In the Image vs Ansible

| Component | Base Image | Ansible |
|-----------|------------|---------|
| Raspberry Pi OS | ✓ | |
| WiFi, SSH, hostname | ✓ | |
| /data partition | ✓ | |
| Auto-expand service | ✓ | |
| Avahi (mDNS) | ✓ | |
| python3-picamera2 | | ✓ |
| python3-venv | | ✓ |
| SyncDrive code | | ✓ |
| Python dependencies | | ✓ |
| Systemd services | | ✓ |
| Config file | | ✓ |
| Fluent Bit (logs) | | ✓ (optional) |
| Overlay filesystem | | Manual (raspi-config) |

## prep-worker-card.sh

Script to set unique hostname on a flashed card:

```bash
#!/bin/bash
# Usage: sudo ./prep-worker-card.sh /dev/sdX hostname

DEVICE=$1
HOSTNAME=$2

if [ -z "$DEVICE" ] || [ -z "$HOSTNAME" ]; then
    echo "Usage: sudo $0 /dev/sdX hostname"
    exit 1
fi

# Mount boot partition
MOUNT_POINT=$(mktemp -d)
mount ${DEVICE}1 $MOUNT_POINT

# Update cloud-init user-data
sed -i "s/hostname: .*/hostname: $HOSTNAME/" $MOUNT_POINT/user-data

# Update cmdline.txt if needed
# sed -i "s/melb-02-cam-01/$HOSTNAME/g" $MOUNT_POINT/cmdline.txt

umount $MOUNT_POINT
rmdir $MOUNT_POINT

echo "Hostname set to: $HOSTNAME"
```

## Troubleshooting

### Pi doesn't get IP / can't SSH

1. Check WiFi credentials in cloud-init user-data
2. Connect HDMI + keyboard to see boot messages
3. Check serial console (if enabled)

### /data didn't expand

```bash
# Check partition size
lsblk

# Manually expand if needed
sudo growpart /dev/mmcblk0 3
sudo resize2fs /dev/mmcblk0p3
```

### mDNS not working

```bash
# Check avahi is running
systemctl status avahi-daemon

# Restart if needed
sudo systemctl restart avahi-daemon
```

## Building a New Image

If you need to create a fresh image:

1. Flash stock Raspberry Pi OS Lite to SD card
2. Boot and configure manually:
   - Set hostname, timezone, WiFi
   - Create /data partition
   - Install cloud-init
   - Configure auto-expand service
3. Shrink and capture:
   ```bash
   # On Linux with PiShrink
   sudo pishrink.sh -z /dev/sdX worker-template.img.gz
   ```

Or use the Raspberry Pi Imager with cloud-init customization.
