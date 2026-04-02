"""
Ezviz Cloud API Client Wrapper
Wraps pyezvizapi (v1.0.x) for use in the HA Add-on.
Handles authentication, token caching, device status and snapshots.
"""

import io
import os
import json
import time
import logging
import threading
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class EzvizClientError(Exception):
    """Base exception for EzvizClient errors."""
    pass


class EzvizAuthError(EzvizClientError):
    """Authentication failed."""
    pass


class EzvizDeviceError(EzvizClientError):
    """Device operation failed."""
    pass


class EzvizClient:
    """
    Wrapper around pyezvizapi that handles:
    - Login / token caching
    - Auto-reconnect on session expiry
    - Device status, snapshot and alarm retrieval

    Compatible with pyezvizapi >= 1.0.0
    """

    TOKEN_CACHE_FILE = "/data/ezviz_token.json"
    TOKEN_EXPIRY_HOURS = 23  # Ezviz tokens last ~24h; refresh before expiry

    def __init__(
        self,
        username: str,
        password: str,
        region: str = "apiieu.ezvizlife.com",
        camera_serial: str = "",
        camera_password: str = "",
    ):
        self.username = username
        self.password = password
        self.region = region
        self.camera_serial = camera_serial
        self.camera_password = camera_password  # verification code (for future use)

        self._client = None  # pyezvizapi.EzvizClient instance
        self._camera = None  # pyezvizapi.EzvizCamera instance
        self._lock = threading.Lock()
        self._last_login: datetime | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """
        Authenticate with the Ezviz Cloud API.
        Returns True on success, raises EzvizAuthError on failure.
        """
        with self._lock:
            return self._login_locked()

    def _login_locked(self) -> bool:
        """Login (must be called while holding _lock)."""
        try:
            from pyezvizapi import EzvizClient as _EzvizClient
        except ImportError as e:
            raise EzvizClientError(
                "pyezvizapi is not installed. Check requirements.txt."
            ) from e

        logger.info("Authenticating with Ezviz Cloud (%s)...", self.region)
        try:
            client = _EzvizClient(
                account=self.username,
                password=self.password,
                url=self.region,
            )
            client.login()

            self._client = client
            self._camera = None  # Reset camera on re-login
            self._last_login = datetime.now(timezone.utc)

            logger.info("Ezviz authentication successful")
            return True

        except Exception as e:
            error_msg = str(e)
            logger.error("Ezviz authentication failed: %s", error_msg)
            if "verification" in error_msg.lower() or "captcha" in error_msg.lower():
                raise EzvizAuthError(
                    "Two-factor authentication or CAPTCHA required. "
                    "Please log in via the Ezviz app once to clear it."
                ) from e
            raise EzvizAuthError(f"Login failed: {error_msg}") from e

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid session, re-login if needed. Must hold _lock."""
        if self._client is None or (
            self._last_login
            and datetime.now(timezone.utc) - self._last_login > timedelta(hours=self.TOKEN_EXPIRY_HOURS)
        ):
            logger.info("Session expired or not initialized, re-authenticating...")
            self._login_locked()

    def _get_camera(self):
        """
        Get an EzvizCamera instance for the configured serial.
        Must be called while holding _lock.
        """
        self._ensure_authenticated()
        if not self.camera_serial:
            raise EzvizDeviceError("No camera serial configured.")

        # Re-use cached camera object
        if self._camera is not None:
            return self._camera

        try:
            from pyezvizapi import EzvizCamera as _EzvizCamera

            # pyezvizapi v1.0.x: EzvizCamera(client, serial, device_obj=None)
            # When device_obj is None, it calls client.get_device_infos(serial)
            camera = _EzvizCamera(self._client, self.camera_serial)
            self._camera = camera
            return camera

        except Exception as e:
            logger.error("Failed to load camera %s: %s", self.camera_serial, e)
            # Invalidate client to force re-login next time
            self._client = None
            self._camera = None
            raise EzvizDeviceError(f"Could not load camera: {e}") from e

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def get_device_status(self) -> dict:
        """
        Return a dict with device status information.
        Uses EzvizCamera.status() which returns CameraStatus TypedDict.
        """
        with self._lock:
            try:
                camera = self._get_camera()
                status_data = camera.status(refresh=True)

                # status_data is a CameraStatus dict with many keys
                result = {
                    "serial": status_data.get("serial", self.camera_serial),
                    "name": status_data.get("name", "HP2 Door Viewer"),
                    "online": status_data.get("status") == 1,
                    "status_code": status_data.get("status"),
                    "battery_level": status_data.get("battery_level"),
                    "local_ip": status_data.get("local_ip"),
                    "wan_ip": status_data.get("wan_ip"),
                    "version": status_data.get("version", ""),
                    "device_category": status_data.get("device_category"),
                    "device_sub_category": status_data.get("device_sub_category"),
                    "alarm_notify": status_data.get("alarm_notify"),
                    "alarm_sound_mod": status_data.get("alarm_sound_mod"),
                    "encrypted": status_data.get("encrypted"),
                    "local_rtsp_port": status_data.get("local_rtsp_port"),
                    "last_alarm_time": status_data.get("last_alarm_time"),
                    "last_alarm_pic": status_data.get("last_alarm_pic"),
                    "last_alarm_type": status_data.get("last_alarm_type_name"),
                    "motion_trigger": status_data.get("Motion_Trigger"),
                    "pir_status": status_data.get("PIR_Status"),
                    "is_sleeping": bool(status_data.get("switches", {}).get(21, False)),  # DeviceSwitchType.AUTO_SLEEP = 21
                    "mac_address": status_data.get("mac_address"),
                    "supported_channels": status_data.get("supported_channels"),
                    "battery_work_mode": status_data.get("battery_camera_work_mode"),
                    "upgrade_available": status_data.get("upgrade_available"),
                }
                return result

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_device_status failed: %s", e)
                self._client = None
                self._camera = None
                raise EzvizDeviceError(f"Status fetch failed: {e}") from e

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """
        Fetch the latest snapshot image.
        Strategy:
        1. Try to download the last_alarm_pic URL from status
        2. Try to trigger capture_picture and get the result
        3. Return None if all fails
        """
        with self._lock:
            try:
                camera = self._get_camera()
                status_data = camera.status(refresh=True)

                # Strategy 1: Get the last alarm picture URL
                pic_url = status_data.get("last_alarm_pic", "")
                if pic_url and pic_url.startswith("http"):
                    try:
                        resp = requests.get(pic_url, timeout=15)
                        if resp.status_code == 200 and len(resp.content) > 100:
                            logger.debug("Snapshot from alarm pic: %d bytes", len(resp.content))
                            return resp.content
                    except Exception as e:
                        logger.warning("Failed to download alarm pic: %s", e)

                # Strategy 2: Try capture_picture API
                try:
                    self._ensure_authenticated()
                    result = self._client.capture_picture(
                        serial=self.camera_serial,
                        channel=1,
                    )
                    # The API may return a URL in the result
                    if isinstance(result, dict):
                        cap_url = result.get("picUrl") or result.get("data", {}).get("picUrl", "")
                        if cap_url and cap_url.startswith("http"):
                            resp = requests.get(cap_url, timeout=15)
                            if resp.status_code == 200 and len(resp.content) > 100:
                                logger.debug("Snapshot from capture: %d bytes", len(resp.content))
                                return resp.content
                except Exception as e:
                    logger.debug("capture_picture not available: %s", e)

                # Strategy 3: Try device messages list for recent images
                try:
                    self._ensure_authenticated()
                    msgs = self._client.get_device_messages_list(
                        serials=self.camera_serial,
                        limit=5,
                    )
                    messages = msgs.get("message") or msgs.get("messages") or []
                    if isinstance(messages, list):
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            msg_pic = msg.get("picUrl") or msg.get("alarmPicUrl") or ""
                            if msg_pic and msg_pic.startswith("http"):
                                resp = requests.get(msg_pic, timeout=15)
                                if resp.status_code == 200 and len(resp.content) > 100:
                                    logger.debug("Snapshot from message: %d bytes", len(resp.content))
                                    return resp.content
                except Exception as e:
                    logger.debug("Messages list fallback failed: %s", e)

                logger.warning("No snapshot source available")
                return None

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_snapshot failed: %s", e)
                self._client = None
                self._camera = None
                raise EzvizDeviceError(f"Snapshot fetch failed: {e}") from e

    # ------------------------------------------------------------------
    # Alarm / Events
    # ------------------------------------------------------------------

    def get_alarm_list(self, max_count: int = 10) -> list[dict]:
        """
        Return a list of recent alarm events using the unified messages API.
        """
        with self._lock:
            try:
                self._ensure_authenticated()
                msgs = self._client.get_device_messages_list(
                    serials=self.camera_serial,
                    limit=min(max_count, 50),
                )
                messages = msgs.get("message") or msgs.get("messages") or []
                result = []
                if isinstance(messages, list):
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        result.append({
                            "alarm_id": msg.get("msgId", ""),
                            "alarm_type": msg.get("sampleName") or msg.get("alarmType", ""),
                            "alarm_time": msg.get("msgTimeStr") or msg.get("alarmStartTimeStr", ""),
                            "alarm_pic_url": msg.get("picUrl") or msg.get("alarmPicUrl", ""),
                            "device_serial": msg.get("deviceSerial", ""),
                        })
                return result

            except Exception as e:
                logger.error("get_alarm_list failed: %s", e)
                # Return empty list instead of raising — non-critical
                return []

    # ------------------------------------------------------------------
    # Device list
    # ------------------------------------------------------------------

    def get_all_devices(self) -> list[dict]:
        """
        Return a list of all cameras/devices associated with the account.
        Uses load_cameras() which returns status dicts keyed by serial.
        """
        with self._lock:
            try:
                self._ensure_authenticated()
                cameras = self._client.load_cameras()
                result = []
                if isinstance(cameras, dict):
                    for serial, cam_status in cameras.items():
                        if isinstance(cam_status, dict):
                            result.append({
                                "serial": serial,
                                "name": cam_status.get("name", serial),
                                "online": cam_status.get("status") == 1,
                                "model": cam_status.get("device_sub_category", ""),
                                "battery_level": cam_status.get("battery_level"),
                            })
                return result

            except Exception as e:
                logger.error("get_all_devices failed: %s", e)
                self._client = None
                self._camera = None
                raise EzvizDeviceError(f"Device list failed: {e}") from e

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True if we currently have an authenticated client."""
        return self._client is not None

    def invalidate_session(self) -> None:
        """Force re-login on next operation."""
        with self._lock:
            if self._client:
                try:
                    self._client.close_session()
                except Exception:
                    pass
            self._client = None
            self._camera = None
            logger.info("Session invalidated, will re-authenticate on next call")
