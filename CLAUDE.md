# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A **Home Assistant Add-on repository** (not a standalone app). The root
`repository.yaml` registers it with the HA Add-on Store; each subdirectory is one
add-on. Currently there is a single add-on: `ezviz-camera-proxy/`.

The add-on bridges **Ezviz cameras that have no RTSP / LAN Live View** (primarily
the battery-powered **CS-HP2 door viewer**) into Home Assistant by polling the
Ezviz Cloud API and re-serving snapshots, a simulated MJPEG stream, status and
events over HTTP behind HA Ingress.

## Build / run / release

There is **no local build system, test suite, or linter** in this repo — it is
built by the Home Assistant Supervisor from `ezviz-camera-proxy/Dockerfile` +
`build.yaml` (HA `*-base-python:3.12-alpine3.20` base images, one per arch).

- **Local smoke-test of the Python app** (outside HA, no bashio):
  ```bash
  cd ezviz-camera-proxy/rootfs/app
  pip install -r requirements.txt
  EZVIZ_USERNAME=… EZVIZ_PASSWORD=… CAMERA_SERIAL=… CAMERA_PASSWORD=… \
    DATA_PATH=/tmp/ezviz INGRESS_ENTRY=/ python3 server.py
  # serves on http://localhost:8099  — try /api/health, /api/snapshot, /
  ```
  In production `run.sh` (s6/bashio) reads `/data/options.json` and exports the
  same env vars before launching `server.py`. Anything `server.py` needs must be
  wired through both `config.yaml` (option + schema) **and** `run.sh` (export).

- **Releasing a change**: bump `version:` in `ezviz-camera-proxy/config.yaml`
  (HA detects updates only via this field) and add an entry to
  `ezviz-camera-proxy/CHANGELOG.md` (Keep a Changelog format). The CHANGELOG and
  `config.yaml` version are the source of truth for what shipped.

## Architecture (the parts that need multiple files to understand)

Runtime flow: `run.sh` → `server.py` (Flask) → `ezviz_client.py` (Cloud wrapper).

- **`server.py`** — Flask app on port 8099. A **single daemon thread**
  (`_snapshot_worker`) polls the cloud every `SNAPSHOT_INTERVAL` seconds and
  writes the latest JPEG to `/data/snapshots/current.jpg`; all HTTP routes serve
  from that cached file / cached status, so requests never block on the cloud.
  Shared state (`_last_status`, `_last_events`, `_snapshot_error`, …) lives in
  module globals guarded by the worker; the Ezviz client is a lazily-built
  singleton behind `_client_lock`. The worker tracks `consecutive_errors` and
  backs off (and force re-logins on `EzvizAuthError`).

- **`ezviz_client.py`** — wraps `pyezvizapi`. **The central reason this wrapper
  exists:** on the HP2, `pyezvizapi.get_device_infos()` crashes with
  `'str' object has no attribute 'get'` because the HP2 returns *strings* where
  the library expects *dicts* in the pagelist `CLOUD`/other sections. So the
  wrapper calls the internal `client._get_page_list()` directly and re-parses
  every section defensively (`_safe_get_device_data`, `isinstance` guards
  everywhere, JSON-string fields like `optionals`/`supportExt` parsed manually).
  Treat all pagelist data as untrusted/loosely-typed — keep the `isinstance`
  guards when editing.

- **Snapshot acquisition is best-effort with 5 fallbacks** (`get_snapshot`,
  strategies 0–4): device `picUrl` → latest alarm pic → `capture_picture` (wakes
  the camera) → device messages list → cached alarm pic. The HP2 is battery
  powered and sleeps, so any single source is often empty; this ladder is
  intentional. `/api/snapshot` falls back to a generated placeholder JPEG.

- **Ingress prefixing** — HA serves the add-on under a dynamic path passed as
  `INGRESS_ENTRY`. `server.py` exposes an `ingress_url()` Jinja global so
  `templates/index.html` builds correct absolute URLs; preserve this when adding
  links/endpoints or the Web UI breaks under Ingress.

- **Web UI** — `templates/index.html`, single page, Tailwind via CDN, dark HA
  theme. No build step.

## Known gap to be aware of

**MQTT event publishing is documented but NOT implemented.** `enable_mqtt_events`
exists in `config.yaml`/`run.sh`/`translations/en.yaml`, `paho-mqtt` is in
`requirements.txt`, and README/DOCS describe `homeassistant/camera/ezviz/<serial>/
{doorbell,motion}` topics — but there is no MQTT code in `rootfs/app/`. The flag is
read into `ENABLE_MQTT_EVENTS` and otherwise unused. Implementing MQTT (or removing
the promise) is a real outstanding task, not a bug to "fix" by accident.

## Conventions

- Timezone-aware UTC only (`datetime.now(timezone.utc)`); `utcnow()` was
  deliberately removed.
- Never log credentials or raw snapshot bytes; `/api/status` strips the `raw`
  field before returning.
- Note the repo URL is inconsistent across files (`gilbert82` vs `g-stuecheli`);
  `repository.yaml` / `config.yaml` use `gilbert82/ha-ezviz-stream`.
