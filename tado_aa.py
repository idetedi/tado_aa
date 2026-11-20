#!/usr/bin/env python3
#
# tado_aa.py (Tado Auto-Assist for Geofencing and Open Window Detection + Temperature Limit)
# Created by Adrian Slabu <adrianslabu@icloud.com> on 11.02.2021
# Reworked in 2026 for python-tado 0.19 and tado's daily API quota.
#
"""Tado Auto-Assist: geofencing, open window mode and temperature limit.

Everything is configured through environment variables (see .env.example).
tado limits its REST API to a daily number of requests (100 without an
Auto-Assist subscription), so the script spreads its checks over the day
instead of polling every few seconds.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from pydantic import ValidationError
from PyTado.exceptions import (
    TadoCredentialsException,
    TadoException,
    TadoRateLimitException,
)
from PyTado.interface import Tado
from PyTado.models import util as _pytado_model_util
from PyTado.types import OverlayMode, Power, ZoneType

__version__ = "0.2.0"

LOG = logging.getLogger("tado_aa")

HTTP_TIMEOUT = 30  # seconds, for every request to tado
NOTIFY_TIMEOUT = 10  # seconds, for ntfy / Telegram
HEARTBEAT_MAX_AGE = 180  # seconds, used by --healthcheck
SLEEP_CHUNK = 30  # seconds between heartbeats while waiting
MAX_RETRY_INTERVAL = 3600  # seconds, upper bound of the error back-off
RATE_LIMIT_FALLBACK_WAIT = 3600  # seconds, when tado does not say when the quota resets
MINUTES_PER_DAY = 24 * 60

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """The configuration given through the environment is not valid."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _get_number(
    env: Mapping[str, str],
    name: str,
    default: float,
    cast: Callable[[str], Any],
    minimum: float | None = None,
) -> Any:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _parse_clock(text: str, name: str) -> int:
    """'HH:MM' -> minutes since midnight. '24:00' is accepted as end of day."""
    try:
        hours_text, minutes_text = text.strip().split(":")
        hours, minutes = int(hours_text), int(minutes_text)
    except ValueError as exc:
        raise ConfigError(f"{name}: {text!r} is not a time like 06:00") from exc
    total = hours * 60 + minutes
    if not (0 <= minutes < 60 and 0 <= total <= MINUTES_PER_DAY):
        raise ConfigError(f"{name}: {text!r} is not a time like 06:00")
    return total


def _parse_active_hours(text: str) -> tuple[int, int]:
    """'06:00-24:00' -> (360, 1440). The window may cross midnight (22:00-06:00)."""
    try:
        start_text, end_text = text.split("-")
    except ValueError as exc:
        raise ConfigError(f"ACTIVE_HOURS must look like 06:00-24:00, got {text!r}") from exc
    start = _parse_clock(start_text, "ACTIVE_HOURS")
    end = _parse_clock(end_text, "ACTIVE_HOURS")
    if start == MINUTES_PER_DAY:
        raise ConfigError("ACTIVE_HOURS cannot start at 24:00, use 00:00")
    if start == end:
        raise ConfigError("ACTIVE_HOURS start and end are the same; use 00:00-24:00")
    return start, end


def _parse_zone_names(text: str) -> dict[int, str]:
    """'1=Living room,2=Bedroom' -> {1: 'Living room', 2: 'Bedroom'}."""
    names: dict[int, str] = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        zone_id, sep, name = item.partition("=")
        if not sep or not zone_id.strip().isdigit() or not name.strip():
            raise ConfigError(f"ZONE_NAMES must look like '1=Living room,2=Bedroom', got {item!r}")
        names[int(zone_id)] = name.strip()
    return names


