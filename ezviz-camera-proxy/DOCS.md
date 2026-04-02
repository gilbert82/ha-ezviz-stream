# Ezviz Camera Proxy — Documentation

## What This Add-on Does

The **Ezviz Camera Proxy** add-on provides Home Assistant integration for Ezviz cameras that
**do not support RTSP or LAN Live View**, most notably the
[Ezviz CS-HP2 Door Viewer](https://www.ezviz.com/product/cs-hp2/7952).

Because the HP2 communicates exclusively via the Ezviz Cloud (P2P protocol), this add-on:

1. **Authenticates with the Ezviz Cloud API** using `pyezvizapi`
2. **Periodically fetches snapshots** from the cloud and caches them locally
3. **Exposes HTTP endpoints** for snapshot, MJPEG stream simulation, device status and alarm events
4. **Provides a built-in Web UI** accessible via Home Assistant Ingress (sidebar panel)
5. **Optionally publishes MQTT events** for doorbell presses and motion detection

---

## Why This Approach?

The HP2 has RTSP and LAN Live View deliberately disabled by Ezviz. Attempts to use standard
RTSP addresses (`rtsp://admin:<pwd>@<ip>:554/...`) will fail. The only supported access path is:

- **Ezviz mobile app** (uses proprietary P2P protocol)
- **Ezviz Cloud API** (via `pyezvizapi` Python library or REST API)
- **Ezviz Open Platform** (for developer HLS/RTMP streams, requires a paid developer account)

This add-on uses the Cloud API approach (pyezvizapi) which is free and works with any standard
Ezviz account.

---

## Installation

### Step 1: Add Repository

In Home Assistant, go to **Settings → Add-ons → Add-on Store → ⋮ Menu → Repositories** and add:

```
https://github.com/g-stuecheli/ha-ezviz-stream
```

### Step 2: Install Add-on

Find "Ezviz Camera Proxy" in the store and click **Install**.

### Step 3: Configure

Edit the add-on configuration:

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `ezviz_username` | Yes | — | Ezviz account email |
| `ezviz_password` | Yes | — | Ezviz account password |
| `ezviz_region` | No | `apiieu.ezvizlife.com` | API region (see below) |
| `camera_serial` | Yes | — | Camera serial number (9 chars) |
| `camera_password` | Yes | — | Camera verification code (label on device) |
| `snapshot_interval` | No | `30` | Seconds between cloud snapshots (5–300) |
| `enable_mqtt_events` | No | `true` | Publish events to HA MQTT |

### API Regions

| Region | Endpoint |
|--------|----------|
| Europe (default) | `apiieu.ezvizlife.com` |
| North America | `apiusa.ezvizlife.com` |
| China | `api.ezvizlife.com` |
| Rest of World | `apiglobal.ezvizlife.com` |

### Step 4: Start the Add-on

Click **Start**. The add-on starts a web server on port 8099 accessible via HA Ingress.

---

## Web UI

Once running, open the **Ezviz Camera** panel in the HA sidebar. The dashboard shows:

- Live snapshot (auto-refreshed every `snapshot_interval` seconds)
- Manual refresh button
- Camera status: online/offline, battery level, WiFi signal, firmware version
- Recent alarm events with type, timestamp and optional alarm image link
- Links to JSON API endpoints

---

## Using as a Home Assistant Camera Entity

### Generic Camera (Snapshot)

Add this to your `configuration.yaml`:

```yaml
camera:
  - platform: generic
    name: "HP2 Door Viewer"
    still_image_url: "http://localhost:8099/api/snapshot"
    verify_ssl: false
    scan_interval: 30
```

### Generic Camera (MJPEG Stream)

For a pseudo-live stream:

```yaml
camera:
  - platform: generic
    name: "HP2 Door Viewer"
    still_image_url: "http://localhost:8099/api/snapshot"
    stream_source: "http://localhost:8099/api/stream"
    verify_ssl: false
```

> **Note:** The MJPEG stream is simulated — it loops through cached snapshots from the cloud.
> True real-time video is not possible without the Ezviz Open Platform developer API.

### REST Sensors for Camera Status

```yaml
sensor:
  - platform: rest
    name: "HP2 Battery"
    resource: "http://localhost:8099/api/status"
    value_template: "{{ value_json.battery_level }}"
    unit_of_measurement: "%"
    device_class: battery
    scan_interval: 300

  - platform: rest
    name: "HP2 Online"
    resource: "http://localhost:8099/api/status"
    value_template: "{{ value_json.online }}"
    scan_interval: 60
```

### Doorbell Automation via MQTT Events

When `enable_mqtt_events: true`, the add-on publishes to:

- `homeassistant/camera/ezviz/<serial>/doorbell` — Doorbell press detected
- `homeassistant/camera/ezviz/<serial>/motion`   — Motion detected

Example automation:

```yaml
automation:
  - alias: "HP2 Doorbell Notification"
    trigger:
      - platform: mqtt
        topic: "homeassistant/camera/ezviz/AB1234567/doorbell"
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "Someone is at the door!"
          data:
            image: "/api/camera_proxy/camera.hp2_door_viewer"
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web dashboard |
| `/api/snapshot` | GET | Current snapshot JPEG |
| `/api/snapshot/refresh` | GET/POST | Force new snapshot from cloud |
| `/api/status` | GET | Camera status JSON |
| `/api/events` | GET | Recent alarm events JSON |
| `/api/stream` | GET | MJPEG stream |
| `/api/devices` | GET | All devices on account |
| `/api/health` | GET | Add-on health check |

---

## Troubleshooting

### "Auth error: Login failed"

- Double-check your Ezviz username and password
- If you recently changed your password, update it in the add-on config
- If you have two-factor authentication enabled in Ezviz, you may need to temporarily log in via
  the Ezviz mobile app after the first add-on login to confirm the new login session

### "Could not load camera: …"

- Verify the camera serial number (9-character code, e.g., "AB1234567")
- Verify the camera verification code (6-digit code on the device label, **not** your account
  password)
- Ensure the camera is registered to the same Ezviz account you configured

### "Snapshot returned empty data"

- The HP2 is battery-powered and enters deep sleep between events
- Wake the camera by pressing the doorbell button or walking in front of it
- Increase `snapshot_interval` to reduce battery drain — the HP2 only wakes to fetch a snapshot
  when the cloud requests it, which also wakes the device briefly

### Snapshot is old / never updates

- Check the add-on logs: **Settings → Add-ons → Ezviz Camera Proxy → Logs**
- Ensure your Ezviz account credentials are correct
- Check internet connectivity from the HA host

### Battery drains quickly

- Increase `snapshot_interval` (e.g., 60 or 120 seconds)
- Each snapshot fetch causes the HP2 to wake from sleep, which uses battery
- Consider using MQTT events + on-demand refresh in automations instead of continuous polling

### Web UI not loading

- Ensure the add-on is running (green indicator)
- Try accessing directly: `http://<ha-ip>:8099/`
- Check that Ingress is enabled in the add-on configuration

---

## Resources

- [pyezvizapi on PyPI](https://pypi.org/project/pyezvizapi/)
- [HA Ezviz Integration](https://www.home-assistant.io/integrations/ezviz/)
- [HA Add-on Development Docs](https://developers.home-assistant.io/docs/add-ons/configuration)
- [Ezviz Open Platform (HLS/RTMP)](https://open.ys7.com/help/en/489)
- [HP2 RTSP community thread](https://community.home-assistant.io/t/ezviz-cameras-rtsp-stream-not-working/523726?page=4)
- [HP2 snapshot GitHub issue](https://github.com/home-assistant/core/issues/134292)
- [Add-on GitHub Repository](https://github.com/g-stuecheli/ha-ezviz-stream)
