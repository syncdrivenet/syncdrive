# Network Setup

## Overview

SyncDrive uses local networking with mDNS for device discovery.

```
                    Local Network (LAN)
    ┌─────────────────────────────────────────────────────┐
    │                                                     │
    │   ┌──────────┐    ┌──────────┐    ┌──────────┐     │
    │   │ Camera 1 │    │ Camera 2 │    │ Camera 3 │     │
    │   │ .local   │    │ .local   │    │ .local   │     │
    │   └────┬─────┘    └────┬─────┘    └────┬─────┘     │
    │        │               │               │           │
    │        └───────────────┼───────────────┘           │
    │                        │                           │
    │                        ▼                           │
    │                 ┌──────────┐      ┌──────────┐     │
    │                 │Controller│◄────►│ iOS App  │     │
    │                 │ .local   │      │          │     │
    │                 └────┬─────┘      └──────────┘     │
    │                      │                             │
    └──────────────────────┼─────────────────────────────┘
                           │ Tailscale (optional)
                           ▼
                    ┌──────────┐
                    │   VPS    │
                    │ Grafana  │
                    └──────────┘
```

## mDNS (.local hostnames)

### How It Works

mDNS (multicast DNS) allows devices to discover each other by name without a central DNS server.

1. **Pi boots** with hostname `melb-02-cam-01`
2. **Avahi broadcasts**: "I am melb-02-cam-01.local at 192.168.1.103"
3. **Your device queries**: "Who is melb-02-cam-01.local?"
4. **Pi responds** with its IP address

### Requirements

| Device | Service |
|--------|---------|
| Raspberry Pi | `avahi-daemon` (pre-installed) |
| macOS | Bonjour (built-in) |
| iOS | Bonjour (built-in) |
| Windows | Bonjour (install iTunes or Bonjour Print Services) |
| Linux | `avahi-daemon` + `libnss-mdns` |

### Limitations

- **LAN only** - devices must be on the same network/subnet
- **No VLANs** - won't work across VLANs without mDNS reflector
- **No internet** - `.local` doesn't work over the internet

### Troubleshooting

```bash
# Check if avahi is running
systemctl status avahi-daemon

# See what names are being advertised
avahi-browse -a

# Resolve a .local hostname
avahi-resolve -n melb-02-cam-01.local

# From Mac - check mDNS
dns-sd -G v4 melb-02-cam-01.local
```

## IP Addressing

### Recommended: DHCP with Reserved IPs

Configure your router to assign fixed IPs based on MAC address:

| Hostname | MAC | Reserved IP |
|----------|-----|-------------|
| melb-02-ctlr | dc:a6:32:xx:xx:xx | 192.168.1.100 |
| melb-02-cam-01 | dc:a6:32:xx:xx:xx | 192.168.1.101 |
| melb-02-cam-02 | dc:a6:32:xx:xx:xx | 192.168.1.102 |
| melb-02-cam-03 | dc:a6:32:xx:xx:xx | 192.168.1.103 |

**Why?** mDNS works regardless of IP, but fixed IPs make debugging easier.

### Alternative: Pure DHCP

Works fine - mDNS handles discovery even when IPs change.

## Remote Access with Tailscale

For access outside the LAN, use [Tailscale](https://tailscale.com):

```bash
# Install on Pi
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Access via Tailscale hostname
ssh pi@melb-02-ctlr.tail-xxxxx.ts.net

# Or via Tailscale IP
ssh pi@100.x.x.x
```

### Tailscale DNS

Tailscale provides its own DNS:
- `hostname.tailnet-name.ts.net`
- Works from anywhere with Tailscale installed

## Ports

| Service | Port | Protocol |
|---------|------|----------|
| Controller API | 8000 | HTTP |
| Controller WebSocket | 8000 | WS |
| Camera API | 8080 | HTTP |
| Grafana | 3000 | HTTP |
| Loki | 3100 | HTTP |
| Prometheus | 9090 | HTTP |

## Firewall

### On Pis (usually disabled)

Raspberry Pi OS has no firewall by default. If you enable `ufw`:

```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 8000/tcp  # Controller API (on controller only)
sudo ufw allow 8080/tcp  # Camera API (on cameras only)
```

### On VPS

Only SSH should be public. Grafana/Loki/Prometheus via Tailscale:

```bash
sudo ufw status
# Should show only:
# 22/tcp    ALLOW    Anywhere
```

## WiFi Configuration

WiFi credentials are baked into the worker image via cloud-init.

To change WiFi after flashing, edit on the SD card:

```bash
# Mount boot partition
sudo mount /dev/sdX1 /mnt

# Edit cloud-init user-data
sudo nano /mnt/user-data
# Find wifi section, update ssid/password

sudo umount /mnt
```

Or after boot:

```bash
# On the Pi
sudo nmcli dev wifi connect "SSID" password "PASSWORD"
```

## Hostname Convention

Format: `{location}-{unit}-{role}-{number}`

Examples:
- `melb-02-ctlr` - Melbourne unit 2, controller
- `melb-02-cam-01` - Melbourne unit 2, camera 1
- `syd-01-cam-03` - Sydney unit 1, camera 3

This allows multiple SyncDrive units on the same network without conflicts.