@dataclass(frozen=True)
class Config:
    """Settings of the script. Build it with Config.from_env()."""

    data_dir: Path = Path("data")

    enable_geofencing: bool = True
    enable_open_window: bool = True
    enable_temp_limit: bool = True

    min_temp: float = 5.0
    max_temp: float = 25.0
    temp_limit_overlay_mode: OverlayMode = OverlayMode.NEXT_TIME_BLOCK

    daily_limit: int = 100  # used until tado reports the real quota
    request_reserve: int = 10  # requests kept for start-up and for changing things
    check_interval: int = 0  # seconds; 0 = computed from the quota
    min_interval: int = 60
    active_start: int = 0  # minutes since midnight
    active_end: int = MINUTES_PER_DAY
    timezone: tzinfo | None = None  # None = local time of the machine

    dry_run: bool = False
    error_retry_interval: int = 60

    ntfy_url: str = ""
    ntfy_token: str = field(default="", repr=False)
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    notify_device_names: bool = False

    zone_names: Mapping[int, str] = field(default_factory=dict)
    log_level: str = "INFO"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "refresh_token"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def text(name: str, default: str = "") -> str:
            return env.get(name, "").strip() or default

        overlay_text = text("TEMP_LIMIT_OVERLAY_MODE", "NEXT_TIME_BLOCK").upper()
        if overlay_text not in {"NEXT_TIME_BLOCK", "MANUAL", "TADO_MODE"}:
            raise ConfigError(
                "TEMP_LIMIT_OVERLAY_MODE must be NEXT_TIME_BLOCK, MANUAL or TADO_MODE, "
                f"got {overlay_text!r}"
            )

        timezone: tzinfo | None = None
        if tz_name := text("TZ"):
            try:
                timezone = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ConfigError(f"TZ: unknown time zone {tz_name!r}") from exc

        active_start, active_end = _parse_active_hours(text("ACTIVE_HOURS", "00:00-24:00"))

        log_level = text("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError(f"LOG_LEVEL: unknown level {log_level!r}")

        config = cls(
            data_dir=Path(text("TADO_DATA_DIR", "data")).expanduser().resolve(),
            enable_geofencing=_get_bool(env, "ENABLE_GEOFENCING", True),
            enable_open_window=_get_bool(env, "ENABLE_OPEN_WINDOW", True),
            enable_temp_limit=_get_bool(env, "ENABLE_TEMP_LIMIT", True),
            min_temp=_get_number(env, "MIN_TEMP", 5.0, float),
            max_temp=_get_number(env, "MAX_TEMP", 25.0, float),
            temp_limit_overlay_mode=OverlayMode(overlay_text),
            daily_limit=_get_number(env, "TADO_DAILY_LIMIT", 100, int, minimum=1),
            request_reserve=_get_number(env, "TADO_REQUEST_RESERVE", 10, int, minimum=0),
            check_interval=_get_number(env, "CHECK_INTERVAL", 0, int, minimum=0),
            min_interval=_get_number(env, "MIN_INTERVAL", 60, int, minimum=10),
            active_start=active_start,
            active_end=active_end,
            timezone=timezone,
            dry_run=_get_bool(env, "DRY_RUN", False),
            error_retry_interval=_get_number(env, "ERROR_RETRY_INTERVAL", 60, int, minimum=10),
            ntfy_url=text("NTFY_URL"),
            ntfy_token=text("NTFY_TOKEN"),
            telegram_bot_token=text("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=text("TELEGRAM_CHAT_ID"),
            notify_device_names=_get_bool(env, "NOTIFY_DEVICE_NAMES", False),
            zone_names=_parse_zone_names(text("ZONE_NAMES")),
            log_level=log_level,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not (self.enable_geofencing or self.enable_open_window or self.enable_temp_limit):
            raise ConfigError(
                "Nothing to do: ENABLE_GEOFENCING, ENABLE_OPEN_WINDOW and "
                "ENABLE_TEMP_LIMIT are all false"
            )
        if self.min_temp >= self.max_temp:
            raise ConfigError(
                f"MIN_TEMP ({self.min_temp}) must be lower than MAX_TEMP ({self.max_temp})"
            )
        if self.ntfy_url and not self.ntfy_url.startswith(("https://", "http://")):
            raise ConfigError("NTFY_URL must start with https:// or http://")
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together")


# --------------------------------------------------------------------------- #
# Request budget and active hours (pure functions, easy to test)
# --------------------------------------------------------------------------- #


def requests_per_cycle(config: Config) -> int:
    """Read requests one check costs. Changing something costs one or two more."""
    needs_zones = config.enable_open_window or config.enable_temp_limit
    cost = 1 if needs_zones else 0  # zoneStates: one call for every zone
    if config.enable_geofencing:
        cost += 1  # mobileDevices
        if not needs_zones:
            cost += 1  # home state; with zoneStates it comes for free
    return cost


def active_minutes_per_day(config: Config) -> int:
    return (config.active_end - config.active_start) % MINUTES_PER_DAY or MINUTES_PER_DAY


def compute_interval(config: Config, daily_limit: int) -> int:
    """Seconds between checks so that a whole day fits in the daily quota."""
    if config.check_interval:
        return max(config.check_interval, config.min_interval)
    per_cycle = requests_per_cycle(config)
    cycles = max((daily_limit - config.request_reserve) // per_cycle, 1)
    interval = math.ceil(active_minutes_per_day(config) * 60 / cycles)
    return max(interval, config.min_interval)


def _minute_of_day(moment: datetime) -> float:
    return moment.hour * 60 + moment.minute + moment.second / 60


def in_active_hours(config: Config, moment: datetime) -> bool:
    if active_minutes_per_day(config) == MINUTES_PER_DAY:
        return True
    minute = _minute_of_day(moment)
    if config.active_start < config.active_end:
        return config.active_start <= minute < config.active_end
    return minute >= config.active_start or minute < config.active_end


def seconds_until_active(config: Config, moment: datetime) -> int:
    """0 inside the active hours, otherwise the seconds until they start."""
    if in_active_hours(config, moment):
        return 0
    minutes = (config.active_start - _minute_of_day(moment)) % MINUTES_PER_DAY
    return max(math.ceil(minutes * 60), 1)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


class Notifier:
    """Sends short messages to ntfy and/or Telegram. Never raises."""

    def __init__(self, config: Config, session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self._config.ntfy_url or self._config.telegram_bot_token)

    def send(self, message: str) -> None:
        if self._config.ntfy_url:
            headers = {"Title": "tado Auto-Assist"}
            if self._config.ntfy_token:
                headers["Authorization"] = f"Bearer {self._config.ntfy_token}"
            self._post(
                "ntfy",
                self._config.ntfy_url,
                data=message.encode("utf-8"),
                headers=headers,
            )
        if self._config.telegram_bot_token:
            url = f"https://api.telegram.org/bot{self._config.telegram_bot_token}/sendMessage"
            self._post(
                "Telegram",
                url,
                json={"chat_id": self._config.telegram_chat_id, "text": message},
            )

    def _post(self, service: str, url: str, **kwargs: Any) -> None:
        try:
            response = self._session.post(url, timeout=NOTIFY_TIMEOUT, **kwargs)
            status = response.status_code
        except requests.RequestException as exc:
            # Only the exception type: its text contains the URL, and the
            # Telegram URL contains the bot token.
            LOG.warning("Notification via %s failed: %s", service, type(exc).__name__)
            return
        if status >= 400:
            LOG.warning("Notification via %s failed: HTTP %d", service, status)


# --------------------------------------------------------------------------- #
# The assistant
# --------------------------------------------------------------------------- #


class _TimeoutSession(requests.Session):
    """requests.Session with a default timeout (PyTado sends without one)."""

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = HTTP_TIMEOUT
        return super().send(request, **kwargs)


def _join_names(names: list[str]) -> str:
    return ", ".join(names)


def _window_detected(state: Any) -> bool:
    """True if tado saw an open window in the zone and did nothing yet.

    python-tado 0.19 has no field for 'openWindowDetected', so it ends up in the
    extra values of the model.
    """
    extra = getattr(state, "model_extra", None) or {}
    return bool(extra.get("openWindowDetected") or getattr(state, "open_window_detected", False))


class TadoAutoAssist:
    """Checks tado once per cycle and fixes home/away, open windows and limits."""

    def __init__(
        self,
        config: Config,
        tado: Any = None,
        notifier: Notifier | None = None,
    ):
        self.config = config
        self.tado = tado
        self.notifier = notifier or Notifier(config)
        self.requests_made = 0
        self._stop = threading.Event()
        self._geofencing_paused = False

    # ------------------------------ plumbing ------------------------------ #

    def stop(self, *_: Any) -> None:
        self._stop.set()

    def _now(self) -> datetime:
        return datetime.now(self.config.timezone)

    def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Every request to tado goes through here so that it is counted."""
        self.requests_made += 1
        return method(*args, **kwargs)

    def _notify(self, message: str) -> None:
        if self.notifier.enabled:
            self.notifier.send(message)

    def _change(
        self, notification: str, method: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> None:
        """Changes something in tado. If it fails, the rest of the check goes on.

        A change that tado rejects is not retried until the next check: retrying
        at once would spend the daily quota on something that keeps failing.
        """
        try:
            self._call(method, *args, **kwargs)
        except (TadoRateLimitException, TadoCredentialsException):
            raise
        except (TadoException, requests.RequestException, ValidationError) as exc:
            LOG.error(
                "The change failed (%s: %s). It will be tried again in the next check.",
                type(exc).__name__,
                exc,
            )
            return
        self._notify(notification)

    def _zone_name(self, zone_id: int) -> str:
        return self.config.zone_names.get(zone_id, f"Zone {zone_id}")

    def _rate_limit(self) -> dict[str, str]:
        if self.tado is None:
            return {}
        try:
            return dict(self.tado.rate_limit_info())
        except (AttributeError, TypeError):
            return {}

    def _rate_value(self, key: str) -> int | None:
        try:
            return int(self._rate_limit()[key])
        except (KeyError, ValueError):
            return None

    def daily_limit(self) -> int:
        """Daily quota tado reported, or the configured one until it does."""
        return self._rate_value("per-day") or self.config.daily_limit

    def interval(self) -> int:
        return compute_interval(self.config, self.daily_limit())

    def _touch_heartbeat(self) -> None:
        try:
            self.config.heartbeat_file.touch()
        except OSError as exc:
            LOG.debug("Could not write the heartbeat file: %s", exc)

    def _sleep(self, seconds: float) -> None:
        """Wait, writing the heartbeat, and return early when asked to stop."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            self._touch_heartbeat()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, SLEEP_CHUNK))

    # ------------------------------- login -------------------------------- #

    def login(self) -> None:
        """Opens the session; the first time it asks to authorise in a browser."""
        self._touch_heartbeat()
        tado = Tado(
            token_file_path=str(self.config.token_file),
            http_session=_TimeoutSession(),
            user_agent=f"tado_aa/{__version__}",
        )
        if tado.device_activation_status() == "PENDING":
            url = tado.device_verification_url()
            LOG.warning(
                "tado needs you to authorise this script. Open this URL in a browser "
                "and log in with your tado account:\n\n    %s\n",
                url,
            )
            self._notify("tado Auto-Assist needs to be authorised. Open the URL shown in its log.")
            tado.device_activation()
        status = tado.device_activation_status()
        if status != "COMPLETED":
            raise TadoException(f"Login not completed, status: {status}")
        self.tado = tado
        self.requests_made += 2  # PyTado reads /me and the home on login
        LOG.info("Logged in to tado.")

    # ------------------------------- checks ------------------------------- #

    def run_cycle(self) -> None:
        """One check of everything that is enabled."""
        zone_states = None
        if self.config.enable_open_window or self.config.enable_temp_limit:
            zone_states = self._call(self.tado.get_zone_states)
            self._check_zones(zone_states)
        if self.config.enable_geofencing:
            self._check_geofencing(zone_states)

    def _check_zones(self, zone_states: Mapping[str, Any]) -> None:
        for zone_key in sorted(zone_states, key=int):
            zone_id = int(zone_key)
            state = zone_states[zone_key]
            if self.config.enable_open_window and self._check_open_window(zone_id, state):
                continue  # the heating of this zone is now off
            if self.config.enable_temp_limit:
                self._check_temp_limit(zone_id, state)

    def _check_open_window(self, zone_id: int, state: Any) -> bool:
        if not _window_detected(state) or state.open_window is not None:
            return False
        name = self._zone_name(zone_id)
        if self.config.dry_run:
            LOG.info("[DRY-RUN] %s: open window detected, would activate open window mode.", name)
            return True
        LOG.info("%s: open window detected, activating open window mode.", name)
        self._change(
            f"{name}: open window detected, heating paused.",
            self.tado.set_open_window,
            zone_id,
        )
        return True

    def _check_temp_limit(self, zone_id: int, state: Any) -> None:
        setting = state.setting
        if (
            setting.type != ZoneType.HEATING
            or setting.power != Power.ON
            or setting.temperature is None
        ):
            return
        current = float(setting.temperature.celsius)
        if current > self.config.max_temp:
            target, word = self.config.max_temp, "higher than the maximum"
        elif current < self.config.min_temp:
            target, word = self.config.min_temp, "lower than the minimum"
        else:
            return
        name = self._zone_name(zone_id)
        if self.config.dry_run:
            LOG.info(
                "[DRY-RUN] %s: set temperature %.1f is %s, would set it to %.1f.",
                name,
                current,
                word,
                target,
            )
            return
        LOG.info("%s: set temperature %.1f is %s, setting it to %.1f.", name, current, word, target)
        self._change(
            f"{name}: temperature {current:.1f} changed to {target:.1f} (limit).",
            self.tado.set_zone_overlay,
            zone_id,
            self.config.temp_limit_overlay_mode,
            target,
            device_type=ZoneType.HEATING,  # given so PyTado does not ask tado for it
        )

    @staticmethod
    def _presence_from_zones(zone_states: Mapping[str, Any] | None) -> str | None:
        """HOME or AWAY if every zone agrees, else None."""
        if not zone_states:
            return None
        modes = {str(state.tado_mode.value) for state in zone_states.values()}
        if len(modes) == 1 and modes <= {"HOME", "AWAY"}:
            return modes.pop()
        return None

    def _check_geofencing(self, zone_states: Mapping[str, Any] | None) -> None:
        devices = self._call(self.tado.get_mobile_devices)
        tracked = [
            device
            for device in devices
            if device.settings.geo_tracking_enabled and device.location is not None
        ]
        if not tracked:
            # Without any location we cannot know that the home is empty:
            # leave the mode as it is instead of switching the heating to AWAY.
            if not self._geofencing_paused:
                LOG.warning(
                    "No mobile device is sharing its location (geofencing off or "
                    "signed out of the tado app). Home/away is NOT managed until one does."
                )
                self._geofencing_paused = True
            return
        if self._geofencing_paused:
            LOG.info("A mobile device is sharing its location again, geofencing resumed.")
            self._geofencing_paused = False

        at_home = [device.name for device in tracked if device.location.at_home]
        wanted = "HOME" if at_home else "AWAY"

        presence = self._presence_from_zones(zone_states)
        confirmed = presence is None
        if presence is None:
            presence = str(self._call(self.tado.get_home_state).presence.value)
        if presence == wanted:
            return
        if not confirmed and not self.config.dry_run:
            # The zones gave the mode for free; ask the home itself before changing it.
            presence = str(self._call(self.tado.get_home_state).presence.value)
            if presence == wanted:
                return

        if wanted == "HOME":
            reason = f"{_join_names(at_home)} at home"
            public_reason = reason if self.config.notify_device_names else "someone is at home"
            action = self.tado.set_home
        else:
            reason = public_reason = "nobody at home"
            action = self.tado.set_away

        if self.config.dry_run:
            LOG.info(
                "[DRY-RUN] Home is in %s mode but %s, would activate %s mode.",
                presence,
                reason,
                wanted,
            )
            return
        LOG.info("Home is in %s mode but %s, activating %s mode.", presence, reason, wanted)
        self._change(f"{wanted} mode activated: {public_reason}.", action)

    # ------------------------------ main loop ----------------------------- #

    def _log_status(self, next_check: float) -> None:
        remaining = self._rate_value("remaining")
        quota = (
            f"{remaining}/{self.daily_limit()} requests left today"
            if remaining is not None
            else f"about {self.requests_made} requests used since start"
        )
        LOG.info("Check done, %s. Next check in %d min.", quota, round(next_check / 60))

    def _handle_rate_limit(self) -> float:
        wait = self._rate_value("reset-seconds") or RATE_LIMIT_FALLBACK_WAIT
        wait += 60  # small margin after the reset
        LOG.warning(
            "tado's daily request quota is used up. Nothing is managed for %d min.",
            round(wait / 60),
        )
        self._notify(
            f"tado Auto-Assist: daily API quota used up, paused for {round(wait / 60)} min."
        )
        return wait

    def run(self) -> None:
        """Runs until stop() is called (SIGTERM / Ctrl+C)."""
        retry = self.config.error_retry_interval
        failures = 0
        self._describe()

        while not self._stop.is_set():
            pause = seconds_until_active(self.config, self._now())
            if pause:
                LOG.info("Outside the active hours, sleeping %d min.", round(pause / 60))
                self._sleep(pause)
                continue
            try:
                if self.tado is None:
                    self.login()
                self.run_cycle()
            except TadoRateLimitException:
                self._sleep(self._handle_rate_limit())
                continue
            except TadoCredentialsException as exc:
                LOG.error("tado rejected the saved login (%s). A new authorisation is needed.", exc)
                self.tado = None
                self._sleep(retry)
                continue
            except Exception as exc:  # noqa: BLE001 - last line of defence of the daemon
                # A crash would make Docker/systemd restart the script in a loop,
                # and every start costs requests of the daily quota.
                failures += 1
                expected = isinstance(
                    exc, (TadoException, requests.RequestException, ValidationError, OSError)
                )
                LOG.log(
                    logging.WARNING if expected else logging.ERROR,
                    "Check failed (%s: %s). Retrying in %d s.",
                    type(exc).__name__,
                    exc,
                    retry,
                    exc_info=not expected,
                )
                if failures == 5:
                    self._notify("tado Auto-Assist: 5 failed checks in a row, see its log.")
                self._sleep(retry)
                retry = min(retry * 2, MAX_RETRY_INTERVAL)
                continue

            if failures:
                LOG.info("Working again after %d failed check(s).", failures)
            failures = 0
            retry = self.config.error_retry_interval
            next_check = self.interval()
            self._log_status(next_check)
            self._sleep(next_check)

        LOG.info("Stopped.")

    def _describe(self) -> None:
        config = self.config
        features = [
            name
            for name, enabled in (
                ("geofencing", config.enable_geofencing),
                ("open window", config.enable_open_window),
                (
                    f"temperature limit {config.min_temp:g}-{config.max_temp:g}",
                    config.enable_temp_limit,
                ),
            )
            if enabled
        ]
        LOG.info(
            "tado_aa %s. Enabled: %s.%s",
            __version__,
            ", ".join(features),
            " DRY-RUN: nothing will be changed." if config.dry_run else "",
        )
        LOG.info(
            "%d request(s) per check, %d h active per day, one check every %d min "
            "with a quota of %d requests/day.",
            requests_per_cycle(config),
            round(active_minutes_per_day(config) / 60),
            round(compute_interval(config, config.daily_limit) / 60),
            config.daily_limit,
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def healthcheck(config: Config) -> int:
    """Exit code 0 if the main loop wrote its heartbeat recently."""
    try:
        age = time.time() - config.heartbeat_file.stat().st_mtime
    except OSError:
        return 1
    return 0 if age < HEARTBEAT_MAX_AGE else 1


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    if level != "DEBUG":
        # This PyTado logger has its own handler and reports every field of a
        # reply that its models do not know, such as openWindowDetected.
        _pytado_model_util.LOGGER.setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="exit 0 if a running tado_aa is alive (used by Docker)",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.healthcheck:
        return healthcheck(config)

    _setup_logging(config.log_level)

    # The refresh token gives full access to the tado account: keep the files
    # this process creates private to its user.
    os.umask(0o077)
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Cannot create the data directory {config.data_dir}: {exc}", file=sys.stderr)
        return 2
    try:
        config.data_dir.chmod(0o700)
    except OSError:
        LOG.warning("Could not make %s private (chmod 700); check its owner.", config.data_dir)
    if not os.access(config.data_dir, os.W_OK):
        print(
            f"The data directory {config.data_dir} is not writable by this user "
            f"(uid {os.getuid() if hasattr(os, 'getuid') else '?'}).",
            file=sys.stderr,
        )
        return 2

    app = TadoAutoAssist(config)
    signal.signal(signal.SIGTERM, app.stop)
    try:
        app.run()
    except KeyboardInterrupt:
        LOG.info("Interrupted by user.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
