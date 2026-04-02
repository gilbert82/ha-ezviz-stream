# Ezviz Camera Proxy — Home Assistant Add-on

[![GitHub Release](https://img.shields.io/github/v/release/g-stuecheli/ha-ezviz-stream)](https://github.com/g-stuecheli/ha-ezviz-stream/releases)
[![License](https://img.shields.io/github/license/g-stuecheli/ha-ezviz-stream)](LICENSE)

A Home Assistant Add-on that brings **Ezviz HP2 door viewer cameras** (and other Ezviz cameras
without RTSP support) into your smart home by using the Ezviz Cloud API.

---

## Why This Add-on?

The **Ezviz CS-HP2** is a smart door viewer / peephole camera that:

- ✅ Works great in the Ezviz app
- ✅ Supports motion detection, doorbell events, 1080p video
- ❌ Does **not** support RTSP
- ❌ Does **not** support LAN Live View
- ❌ Cannot be integrated with standard HA camera platforms

This add-on solves that by acting as a local proxy between HA and the Ezviz Cloud API.

---

## Features

- 📸 **Snapshot polling** — Periodic cloud snapshots cached locally, served as JPEG
- 📹 **MJPEG stream** — Simulated live view from cached snapshots (compatible with HA Generic Camera)
- 🔋 **Battery & status** — Camera online status, battery level, WiFi signal via REST API
- 🔔 **Alarm events** — Recent doorbell and motion events, with optional MQTT publishing
- 🌐 **Web UI** — Built-in dashboard with HA Ingress support (sidebar panel)
- 🔒 **Secure** — Credentials stored in HA add-on config, never logged

---

## Installation

1. Add this repository to your HA Add-on Store:
   ```
   https://github.com/g-stuecheli/ha-ezviz-stream
   ```
2. Install **Ezviz Camera Proxy**
3. Configure with your Ezviz credentials and camera serial
4. Start the add-on

---

## Quick Start Configuration

```yaml
ezviz_username: "your@email.com"
ezviz_password: "your-password"
ezviz_region: "apiieu.ezvizlife.com"
camera_serial: "AB1234567"
camera_password: "123456"
snapshot_interval: 30
enable_mqtt_events: true
```

---

## Add as HA Camera Entity

```yaml
camera:
  - platform: generic
    name: "HP2 Door Viewer"
    still_image_url: "http://localhost:8099/api/snapshot"
    stream_source: "http://localhost:8099/api/stream"
```

---

## Documentation

See [DOCS.md](DOCS.md) for full configuration reference, HA integration examples,
API documentation and troubleshooting.

---

## Supported Architectures

`amd64` · `aarch64` · `armv7` · `armhf` · `i386`

---

## License

MIT — see [LICENSE](../../LICENSE)
