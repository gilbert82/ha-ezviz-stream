"""
Ezviz Cloud API Client Wrapper
Wraps pyezvizapi for use in the HA Add-on.
Handles authentication, token caching, device status and snapshots.
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
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
        self.camera_password = camera_password

        self._client = None
        self._camera = None
        self._token_data: dict = {}
        self._lock = threading.Lock()
        self._last_login: datetime | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _load_cached_token(self) -> dict | None:
        """Load token from disk cache if not expired."""
        try:
            if not Path(self.TOKEN_CACHE_FILE).exists():
                return None
            with open(self.TOKEN_CACHE_FILE, "r") as f:
                data = json.load(f)
            expires_at = datetime.fromisoformat(data.get("expires_at", "2000-01-01"))
            if datetime.utcnow() < expires_at:
                logger.debug("Using cached Ezviz token (expires %s)", expires_at)
                return data
            logger.info("Cached token expired, will re-authenticate")
        except Exception as e:
            logger.warning("Could not load token cache: %s", e)
        return None

    def _save_token_cache(self, token_data: dict) -> None:
        """Save token to disk with expiry timestamp."""
        try:
            expires_at = datetime.utcnow() + timedelta(hours=self.TOKEN_EXPIRY_HOURS)
            token_data["expires_at"] = expires_at.isoformat()
            with open(self.TOKEN_CACHE_FILE, "w") as f:
                json.dump(token_data, f)
        except Exception as e:
            logger.warning("Could not save token cache: %s", e)

    def login(self) -> bool:
        """
        Authenticate with the Ezviz Cloud API.
        Returns True on success, raises EzvizAuthError on failure.
        """
        with self._lock:
            try:
                from pyezvizapi import EzvizClient as _EzvizClient
                from pyezvizapi.exceptions import (
                    AuthTestResultFailed,
                    EzvizAuthVerificationCode,
                    PyEzvizError,
                )
            except ImportError as e:
                raise EzvizClientError(
                    "pyezvizapi is not installed. Check requirements.txt."
                ) from e

            # Check token cache first
            cached = self._load_cached_token()
            if cached and self._client is not None:
                logger.debug("Already authenticated with valid token")
                return True

            logger.info("Authenticating with Ezviz Cloud (%s)...", self.region)
            try:
                client = _EzvizClient(
                    self.username,
                    self.password,
                    self.region,
                )
                login_response = client.login()

                self._client = client
                self._last_login = datetime.utcnow()

                # Extract token from login response for caching
                token_data = {}
                if isinstance(login_response, dict):
                    token_data = login_response
                self._save_token_cache(token_data)

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
        """Ensure we have a valid session, re-login if needed."""
        # Re-login if session is older than TOKEN_EXPIRY_HOURS
        if self._client is None or (
            self._last_login
            and datetime.utcnow() - self._last_login > timedelta(hours=self.TOKEN_EXPIRY_HOURS)
        ):
            logger.info("Session expired or not initialized, re-authenticating...")
            self.login()

    def _get_camera(self):
        """Get an EzvizCamera instance for the configured serial."""
        self._ensure_authenticated()
        if not self.camera_serial:
            raise EzvizDeviceError("No camera serial configured.")
        try:
            from pyezvizapi import EzvizCamera
            camera = EzvizCamera(
                self._client,
                self.camera_serial,
                self.camera_password or None,
            )
            camera.load()
            return camera
        except Exception as e:
            logger.error("Failed to load camera %s: %s", self.camera_serial, e)
            # Invalidate client to force re-login next time
            self._client = None
            raise EzvizDeviceError(f"Could not load camera: {e}") from e

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def get_device_status(self) -> dict:
        """
        Return a dict with device status information.
        Keys: serial, name, online, battery_level, wifi_signal,
              last_alarm_time, is_sleeping, version
        """
        with self._lock:
            try:
                camera = self._get_camera()
                props = camera.status()

                status = {
                    "serial": self.camera_serial,
                    "name": props.get("name", "HP2 Door Viewer"),
                    "online": props.get("online", False),
                    "battery_level": props.get("battery_level", None),
                    "wifi_signal": props.get("wifiSignal", None),
                    "last_alarm_time": props.get("last_alarm_time", None),
                    "is_sleeping": props.get("sleeping", False),
                    "version": props.get("version", ""),
                    "alarm_sound_mod": props.get("alarm_sound_mod", None),
                    "privacy": props.get("privacy", False),
                    "raw": props,
                }
                return status

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_device_status failed: %s", e)
                self._client = None
                raise EzvizDeviceError(f"Status fetch failed: {e}") from e

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """
        Fetch the latest snapshot image from the Ezviz Cloud.
        Returns raw JPEG bytes or None on failure.
        """
        with self._lock:
            try:
                camera = self._get_camera()
                image_data = camera.get_image()
                if image_data:
                    logger.debug("Snapshot fetched: %d bytes", len(image_data))
                    return image_data
                logger.warning("Snapshot returned empty data")
                return None

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_snapshot failed: %s", e)
                self._client = None
                raise EzvizDeviceError(f"Snapshot fetch failed: {e}") from e

    # ------------------------------------------------------------------
    # Alarm / Events
    # ------------------------------------------------------------------

    def get_last_alarm(self) -> dict | None:
        """
        Return the most recent alarm event dict.
        Keys: alarm_id, alarm_type, alarm_time, alarm_pic_url
        """
        with self._lock:
            try:
                camera = self._get_camera()
                alarm = camera.last_alarm()
                if alarm:
                    return {
                        "alarm_id": alarm.get("alarmId", ""),
                        "alarm_type": alarm.get("alarmType", ""),
                        "alarm_time": alarm.get("alarmStartTime", ""),
                        "alarm_pic_url": alarm.get("alarmPicUrl", ""),
                        "raw": alarm,
                    }
                return None

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_last_alarm failed: %s", e)
                self._client = None
                raise EzvizDeviceError(f"Alarm fetch failed: {e}") from e

    def get_alarm_list(self, max_count: int = 10) -> list[dict]:
        """
        Return a list of recent alarm events.
        """
        with self._lock:
            try:
                self._ensure_authenticated()
                alarms_raw = self._client.get_alarmlist(
                    serial=self.camera_serial, max_count=max_count
                )
                result = []
                for alarm in alarms_raw or []:
                    result.append(
                        {
                            "alarm_id": alarm.get("alarmId", ""),
                            "alarm_type": alarm.get("alarmType", ""),
                            "alarm_time": alarm.get("alarmStartTime", ""),
                            "alarm_pic_url": alarm.get("alarmPicUrl", ""),
                        }
                    )
                return result

            except Exception as e:
                logger.error("get_alarm_list failed: %s", e)
                self._client = None
                # Return empty list instead of raising — non-critical
                return []

    # ------------------------------------------------------------------
    # Device list
    # ------------------------------------------------------------------

    def get_all_devices(self) -> list[dict]:
        """
        Return a list of all cameras/devices associated with the account.
        """
        with self._lock:
            try:
                self._ensure_authenticated()
                devices = self._client.get_all_camera()
                result = []
                for serial, props in (devices or {}).items():
                    result.append(
                        {
                            "serial": serial,
                            "name": props.get("name", serial),
                            "online": props.get("online", False),
                            "model": props.get("model", ""),
                        }
                    )
                return result

            except Exception as e:
                logger.error("get_all_devices failed: %s", e)
                self._client = None
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
            self._client = None
            logger.info("Session invalidated, will re-authenticate on next call")
