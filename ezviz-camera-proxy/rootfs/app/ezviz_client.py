"""
Ezviz Cloud API Client Wrapper
Wraps pyezvizapi (v1.0.x) for use in the HA Add-on.
Handles authentication, token caching, device status and snapshots.

NOTE: The HP2 camera returns non-standard data in the CLOUD section of the
pagelist API, causing pyezvizapi's get_device_infos() to crash with
'str' object has no attribute 'get'.  This wrapper works around that by
calling the pagelist API directly and parsing the response safely.
"""

import io
import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


class EzvizClientError(Exception):
    """Base exception for EzvizClient errors."""


class EzvizAuthError(EzvizClientError):
    """Authentication failed."""


class EzvizDeviceError(EzvizClientError):
    """Device operation failed."""


class EzvizClient:
    """
    Wrapper around pyezvizapi that handles:
    - Login / token caching
    - Auto-reconnect on session expiry
    - Device status, snapshot and alarm retrieval

    Compatible with pyezvizapi >= 1.0.0.
    Works around HP2-specific pagelist format issues.
    """

    TOKEN_EXPIRY_HOURS = 23

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

        self._client = None  # pyezvizapi.EzvizClient instance
        self._lock = threading.Lock()
        self._last_login: datetime | None = None

        # Cached data from last successful pagelist fetch
        self._cached_device_data: dict = {}
        self._cached_status: dict = {}

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """Authenticate with the Ezviz Cloud API."""
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

    def _ensure_authenticated(self):
        """Ensure we have a valid session. Must hold _lock."""
        if self._client is None or (
            self._last_login
            and datetime.now(timezone.utc) - self._last_login
            > timedelta(hours=self.TOKEN_EXPIRY_HOURS)
        ):
            logger.info("Session expired or not initialized, re-authenticating...")
            self._login_locked()

    # ------------------------------------------------------------------
    # Safe pagelist fetch (works around HP2 'str' has no attr 'get')
    # ------------------------------------------------------------------

    def _safe_get_page_list(self) -> dict:
        """
        Fetch the pagelist via pyezvizapi's internal API and return the raw dict.
        This is the same as client._get_page_list() but we catch errors.
        """
        self._ensure_authenticated()
        try:
            data = self._client._get_page_list()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error("_get_page_list failed: %s", e)
            return {}

    def _safe_get_device_data(self, serial: str) -> dict:
        """
        Build device data dict for the given serial from the raw pagelist.
        This is a safe reimplementation of get_device_infos() that handles
        the HP2's non-standard CLOUD section (strings instead of dicts).
        """
        pages = self._safe_get_page_list()
        if not pages:
            return {}

        # Find the device in deviceInfos
        device_info = None
        for dev in pages.get("deviceInfos", []) or []:
            if isinstance(dev, dict) and dev.get("deviceSerial") == serial:
                device_info = dev
                break

        if device_info is None:
            logger.warning("Device %s not found in pagelist deviceInfos", serial)
            # List available devices for debugging
            available = []
            for dev in pages.get("deviceInfos", []) or []:
                if isinstance(dev, dict):
                    available.append(f"{dev.get('deviceSerial')} ({dev.get('name', '?')})")
            if available:
                logger.info("Available devices: %s", ", ".join(available))
            return {}

        # Safely extract sections — each section might be keyed by serial or resource ID
        def safe_get(section_name: str, key: str) -> dict:
            section = pages.get(section_name)
            if not isinstance(section, dict):
                return {}
            val = section.get(key)
            return val if isinstance(val, dict) else {}

        result = {
            "deviceInfos": device_info,
            "STATUS": safe_get("STATUS", serial),
            "CONNECTION": safe_get("CONNECTION", serial),
            "P2P": safe_get("P2P", serial),
            "KMS": safe_get("KMS", serial),
            "QOS": safe_get("QOS", serial),
            "NODISTURB": safe_get("NODISTURB", serial),
            "FEATURE": safe_get("FEATURE", serial),
            "UPGRADE": safe_get("UPGRADE", serial),
            "FEATURE_INFO": safe_get("FEATURE_INFO", serial),
            "SWITCH": safe_get("SWITCH", serial),
            "WIFI": safe_get("WIFI", serial),
            "TIME_PLAN": safe_get("TIME_PLAN", serial),
        }

        # Parse supportExt if it's a JSON string
        support_ext = device_info.get("supportExt")
        if isinstance(support_ext, str) and support_ext:
            try:
                result["deviceInfos"]["supportExt"] = json.loads(support_ext)
            except (ValueError, TypeError):
                pass

        # Parse optionals if it's a JSON string (common in STATUS)
        optionals = result["STATUS"].get("optionals")
        if isinstance(optionals, str) and optionals:
            try:
                result["STATUS"]["optionals"] = json.loads(optionals)
            except (ValueError, TypeError):
                pass
        elif isinstance(optionals, dict):
            # Recursively parse any string values that are JSON
            for k, v in list(optionals.items()):
                if isinstance(v, str):
                    try:
                        optionals[k] = json.loads(v)
                    except (ValueError, TypeError):
                        pass

        self._cached_device_data = result

        # Log device data structure on first successful fetch for debugging
        if not hasattr(self, '_logged_structure'):
            self._logged_structure = True
            logger.info("=== Pagelist top-level keys: %s ===", list(pages.keys()))
            logger.info("=== deviceInfos fields for %s: %s ===",
                        serial, list(device_info.keys()) if isinstance(device_info, dict) else "N/A")
            # Log all string values in deviceInfos that might be URLs
            for k, v in device_info.items():
                if isinstance(v, str) and (v.startswith("http") or "pic" in k.lower() or "url" in k.lower() or "image" in k.lower()):
                    logger.info("  deviceInfos[%s] = %s", k, v[:200])
            # Log STATUS keys
            logger.info("=== STATUS fields: %s ===",
                        list(result["STATUS"].keys()) if isinstance(result["STATUS"], dict) else "N/A")
            # Log any URL fields in STATUS
            if isinstance(result["STATUS"], dict):
                for k, v in result["STATUS"].items():
                    if isinstance(v, str) and (v.startswith("http") or "pic" in k.lower() or "url" in k.lower()):
                        logger.info("  STATUS[%s] = %s", k, v[:200])

        return result

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def get_device_status(self) -> dict:
        """Return a dict with device status information."""
        with self._lock:
            try:
                device = self._safe_get_device_data(self.camera_serial)
                if not device:
                    raise EzvizDeviceError(
                        f"Device {self.camera_serial} not found in account"
                    )

                dev_info = device.get("deviceInfos", {})
                status = device.get("STATUS", {})
                connection = device.get("CONNECTION", {})
                optionals = status.get("optionals", {})
                if not isinstance(optionals, dict):
                    optionals = {}

                # Parse SWITCH list into a dict
                switch_data = device.get("SWITCH")
                switches = {}
                if isinstance(switch_data, list):
                    for item in switch_data:
                        if isinstance(item, dict):
                            t = item.get("type")
                            en = item.get("enable")
                            if t is not None:
                                switches[int(t)] = bool(en)
                elif isinstance(switch_data, dict):
                    switches = switch_data

                # Alarm info
                last_alarm_pic = ""
                last_alarm_time = ""
                last_alarm_type = ""
                try:
                    alarm_resp = self._client.get_alarminfo(
                        serial=self.camera_serial, limit=1
                    )
                    alarm_list = (
                        alarm_resp.get("alarmList")
                        or alarm_resp.get("page", {}).get("alarmList")
                        or []
                    )
                    if isinstance(alarm_list, list) and alarm_list:
                        latest = alarm_list[0]
                        last_alarm_pic = latest.get("alarmPicUrl", "")
                        last_alarm_time = latest.get("alarmStartTimeStr", "")
                        last_alarm_type = latest.get("sampleName") or latest.get(
                            "alarmType", ""
                        )
                except Exception as e:
                    logger.debug("Alarm info fetch failed: %s", e)

                result = {
                    "serial": self.camera_serial,
                    "name": dev_info.get("name", "HP2"),
                    "online": dev_info.get("status") == 1,
                    "status_code": dev_info.get("status"),
                    "battery_level": optionals.get("powerRemaining"),
                    "local_ip": connection.get("localIp") or dev_info.get("localIp"),
                    "wan_ip": connection.get("netIp"),
                    "version": dev_info.get("version", ""),
                    "device_category": dev_info.get("deviceCategory"),
                    "device_sub_category": dev_info.get("deviceSubCategory"),
                    "alarm_notify": bool(status.get("globalStatus")),
                    "alarm_sound_mod": status.get("alarmSoundMode"),
                    "encrypted": bool(status.get("isEncrypt")),
                    "local_rtsp_port": connection.get("localRtspPort", "0"),
                    "last_alarm_time": last_alarm_time,
                    "last_alarm_pic": last_alarm_pic,
                    "last_alarm_type": last_alarm_type,
                    "motion_trigger": bool(status.get("pirStatus")),
                    "pir_status": status.get("pirStatus"),
                    "is_sleeping": bool(switches.get(21, False)),
                    "mac_address": dev_info.get("mac"),
                    "supported_channels": dev_info.get("channelNumber"),
                    "battery_work_mode": optionals.get("batteryCameraWorkMode"),
                    "upgrade_available": device.get("UPGRADE", {}).get("isNeedUpgrade")
                    == 3,
                }
                self._cached_status = result
                return result

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_device_status failed: %s\n%s", e, traceback.format_exc())
                self._client = None
                raise EzvizDeviceError(f"Status fetch failed: {e}") from e

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def get_snapshot(self) -> bytes | None:
        """
        Fetch the latest snapshot image.
        Strategies (tried in order):
        0. picUrl from deviceInfos in pagelist (device cover image)
        1. last_alarm_pic from alarminfo API
        2. capture_picture API (wakes camera)
        3. device_messages_list for recent event images
        4. cached alarm pic from last status call
        """
        with self._lock:
            try:
                self._ensure_authenticated()

                # Strategy 0: Device cover image (picUrl in deviceInfos)
                device_pic = self._get_device_pic_url()
                if device_pic:
                    logger.info("Strategy 0: trying device picUrl: %s", device_pic[:120])
                    img = self._download_image(device_pic)
                    if img:
                        logger.info("Snapshot from device picUrl: %d bytes", len(img))
                        return img
                    else:
                        logger.info("Strategy 0: device picUrl download failed or empty")
                else:
                    logger.info("Strategy 0: no picUrl in device data")

                # Strategy 1: Get last alarm picture from alarminfo API
                pic_url = self._get_latest_alarm_pic()
                if pic_url:
                    logger.info("Strategy 1: trying alarm pic: %s", pic_url[:120])
                    img = self._download_image(pic_url)
                    if img:
                        logger.info("Snapshot from alarm pic: %d bytes", len(img))
                        return img
                    else:
                        logger.info("Strategy 1: alarm pic download failed or empty")
                else:
                    logger.info("Strategy 1: no alarm pic URL available")

                # Strategy 2: Try capture_picture API
                try:
                    logger.info("Strategy 2: trying capture_picture API...")
                    result = self._client.capture_picture(
                        serial=self.camera_serial, channel=1
                    )
                    logger.info("Strategy 2: capture_picture returned: %s",
                                str(result)[:300] if result else "None")
                    if isinstance(result, dict):
                        cap_url = (
                            result.get("picUrl")
                            or result.get("data", {}).get("picUrl", "")
                            if isinstance(result.get("data"), dict)
                            else ""
                        )
                        if cap_url:
                            img = self._download_image(cap_url)
                            if img:
                                logger.info("Snapshot from capture: %d bytes", len(img))
                                return img
                            else:
                                logger.info("Strategy 2: capture pic download failed")
                except Exception as e:
                    logger.info("Strategy 2: capture_picture failed: %s", e)

                # Strategy 3: Device messages list
                try:
                    logger.info("Strategy 3: trying device_messages_list...")
                    msgs = self._client.get_device_messages_list(
                        serials=self.camera_serial, limit=5
                    )
                    logger.info("Strategy 3: messages response keys: %s",
                                list(msgs.keys()) if isinstance(msgs, dict) else type(msgs))
                    messages = msgs.get("message") or msgs.get("messages") or []
                    if isinstance(messages, list):
                        logger.info("Strategy 3: found %d messages", len(messages))
                        for i, msg in enumerate(messages):
                            if not isinstance(msg, dict):
                                continue
                            msg_pic = (
                                msg.get("picUrl")
                                or msg.get("alarmPicUrl")
                                or ""
                            )
                            if msg_pic:
                                logger.info("Strategy 3: message[%d] pic: %s", i, msg_pic[:120])
                                img = self._download_image(msg_pic)
                                if img:
                                    logger.info("Snapshot from message: %d bytes", len(img))
                                    return img
                        logger.info("Strategy 3: no usable picture in messages")
                    else:
                        logger.info("Strategy 3: messages list empty or not a list")
                except Exception as e:
                    logger.info("Strategy 3: messages list failed: %s", e)

                # Strategy 4: Use cached alarm pic from last status
                cached_pic = self._cached_status.get("last_alarm_pic", "")
                if cached_pic:
                    logger.info("Strategy 4: trying cached alarm pic: %s", cached_pic[:120])
                    img = self._download_image(cached_pic)
                    if img:
                        logger.info("Snapshot from cached status: %d bytes", len(img))
                        return img
                    else:
                        logger.info("Strategy 4: cached pic download failed")
                else:
                    logger.info("Strategy 4: no cached alarm pic")

                logger.warning("All 5 snapshot strategies failed — no image available")
                return None

            except EzvizDeviceError:
                raise
            except Exception as e:
                logger.error("get_snapshot failed: %s", e)
                self._client = None
                raise EzvizDeviceError(f"Snapshot fetch failed: {e}") from e

    def _get_device_pic_url(self) -> str:
        """Get the device's cover/status picture URL from cached pagelist data."""
        dev_info = self._cached_device_data.get("deviceInfos", {})
        if not isinstance(dev_info, dict):
            return ""

        # Try various known picture URL fields
        for key in ("picUrl", "devicePicUrl", "statusPicUrl", "coverUrl"):
            url = dev_info.get(key, "")
            if url and isinstance(url, str) and url.startswith("http"):
                return url

        # Check in STATUS section
        status = self._cached_device_data.get("STATUS", {})
        if isinstance(status, dict):
            for key in ("picUrl", "devicePicUrl"):
                url = status.get(key, "")
                if url and isinstance(url, str) and url.startswith("http"):
                    return url

        return ""

    def _get_latest_alarm_pic(self) -> str:
        """Get the URL of the latest alarm picture."""
        try:
            alarm_resp = self._client.get_alarminfo(
                serial=self.camera_serial, limit=1
            )
            alarm_list = (
                alarm_resp.get("alarmList")
                or alarm_resp.get("page", {}).get("alarmList")
                or []
            )
            if isinstance(alarm_list, list) and alarm_list:
                pic = alarm_list[0].get("alarmPicUrl", "")
                if pic and pic.startswith("http"):
                    return pic
        except Exception as e:
            logger.debug("get_alarminfo failed: %s", e)
        return ""

    def _download_image(self, url: str) -> bytes | None:
        """Download an image from a URL, return bytes or None."""
        if not url or not url.startswith("http"):
            return None
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and len(resp.content) > 100:
                return resp.content
        except Exception as e:
            logger.debug("Image download failed from %s: %s", url[:80], e)
        return None

    # ------------------------------------------------------------------
    # Alarm / Events
    # ------------------------------------------------------------------

    def get_alarm_list(self, max_count: int = 10) -> list[dict]:
        """Return a list of recent alarm events."""
        with self._lock:
            try:
                self._ensure_authenticated()

                # Try unified messages first
                try:
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
                            result.append(
                                {
                                    "alarm_id": msg.get("msgId", ""),
                                    "alarm_type": msg.get("sampleName")
                                    or msg.get("alarmType", ""),
                                    "alarm_time": msg.get("msgTimeStr")
                                    or msg.get("alarmStartTimeStr", ""),
                                    "alarm_pic_url": msg.get("picUrl")
                                    or msg.get("alarmPicUrl", ""),
                                    "device_serial": msg.get("deviceSerial", ""),
                                }
                            )
                    if result:
                        return result
                except Exception as e:
                    logger.debug("get_device_messages_list failed: %s", e)

                # Fallback: alarminfo API
                try:
                    alarm_resp = self._client.get_alarminfo(
                        serial=self.camera_serial, limit=max_count
                    )
                    alarm_list = (
                        alarm_resp.get("alarmList")
                        or alarm_resp.get("page", {}).get("alarmList")
                        or []
                    )
                    result = []
                    if isinstance(alarm_list, list):
                        for alarm in alarm_list:
                            if not isinstance(alarm, dict):
                                continue
                            result.append(
                                {
                                    "alarm_id": alarm.get("alarmId", ""),
                                    "alarm_type": alarm.get("sampleName")
                                    or alarm.get("alarmType", ""),
                                    "alarm_time": alarm.get("alarmStartTimeStr", ""),
                                    "alarm_pic_url": alarm.get("alarmPicUrl", ""),
                                    "device_serial": self.camera_serial,
                                }
                            )
                    return result
                except Exception as e:
                    logger.debug("get_alarminfo fallback failed: %s", e)

                return []

            except Exception as e:
                logger.error("get_alarm_list failed: %s", e)
                return []

    # ------------------------------------------------------------------
    # Device list
    # ------------------------------------------------------------------

    def get_all_devices(self) -> list[dict]:
        """Return a list of all devices on the account."""
        with self._lock:
            try:
                self._ensure_authenticated()
                pages = self._safe_get_page_list()
                result = []
                for dev in pages.get("deviceInfos", []) or []:
                    if not isinstance(dev, dict):
                        continue
                    serial = dev.get("deviceSerial", "")
                    status_section = pages.get("STATUS", {})
                    dev_status = (
                        status_section.get(serial, {})
                        if isinstance(status_section, dict)
                        else {}
                    )
                    optionals = dev_status.get("optionals", {})
                    if isinstance(optionals, str):
                        try:
                            optionals = json.loads(optionals)
                        except (ValueError, TypeError):
                            optionals = {}

                    result.append(
                        {
                            "serial": serial,
                            "name": dev.get("name", serial),
                            "online": dev.get("status") == 1,
                            "model": dev.get("deviceSubCategory", ""),
                            "battery_level": optionals.get("powerRemaining")
                            if isinstance(optionals, dict)
                            else None,
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
        return self._client is not None

    def invalidate_session(self) -> None:
        with self._lock:
            if self._client:
                try:
                    self._client.close_session()
                except Exception:
                    pass
            self._client = None
            logger.info("Session invalidated, will re-authenticate on next call")
