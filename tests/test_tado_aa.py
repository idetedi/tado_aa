"""Tests of tado_aa against the real python-tado models with a fake HTTP layer.

No test talks to tado: FakeHttp answers with JSON shaped like the V3 API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
import requests
from PyTado.exceptions import TadoException, TadoRateLimitException
from PyTado.http import DeviceActivationStatus
from PyTado.interface import Tado
from PyTado.interface import api as pytado_api

import tado_aa
from tado_aa import Config, ConfigError, Notifier, TadoAutoAssist


def zone_state(
    mode: str = "HOME",
    celsius: float | None = 21.0,
    power: str = "ON",
    window_detected: bool = False,
    window_active: bool = False,
    zone_type: str = "HEATING",
) -> dict[str, Any]:
    setting: dict[str, Any] = {"type": zone_type, "power": power}
    if celsius is not None:
        setting["temperature"] = {"celsius": celsius, "fahrenheit": celsius * 9 / 5 + 32}
    state: dict[str, Any] = {
        "tadoMode": mode,
        "geolocationOverride": False,
        "setting": setting,
        "nextTimeBlock": {"start": "2026-11-10T21:00:00.000Z"},
        "link": {"state": "ONLINE"},
        "activityDataPoints": {
            "heatingPower": {
                "type": "PERCENTAGE",
                "percentage": 0.0,
                "timestamp": "2026-11-10T10:00:00.000Z",
            }
        },
        "sensorDataPoints": {},
    }
    if window_detected:
        state["openWindowDetected"] = True
    if window_active:
        state["openWindow"] = {
            "detectedTime": "2026-11-10T10:00:00Z",
            "durationInSeconds": 900,
            "expiry": "2026-11-10T10:15:00Z",
            "remainingTimeInSeconds": 800,
        }
    return state


def mobile(name: str, at_home: bool | None, tracking: bool = True) -> dict[str, Any]:
    device: dict[str, Any] = {
        "name": name,
        "id": abs(hash(name)) % 10000,
        "deviceMetadata": {
            "platform": "Android",
            "osVersion": "15",
            "model": "Phone",
            "locale": "es",
        },
        "settings": {
            "geoTrackingEnabled": tracking,
            "specialOffersEnabled": False,
            "onDemandLogRetrievalEnabled": False,
            "pushNotifications": {"lowBatteryReminder": True},
        },
    }
    if at_home is not None:
        device["location"] = {
            "stale": False,
            "atHome": at_home,
            "bearingFromHome": {"degrees": 0.0, "radians": 0.0},
            "relativeDistanceFromHomeFence": 0.0,
        }
    return device


class FakeHttp:
    """Stands in for PyTado.http.Http: records requests, returns canned JSON."""

    device_activation_status = DeviceActivationStatus.COMPLETED

    def __init__(self) -> None:
        self.zones: dict[str, dict[str, Any]] = {"1": zone_state()}
        self.mobiles: list[dict[str, Any]] = [mobile("Phone A", True)]
        self.presence = "HOME"
        self.rate_limit: dict[str, str] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.fail_with: Exception | None = None
        self.rejected: set[str] = set()

    @property
    def commands(self) -> list[str]:
        return [command for command, _, _ in self.requests]

    def request(self, request: Any) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        command = request.command or ""
        self.requests.append((command, str(request.action.value), request.payload))
        if command == "zoneStates":
            return {"zoneStates": self.zones}
        if command == "mobileDevices":
            return self.mobiles
        if command == "state":
            return {"presence": self.presence, "presenceLocked": True}
        if command == "presenceLock":
            self.presence = request.payload["homePresence"]
            return {"success": True}
        if command in self.rejected:
            raise TadoException("Request failed with status code 422")
        if command.endswith("/state/openWindow/activate") or command.endswith("/overlay"):
            return {"success": True}
        raise AssertionError(f"Unexpected request to tado: {command}")


class FakeNotifier:
    enabled = True

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


def make_app(http: FakeHttp, **settings: Any) -> tuple[TadoAutoAssist, FakeNotifier]:
    tado = Tado.__new__(Tado)
    tado._http = http  # type: ignore[assignment]
    tado._api = pytado_api.Tado.from_http(http=http)  # type: ignore[arg-type]
    tado._debug = False
    notifier = FakeNotifier()
    app = TadoAutoAssist(Config(**settings), tado=tado, notifier=notifier)  # type: ignore[arg-type]
    return app, notifier


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


# ------------------------------ request budget ------------------------------ #


def test_quiet_cycle_costs_two_requests(http: FakeHttp) -> None:
    app, notifier = make_app(http)
    app.run_cycle()
    assert http.commands == ["zoneStates", "mobileDevices"]
    assert app.requests_made == 2
    assert notifier.messages == []


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, 2),
        ({"enable_geofencing": False}, 1),
        ({"enable_open_window": False, "enable_temp_limit": False}, 2),
        ({"enable_open_window": False}, 2),
    ],
)
def test_requests_per_cycle_matches_real_cycle(
    http: FakeHttp, settings: dict[str, Any], expected: int
) -> None:
    app, _ = make_app(http, **settings)
    app.run_cycle()
    assert tado_aa.requests_per_cycle(app.config) == expected == len(http.requests)


def test_interval_fits_the_daily_quota() -> None:
    config = Config()
    interval = tado_aa.compute_interval(config, 100)
    assert interval == 1920  # 90 requests / 2 per check = 45 checks in 24 h
    checks_per_day = 86400 / interval
    assert checks_per_day * tado_aa.requests_per_cycle(config) <= 100 - config.request_reserve


def test_interval_uses_active_hours_and_reported_quota(http: FakeHttp) -> None:
    config = Config(active_start=6 * 60, active_end=24 * 60)
    assert tado_aa.compute_interval(config, 100) == 1440  # 18 h / 45 checks = 24 min
    app, _ = make_app(http)
    http.rate_limit = {"per-day": "20000", "remaining": "19990"}
    assert app.daily_limit() == 20000
    assert app.interval() == app.config.min_interval


def test_fixed_check_interval_is_respected() -> None:
    assert tado_aa.compute_interval(Config(check_interval=900), 100) == 900


# -------------------------------- geofencing -------------------------------- #


def test_everyone_leaves_sets_away(http: FakeHttp) -> None:
    http.mobiles = [mobile("Phone A", False), mobile("Phone B", False)]
    app, notifier = make_app(http)
    app.run_cycle()
    assert http.commands == ["zoneStates", "mobileDevices", "state", "presenceLock"]
    assert http.requests[-1][2] == {"homePresence": "AWAY"}
    assert notifier.messages == ["AWAY mode activated: nobody at home."]


def test_someone_arrives_sets_home_without_leaking_names(http: FakeHttp) -> None:
    http.zones = {"1": zone_state(mode="AWAY"), "2": zone_state(mode="AWAY")}
    http.presence = "AWAY"
    http.mobiles = [mobile("Phone A", False), mobile("Phone B", True)]
    app, notifier = make_app(http)
    app.run_cycle()
    assert http.requests[-1] == ("presenceLock", "PUT", {"homePresence": "HOME"})
    assert notifier.messages == ["HOME mode activated: someone is at home."]


def test_device_names_in_notification_only_when_enabled(http: FakeHttp) -> None:
    http.zones = {"1": zone_state(mode="AWAY")}
    http.presence = "AWAY"
    app, notifier = make_app(http, notify_device_names=True)
    app.run_cycle()
    assert notifier.messages == ["HOME mode activated: Phone A at home."]


def test_no_change_when_home_already_reports_the_wanted_mode(http: FakeHttp) -> None:
    """Zones say AWAY but the home itself is already HOME: do not write."""
    http.zones = {"1": zone_state(mode="AWAY")}
    http.presence = "HOME"
    app, _ = make_app(http)
    app.run_cycle()
    assert http.commands == ["zoneStates", "mobileDevices", "state"]


def test_no_location_at_all_never_switches_to_away(http: FakeHttp) -> None:
    http.mobiles = [mobile("Phone A", None), mobile("Phone B", False, tracking=False)]
    app, _ = make_app(http)
    app.run_cycle()
    app.run_cycle()
    assert "presenceLock" not in http.commands
    assert "state" not in http.commands


def test_geofencing_only_reads_home_state(http: FakeHttp) -> None:
    http.mobiles = [mobile("Phone A", False)]
    app, _ = make_app(http, enable_open_window=False, enable_temp_limit=False)
    app.run_cycle()
    assert http.commands == ["mobileDevices", "state", "presenceLock"]


# ------------------------- open window / temperature ------------------------ #


def test_open_window_is_activated_once_per_zone(http: FakeHttp) -> None:
    http.zones = {
        "1": zone_state(window_detected=True, celsius=30.0),
        "2": zone_state(window_detected=True, window_active=True, power="OFF", celsius=None),
        "3": zone_state(),
    }
    app, notifier = make_app(http, zone_names={1: "Salón"})
    app.run_cycle()
    writes = [request for request in http.requests if request[1] != "GET"]
    assert writes == [("zones/1/state/openWindow/activate", "POST", None)]
    assert notifier.messages == ["Salón: open window detected, heating paused."]


def test_temperature_limit_high_and_low(http: FakeHttp) -> None:
    http.zones = {
        "1": zone_state(celsius=28.0),
        "2": zone_state(celsius=3.0),
        "3": zone_state(celsius=21.0),
        "4": zone_state(celsius=30.0, power="OFF"),
        "5": zone_state(celsius=60.0, zone_type="HOT_WATER"),
    }
    app, _ = make_app(http)
    app.run_cycle()
    overlays = {command: payload for command, _, payload in http.requests if "overlay" in command}
    assert set(overlays) == {"zones/1/overlay", "zones/2/overlay"}
    assert overlays["zones/1/overlay"] == {
        "setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": 25.0}},
        "termination": {"typeSkillBasedApp": "NEXT_TIME_BLOCK"},
    }
    assert overlays["zones/2/overlay"]["setting"]["temperature"] == {"celsius": 5.0}
    # The zone type is passed in, so PyTado must not spend a request on "zones".
    assert "zones" not in http.commands


def test_rejected_change_does_not_stop_the_rest_of_the_check(http: FakeHttp) -> None:
    http.zones = {"1": zone_state(celsius=30.0), "2": zone_state(window_detected=True)}
    http.mobiles = [mobile("Phone A", False)]
    http.rejected = {"zones/1/overlay"}
    app, notifier = make_app(http)
    app.run_cycle()
    assert http.commands == [
        "zoneStates",
        "zones/1/overlay",
        "zones/2/state/openWindow/activate",
        "mobileDevices",
        "state",
        "presenceLock",
    ]
    assert len(notifier.messages) == 2  # nothing is announced for the failed change


def test_disabled_features_do_nothing(http: FakeHttp) -> None:
    http.zones = {"1": zone_state(window_detected=True, celsius=30.0)}
    http.mobiles = [mobile("Phone A", False)]
    app, _ = make_app(
        http, enable_open_window=False, enable_temp_limit=False, enable_geofencing=True
    )
    app.run_cycle()
    assert not any("zones/" in command for command in http.commands)

    http.requests.clear()
    app, _ = make_app(http, enable_geofencing=False)
    app.run_cycle()
    assert http.commands == ["zoneStates", "zones/1/state/openWindow/activate"]


def test_dry_run_never_writes(http: FakeHttp, caplog: pytest.LogCaptureFixture) -> None:
    http.zones = {"1": zone_state(window_detected=True), "2": zone_state(celsius=30.0)}
    http.mobiles = [mobile("Phone A", False)]
    app, notifier = make_app(http, dry_run=True)
    with caplog.at_level("INFO", logger="tado_aa"):
        app.run_cycle()
    assert http.commands == ["zoneStates", "mobileDevices"]
    assert notifier.messages == []
    assert caplog.text.count("[DRY-RUN]") == 3


# ------------------------------- active hours ------------------------------- #


@pytest.mark.parametrize(
    ("start", "end", "clock", "active", "wait"),
    [
        (0, 1440, "03:00", True, 0),
        (360, 1440, "05:30", False, 1800),
        (360, 1440, "06:00", True, 0),
        (360, 1440, "23:59", True, 0),
        (1320, 360, "23:00", True, 0),  # 22:00-06:00 crosses midnight
        (1320, 360, "05:59", True, 0),
        (1320, 360, "12:00", False, 36000),
    ],
)
def test_active_hours(start: int, end: int, clock: str, active: bool, wait: int) -> None:
    config = Config(active_start=start, active_end=end)
    moment = datetime.fromisoformat(f"2026-11-10T{clock}:00")
    assert tado_aa.in_active_hours(config, moment) is active
    assert tado_aa.seconds_until_active(config, moment) == wait


# ------------------------------- configuration ------------------------------ #


def test_config_from_env() -> None:
    config = Config.from_env(
        {
            "ENABLE_OPEN_WINDOW": "false",
            "MAX_TEMP": "23.5",
            "ACTIVE_HOURS": "06:30-24:00",
            "TZ": "Europe/Madrid",
            "DRY_RUN": "yes",
            "ZONE_NAMES": "1=Salón, 2=Dormitorio",
            "TEMP_LIMIT_OVERLAY_MODE": "manual",
        }
    )
    assert config.enable_open_window is False and config.enable_geofencing is True
    assert config.max_temp == 23.5
    assert (config.active_start, config.active_end) == (390, 1440)
    assert config.dry_run is True
    assert config.zone_names == {1: "Salón", 2: "Dormitorio"}
    assert config.temp_limit_overlay_mode.value == "MANUAL"
    assert str(config.timezone) == "Europe/Madrid"


@pytest.mark.parametrize(
    "env",
    [
        {"MIN_TEMP": "25", "MAX_TEMP": "20"},
        {"ENABLE_GEOFENCING": "maybe"},
        {"ACTIVE_HOURS": "6-24"},
        {"ACTIVE_HOURS": "08:00-08:00"},
        {"TZ": "Mars/Olympus"},
        {"TELEGRAM_BOT_TOKEN": "123:abc"},
        {"NTFY_URL": "ntfy.sh/topic"},
        {"ZONE_NAMES": "Salón"},
        {"TADO_DAILY_LIMIT": "0"},
        {"ENABLE_GEOFENCING": "0", "ENABLE_OPEN_WINDOW": "0", "ENABLE_TEMP_LIMIT": "0"},
    ],
)
def test_invalid_config_is_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_secrets_are_not_in_config_repr() -> None:
    config = Config(telegram_bot_token="123:SECRET", telegram_chat_id="1", ntfy_token="TOPSECRET")
    assert "SECRET" not in repr(config)


# ----------------------------- main loop / errors --------------------------- #


def run_loop(app: TadoAutoAssist, cycles: int) -> list[float]:
    """Runs app.run() with a fake sleep that stops after `cycles` sleeps."""
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= cycles:
            app.stop()

    app._sleep = fake_sleep  # type: ignore[method-assign]
    app.run()
    return sleeps


def test_rate_limit_sleeps_until_the_quota_resets(http: FakeHttp) -> None:
    app, notifier = make_app(http)
    http.fail_with = TadoRateLimitException("Request failed with status code 429")
    http.rate_limit = {"per-day": "100", "remaining": "0", "reset-seconds": "1301"}
    sleeps = run_loop(app, cycles=1)
    assert sleeps == [1361]
    assert len(notifier.messages) == 1 and "quota" in notifier.messages[0]


def test_errors_back_off_and_recover(http: FakeHttp) -> None:
    app, _ = make_app(http)
    http.fail_with = requests.ConnectionError("boom")
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            http.fail_with = None
        if len(sleeps) == 4:
            app.stop()

    app._sleep = fake_sleep  # type: ignore[method-assign]
    app.run()
    assert sleeps == [60, 120, 240, 1920]


def test_unexpected_bug_does_not_crash_the_loop(http: FakeHttp) -> None:
    app, _ = make_app(http)
    http.fail_with = RuntimeError("bug")
    assert run_loop(app, cycles=2) == [60, 120]


def test_outside_active_hours_makes_no_request(http: FakeHttp) -> None:
    app, _ = make_app(http, active_start=360, active_end=1440)
    app._now = lambda: datetime.fromisoformat("2026-11-10T02:00:00")  # type: ignore[method-assign]
    assert run_loop(app, cycles=1) == [4 * 3600]
    assert http.requests == []


# -------------------------------- notifications ----------------------------- #


class RecordingSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error

        class Response:
            status_code = 200

        return Response()


def test_notifier_posts_to_ntfy_and_telegram() -> None:
    session = RecordingSession()
    config = Config(
        ntfy_url="https://ntfy.example/tado",
        ntfy_token="tk",
        telegram_bot_token="123:abc",
        telegram_chat_id="42",
    )
    Notifier(config, session=session).send("hola")  # type: ignore[arg-type]
    (ntfy_url, ntfy), (telegram_url, telegram) = session.calls
    assert ntfy_url == "https://ntfy.example/tado"
    assert ntfy["data"] == b"hola" and ntfy["headers"]["Authorization"] == "Bearer tk"
    assert telegram_url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert telegram["json"] == {"chat_id": "42", "text": "hola"}
    assert ntfy["timeout"] == telegram["timeout"] == tado_aa.NOTIFY_TIMEOUT


def test_notifier_failure_is_swallowed_and_hides_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = requests.ConnectionError("https://api.telegram.org/bot123:SECRET/sendMessage down")
    config = Config(telegram_bot_token="123:SECRET", telegram_chat_id="42")
    with caplog.at_level("WARNING", logger="tado_aa"):
        Notifier(config, session=RecordingSession(error)).send("hola")  # type: ignore[arg-type]
    assert "Telegram failed" in caplog.text
    assert "SECRET" not in caplog.text


def test_notifier_disabled_by_default() -> None:
    assert Notifier(Config()).enabled is False


# --------------------------------- healthcheck ------------------------------ #


def test_healthcheck(tmp_path: Any) -> None:
    config = Config(data_dir=tmp_path)
    assert tado_aa.healthcheck(config) == 1
    config.heartbeat_file.touch()
    assert tado_aa.healthcheck(config) == 0
