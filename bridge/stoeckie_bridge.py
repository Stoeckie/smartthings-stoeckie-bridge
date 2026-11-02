"""Stoeckie Bridge: brings cloud appliances onto the LAN for a SmartThings Edge driver.

Sources
  * AEG / Electrolux appliances (dryer, washer-dryer) via the official Electrolux Group Developer API
    (developer.electrolux.one): state at start, live updates over SSE, poll as fallback.
  * Fujitsu air conditioners with an FGLair wifi adapter, via the Ayla cloud that the FGLair app uses
    (library ayla-iot-unofficial, the same one Home Assistant uses). Polled every 60 s.
  * Škoda cars via the official MyŠkoda public API (API key from the MyŠkoda app): battery, range, charging,
    plug, doors, climate; start/stop charging and climate. Polled every 10 minutes (the API allows 20 calls/hour).
  * iRobot account (optional): favorite names and rooms for the "iRobot Roomba (local)" Edge driver
    (library roombapy-prime). Only read, twice a day; the robot itself is controlled locally by the hub.
  * Rain radar (optional): rain now and the next 2 hours for an address with a radius, every 5 minutes. Buienradar in
    the Netherlands and Belgium (5 minute steps), Open-Meteo elsewhere (15 minute steps). Addresses: PDOK (NL
    postcode) or OpenStreetMap Nominatim (anywhere).
  * VELUX ACTIVE (optional): departure mode lock/unlock via the VELUX/Netatmo cloud. Unlock is signed with a key
    the gateway hands out locally once (button press). The windows stay local (HomeKit driver on the hub).

Credentials live only in config.json next to this script (never in the code).

LAN API (default port 8787, no auth, LAN only)
  GET  /stbridge/info                    -> {"service": "stoeckie-bridge", "id": ..., "version": ...}
  GET  /stbridge/devices                 -> {"devices": [ {id, type, name, online, state{...}} ]}
  POST /stbridge/devices/<id>/command    body {"cmd": "...", "value": ...}
      dryer:   run | pause | resume | stop | refresh
      aircon:  on | off | mode (auto cool dry fan heat) | setpoint (C) | fan (quiet low medium high auto)
               | swing (off vertical horizontal both) | refresh | settings {poll_seconds}
      car:     on | off (charging) | climate_on | climate_off | limit (percent) | refresh
      velux_lock: lock | unlock | refresh | settings {poll_seconds}
      rain:    refresh (VELUX gateway rain sensor)
      rainradar: refresh | settings {postcode, housenumber, address, location, radius_m, threshold, provider}
               (state: raining, rainRate mm/h, minutesUntilRain, minutesOfRain, intensity, summary)
  POST /stbridge/buienradar               body {"enabled": true} (from the bridge device)
  GET  /stbridge/irobot/<blid>[?refresh=1] -> {"name", "favorites": [{name, id, command, params}], "updated"}

Usage
  python stoeckie_bridge.py           run the bridge
  python stoeckie_bridge.py --check   test the AEG and FGLair logins and list appliances
  python stoeckie_bridge.py --demo    fake devices, to test the Edge driver
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import html
import ipaddress
import json
import os
import logging
import logging.handlers
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

VERSION = "2026-11-02a"
HERE = Path(__file__).resolve().parent
# Data folder: next to the script on Windows, /data in the Docker image (STBRIDGE_DATA)
DATA = Path(os.environ.get("STBRIDGE_DATA") or HERE)
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA / "config.json"
LOG_FILE = DATA / "bridge.log"
# Extra networks allowed to open the setup page, e.g. "192.168.2.0/24" (Docker without host networking)
SETUP_ALLOW = [ipaddress.ip_network(n.strip(), strict=False)
               for n in os.environ.get("STBRIDGE_SETUP_ALLOW", "").split(",") if n.strip()]

log = logging.getLogger("bridge")

# ---------------------------------------------------------------- registry

class Registry:
    """All devices the bridge exposes, keyed by a stable id."""

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self.handlers: dict[str, Any] = {}  # id -> async fn(cmd, value)

    def upsert(self, dev_id: str, dev_type: str, name: str, state: dict[str, Any],
               online: bool = True, handler=None) -> None:
        d = self.devices.setdefault(dev_id, {"id": dev_id, "type": dev_type, "name": name})
        d["type"], d["name"], d["online"] = dev_type, name, online
        d.setdefault("state", {}).update(state)
        d["updated"] = int(time.time())
        if handler:
            self.handlers[dev_id] = handler

    def set_online(self, prefix: str, online: bool) -> None:
        for d in self.devices.values():
            if d["id"].startswith(prefix):
                d["online"] = online


REG = Registry()


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: dict[str, Any]) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


# ---------------------------------------------------------------- Electrolux / AEG

DRYER_TYPES = {"TD", "WD"}  # tumble dryer, washer-dryer


def aeg_configured(e: dict[str, Any]) -> bool:
    keys = ("api_key", "access_token", "refresh_token")
    return all(e.get(k) and not str(e.get(k)).startswith("PLAK_HIER") for k in keys)


def _iso_in(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    # whole minutes: the end time then stays the same between polls
    t = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(seconds) + 30)).replace(second=0, microsecond=0)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def dryer_state(reported: dict[str, Any], connection: str | None) -> dict[str, Any]:
    """Flatten an Electrolux TD/WD reported state into what the Edge driver needs."""
    app_state = str(reported.get("applianceState") or "OFF").upper()
    phase = str(reported.get("cyclePhase") or "").upper()
    sel = reported.get("userSelections") or {}
    tte = reported.get("timeToEnd")
    try:
        tte = float(tte) if tte is not None else None
    except (TypeError, ValueError):
        tte = None
    running = app_state in ("RUNNING", "DELAYED_START")
    return {
        "applianceState": app_state,       # OFF IDLE RUNNING PAUSED READY_TO_START DELAYED_START END_OF_CYCLE
        "cyclePhase": phase,
        "program": sel.get("programUID"),
        "timeToEnd": tte,                  # seconds
        "completionTime": _iso_in(tte) if running else None,
        "doorState": reported.get("doorState"),
        "remoteControl": reported.get("remoteControl"),
        "alerts": reported.get("alerts") or [],
        "connection": connection,
    }


class Electrolux:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.client = None
        self.appliances: dict[str, dict[str, Any]] = {}  # id -> {type, name, reported, connection}

    def _on_tokens(self, access: str, refresh: str, api_key: str) -> None:
        e = self.cfg.setdefault("electrolux", {})
        if e.get("refresh_token") != refresh or e.get("access_token") != access:
            e["access_token"], e["refresh_token"], e["api_key"] = access, refresh, api_key
            save_config(self.cfg)
            log.info("[aeg] tokens refreshed and saved")

    def _publish(self, app_id: str) -> None:
        a = self.appliances[app_id]
        online = str(a.get("connection") or "").lower() != "disconnected"
        REG.upsert("aeg-" + app_id, "dryer", a["name"],
                   dryer_state(a["reported"], a.get("connection")), online, self._command(app_id))

    def _command(self, app_id: str):
        async def handler(cmd: str, value: Any) -> None:
            if cmd == "refresh":
                await self._load_state(app_id)
                return
            mapping = {"run": "START", "pause": "PAUSE", "resume": "RESUME", "stop": "STOPRESET"}
            if cmd not in mapping:
                raise ValueError(f"unsupported command {cmd}")
            await self.client.send_command(app_id, {"executeCommand": mapping[cmd]})
        return handler

    async def _load_state(self, app_id: str) -> None:
        st = await self.client.get_appliance_state(app_id)
        a = self.appliances[app_id]
        a["reported"] = (st.properties or {}).get("reported", {}) or {}
        a["connection"] = st.connectionState
        self._publish(app_id)

    def _on_event(self, event: dict[str, Any]) -> None:
        app_id = event.get("applianceId")
        if app_id not in self.appliances:
            return
        prop, value = event.get("property"), event.get("value")
        if prop is None:
            return
        a = self.appliances[app_id]
        if prop in ("connectionState", "connectivityState"):
            a["connection"] = value
        if prop != "connectionState":
            target = a["reported"]
            path = prop.split("/")
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = value
        log.info("[aeg] %s %s = %s", a["name"], prop, value)
        self._publish(app_id)

    async def run(self) -> None:
        e = self.cfg.get("electrolux") or {}
        if not aeg_configured(e):
            log.info("[aeg] not configured: open http://localhost:%s/setup on this pc", self.cfg.get("port", 8787))
            return
        from electrolux_group_developer_sdk.auth.token_manager import TokenManager
        from electrolux_group_developer_sdk.client.appliance_client import ApplianceClient

        tm = TokenManager(e["access_token"], e["refresh_token"], e["api_key"], on_token_update=self._on_tokens)
        self.client = ApplianceClient(tm, external_user_agent=f"StoeckieBridge/{VERSION}")
        while True:
            try:
                apps = await self.client.get_appliances()
                break
            except Exception as ex:  # noqa: BLE001
                log.error("[aeg] cannot list appliances: %s (retry in 60 s)", ex)
                await asyncio.sleep(60)
        for app in apps:
            t = str(app.applianceType).upper()
            log.info("[aeg] appliance %s '%s' type %s", app.applianceId, app.applianceName, t)
            if t not in DRYER_TYPES:
                continue
            self.appliances[app.applianceId] = {"type": t, "name": app.applianceName or "Droger",
                                                "reported": {}, "connection": None}
            self.client.add_listener(app.applianceId, self._on_event)
            try:
                await self._load_state(app.applianceId)
            except Exception as ex:  # noqa: BLE001
                log.error("[aeg] state of %s failed: %s", app.applianceId, ex)
        if not self.appliances:
            log.warning("[aeg] no dryer found on this account")
            return

        async def resync() -> None:
            for app_id in self.appliances:
                try:
                    await self._load_state(app_id)
                except Exception as ex:  # noqa: BLE001
                    log.warning("[aeg] resync %s failed: %s", app_id, ex)

        async def poll() -> None:  # fallback when the stream is quiet or broken
            while True:
                await asyncio.sleep(int(e.get("poll_seconds", 300)))
                await resync()

        asyncio.create_task(poll())
        await self.client.start_event_stream(do_on_livestream_opening_list=[resync])


# ---------------------------------------------------------------- Fujitsu FGLair (Ayla cloud)

def fglair_configured(f: dict[str, Any]) -> bool:
    return bool(f.get("username") and f.get("password") and not str(f.get("password")).startswith("PLAK_HIER"))


def _name(e) -> str:
    return getattr(e, "name", str(e)).lower()


SWING_NAMES = {"OFF": "off", "SWING_VERTICAL": "vertical", "SWING_HORIZONTAL": "horizontal", "SWING_BOTH": "both"}


def aircon_state(dev) -> dict[str, Any]:
    """Flatten a FujitsuHVAC object into what the Edge driver needs."""
    def safe(fn, default=None):
        try:
            return fn()
        except Exception:  # noqa: BLE001  (missing property on this model)
            return default
    mode = safe(lambda: _name(dev.op_mode), "off")
    lo, hi = safe(lambda: dev.temperature_range, (16.0, 30.0)) if mode not in ("off", "on") else (16.0, 30.0)
    return {
        "power": "off" if mode == "off" else "on",
        "mode": mode,                                   # off on auto cool dry fan heat
        "setpoint": safe(lambda: dev.set_temp),
        "temperature": safe(lambda: dev.sensed_temp),   # None on models without a room sensor
        "outdoorTemperature": safe(lambda: dev.outdoor_temperature),
        "fan": safe(lambda: _name(dev.fan_speed)),
        "swing": safe(lambda: SWING_NAMES.get(dev.swing_mode.name, "off"), "off"),
        "supportedModes": [m for m in (_name(x) for x in safe(lambda: dev.supported_op_modes, []))
                           if m not in ("off", "on")],
        "supportedFans": [_name(x) for x in safe(lambda: dev.supported_fan_speeds, [])],
        "supportedSwings": [SWING_NAMES.get(x.name, "off") for x in safe(lambda: dev.supported_swing_modes, [])],
        "setpointRange": [lo, hi],
        "model": getattr(dev, "oem_model_number", None),
    }


class FGLair:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.f = cfg.get("fglair") or {}
        self.api = None
        self.devices: dict[str, Any] = {}  # dsn -> FujitsuHVAC
        self.skipped: set[str] = set()
        self.lock = asyncio.Lock()

    def _new_api(self, session):
        from ayla_iot_unofficial import new_ayla_api
        from ayla_iot_unofficial.fujitsu_consts import FGLAIR_APP_CREDENTIALS
        region = str(self.f.get("region") or "eu").lower()
        app_id, app_secret = FGLAIR_APP_CREDENTIALS.get(region, FGLAIR_APP_CREDENTIALS["default"])
        return new_ayla_api(self.f["username"], self.f["password"], app_id, app_secret,
                            websession=session, europe=(region == "eu"))

    async def _auth(self) -> None:
        from ayla_iot_unofficial.exc import AylaAuthExpiringError
        try:
            self.api.check_auth()
            return
        except AylaAuthExpiringError:
            try:
                await self.api.async_refresh_auth()
                return
            except Exception as ex:  # noqa: BLE001
                log.info("[fglair] token refresh failed (%s), signing in again", ex)
        except Exception:  # noqa: BLE001  (not signed in or expired)
            pass
        await self.api.async_sign_in()

    def _publish(self, dsn: str) -> None:
        dev = self.devices[dsn]
        try:
            name = dev.device_name or dev.name
        except Exception:  # noqa: BLE001
            name = getattr(dev, "name", None) or "Airco"
        REG.upsert("fglair-" + dsn, "aircon", name, aircon_state(dev), dev.is_online(), self._command(dsn))

    async def _refresh(self, dsn: str) -> None:
        dev = self.devices[dsn]
        if dev.is_online():
            await dev.async_update()
        self._publish(dsn)

    async def _sync(self) -> None:
        """List devices (online state), then read every unit."""
        from ayla_iot_unofficial.fujitsu_hvac import FujitsuHVAC
        async with self.lock:
            await self._auth()
            listed = await self.api.async_get_devices()
            for d in listed:
                dsn = d.serial_number
                if not isinstance(d, FujitsuHVAC):
                    if dsn not in self.skipped:
                        self.skipped.add(dsn)
                        log.warning("[fglair] device %s '%s' model %s not supported by ayla-iot-unofficial",
                                    dsn, d.name, getattr(d, "oem_model_number", "?"))
                    continue
                if dsn in self.devices:
                    self.devices[dsn]._connection_status = d._connection_status
                else:
                    log.info("[fglair] unit %s '%s' model %s", dsn, d.name, d.oem_model_number)
                    self.devices[dsn] = d
                try:
                    await self._refresh(dsn)
                except Exception as ex:  # noqa: BLE001
                    log.warning("[fglair] update %s failed: %s", dsn, ex)
                    REG.set_online("fglair-" + dsn, False)

    def _command(self, dsn: str):
        async def handler(cmd: str, value: Any) -> None:
            from ayla_iot_unofficial.fujitsu_consts import FanSpeed, OpMode
            if cmd == "settings":  # from the SmartThings device settings (all air conditioners share it)
                sec = poll_seconds((value or {}).get("poll_seconds"), int(self.f.get("poll_seconds", 60)))
                self.f["poll_seconds"] = sec
                save_poll_seconds("fglair", sec)
                log.info("[fglair] settings from SmartThings: every %s s", sec)
                return
            dev = self.devices[dsn]
            async with self.lock:
                await self._auth()
                if cmd == "refresh":
                    pass
                elif cmd == "on":
                    await dev.async_set_op_mode(OpMode.ON)
                elif cmd == "off":
                    await dev.async_set_op_mode(OpMode.OFF)
                elif cmd == "mode":
                    await dev.async_set_op_mode(OpMode[str(value).upper()])
                elif cmd == "setpoint":
                    await dev.async_set_set_temp(float(value))
                elif cmd == "fan":
                    await dev.async_set_fan_speed(FanSpeed[str(value).upper()])
                elif cmd == "swing":
                    # set each axis the unit has (the library also touches axes the unit lacks)
                    from ayla_iot_unofficial.fujitsu_consts import Capability
                    want = str(value).lower()
                    if want not in ("off", "vertical", "horizontal", "both"):
                        raise ValueError(f"unsupported swing {value}")
                    if dev.has_capability(Capability.SWING_VERTICAL):
                        await dev.async_set_vertical_swing(want in ("vertical", "both"))
                    if dev.has_capability(Capability.SWING_HORIZONTAL):
                        await dev.async_set_horizontal_swing(want in ("horizontal", "both"))
                else:
                    raise ValueError(f"unsupported command {cmd}")
                log.info("[fglair] %s %s %s", dsn, cmd, "" if value is None else value)
                if cmd != "refresh":
                    await asyncio.sleep(2)  # the cloud needs a moment before the unit reports back
                await self._refresh(dsn)
        return handler

    async def run(self) -> None:
        if not fglair_configured(self.f):
            log.info("[fglair] not configured")
            return
        import aiohttp
        async with aiohttp.ClientSession() as session:
            self.api = self._new_api(session)
            while True:
                try:
                    await self.api.async_sign_in()
                    break
                except Exception as ex:  # noqa: BLE001
                    log.error("[fglair] login failed: %s (retry in 5 min)", ex)
                    await asyncio.sleep(300)
            log.info("[fglair] logged in")
            while True:
                try:
                    await self._sync()
                except Exception as ex:  # noqa: BLE001
                    log.warning("[fglair] poll failed: %s", ex)
                    REG.set_online("fglair-", False)
                await asyncio.sleep(int(self.f.get("poll_seconds", 60)))


def poll_seconds(value: Any, default: int = 60) -> int:
    """Update interval from SmartThings or the setup page: 30..600 seconds."""
    try:
        return max(30, min(600, int(float(str(value).replace(",", ".")))))
    except (TypeError, ValueError):
        return default


def save_poll_seconds(section: str, seconds: int) -> None:
    cfg = load_config()
    if cfg.setdefault(section, {}).get("poll_seconds") != seconds:
        cfg[section]["poll_seconds"] = seconds
        save_config(cfg)


# ---------------------------------------------------------------- Škoda (official MyŠkoda public API)

SKODA_API = "https://public.api.connect.skoda-auto.cz/api/v1"
SKODA_PARTS = "info,status,fuelStatus,odometer,airConditioning,charging"


def skoda_configured(k: dict[str, Any]) -> bool:
    return bool(k.get("api_key") and k.get("vin") and not str(k.get("api_key")).startswith("PLAK_HIER"))


def _g(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def car_state(v: dict[str, Any]) -> dict[str, Any]:
    """Flatten a MyŠkoda vehicle into what the Edge driver needs. Missing parts stay None."""
    st = _g(v, "charging", "status") or {}
    bat = st.get("battery") or {}
    fuel = v.get("fuelStatus") or {}
    elec = None
    for eng in (fuel.get("primaryEngineRange"), fuel.get("secondaryEngineRange")):
        if isinstance(eng, dict) and str(eng.get("engineType", "")).lower() == "electric":
            elec = eng
    soc = bat.get("stateOfChargeInPercent")
    if soc is None and elec:
        soc = elec.get("currentSoCInPercent")
    rng = bat.get("remainingCruisingRangeInMeters")
    rng = round(rng / 1000) if isinstance(rng, (int, float)) else (elec or {}).get("remainingRangeInKm")
    overall = _g(v, "status", "overall") or {}
    ac = v.get("airConditioning") or {}
    return {
        "battery": soc,
        "electricRange": rng,
        "totalRange": fuel.get("totalRangeInKm"),
        "fuelLevel": next((e.get("currentFuelLevelInPercent") for e in (fuel.get("primaryEngineRange"),
                          fuel.get("secondaryEngineRange")) if isinstance(e, dict)
                          and e.get("currentFuelLevelInPercent") is not None), None),
        "charging": str(st.get("state") or "").upper() or None,          # e.g. CHARGING, READY_FOR_CHARGING
        "chargePowerKw": st.get("chargePowerInKw"),
        "minutesToFull": st.get("remainingTimeToFullyChargedInMinutes"),
        "plug": str(st.get("plugConnectionState") or "").upper() or None,  # CONNECTED / DISCONNECTED
        "targetSoc": _g(v, "charging", "settings", "targetStateOfChargeInPercent"),
        "atHome": _g(v, "charging", "isVehicleInSavedLocation"),
        "odometer": _g(v, "odometer", "mileageInKm"),
        "locked": str(overall.get("locked") or overall.get("doorsLocked") or "").upper() or None,
        "doors": str(overall.get("doors") or "").upper() or None,
        "windows": str(overall.get("windows") or "").upper() or None,
        "climate": str(ac.get("state") or "").upper() or None,             # ON / OFF / HEATING / COOLING ...
        "climateTarget": _g(ac, "targetTemperature", "value"),
        "updated": _g(v, "charging", "carCapturedTimestamp") or _g(v, "status", "carCapturedTimestamp"),
    }


def skoda_settings(poll: Any, temp: Any, current: dict[str, Any]) -> dict[str, Any]:
    """Validated poll interval (5..60 min) and climate temperature (16..29 C)."""
    out = {"poll_minutes": current.get("poll_minutes", 10), "climate_temp": current.get("climate_temp", 21.0)}
    try:
        if poll not in (None, ""):
            out["poll_minutes"] = max(5, min(60, int(float(str(poll).replace(",", ".")))))
    except ValueError:
        pass
    try:
        if temp not in (None, ""):
            out["climate_temp"] = max(16.0, min(29.0, round(float(str(temp).replace(",", ".")) * 2) / 2))
    except ValueError:
        pass
    return out


class Skoda:
    POLL = 600  # 6 calls per hour by default, leaves room for commands within the 20/hour limit

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.k = cfg.get("skoda") or {}
        self.vin = str(self.k.get("vin") or "").strip().upper()
        try:  # optional "poll_minutes" in config.json, at least 5
            self.POLL = max(300, int(float(self.k.get("poll_minutes") or 10) * 60))
        except (TypeError, ValueError):
            pass
        self.session = None
        self.wake = asyncio.Event()
        self.pause_until = 0.0
        self.last: dict[str, Any] = {}
        self.expect: tuple | None = None  # (command, value, retries left) after a command

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        if time.time() < self.pause_until:
            raise RuntimeError("rate limited, waiting")
        headers = {"X-API-Key": self.k["api_key"], "Accept": "application/json"}
        async with self.session.request(method, SKODA_API + path, headers=headers, json=body) as r:
            exp = r.headers.get("X-API-Key-Expires-At")
            if exp:
                self.last["keyExpires"] = exp
            if r.status == 429:
                wait = int(r.headers.get("Retry-After") or 600)
                self.pause_until = time.time() + wait
                raise RuntimeError(f"rate limited for {wait} s")
            if r.status in (401, 403):
                raise PermissionError("API key expired or not valid for this car (make a new one in the MyŠkoda app)")
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")
            return json.loads(text) if text else {}

    async def _refresh(self) -> None:
        data = await self._call("GET", f"/vehicles/{self.vin}?include={SKODA_PARTS}")
        v = data.get("vehicle") or {}
        state = car_state(v)
        state["keyExpires"] = self.last.get("keyExpires")
        state["pollMinutes"] = self.POLL // 60
        state["climateTemp"] = float(self.k.get("climate_temp") or 21)
        state["errors"] = [e.get("type") for e in data.get("errors") or [] if isinstance(e, dict)]
        name = v.get("name") or v.get("licensePlate") or "Škoda"
        REG.upsert("skoda-" + self.vin, "car", name, state, True, self._command())
        log.info("[skoda] %s: battery %s%%, %s km, charging %s, plug %s", name, state["battery"],
                 state["electricRange"], state["charging"], state["plug"])
        if self.expect:
            cmd, value, left = self.expect
            done = {"on": state["charging"] == "CHARGING", "off": state["charging"] != "CHARGING",
                    "climate_on": state["climate"] not in (None, "OFF"), "climate_off": state["climate"] == "OFF",
                    "limit": state["targetSoc"] == value}.get(cmd, True)
            self.expect = None
            if not done and left > 0:
                self.expect = (cmd, value, left - 1)
                asyncio.get_running_loop().call_later(60, self.wake.set)

    def _command(self):
        async def handler(cmd: str, value: Any) -> None:
            paths = {"on": "/charging/start", "off": "/charging/stop",
                     "climate_off": "/air-conditioning/stop"}
            if cmd == "refresh":
                self.wake.set()
                return
            if cmd == "settings":  # from the SmartThings device settings
                v = value if isinstance(value, dict) else {}
                cfg = load_config()
                k = cfg.setdefault("skoda", {})
                new = skoda_settings(v.get("poll_minutes"), v.get("climate_temp"), k)
                if new != {"poll_minutes": k.get("poll_minutes", 10), "climate_temp": k.get("climate_temp", 21.0)}:
                    k.update(new)
                    save_config(cfg)
                self.k.update(new)
                self.POLL = int(new["poll_minutes"]) * 60
                log.info("[skoda] settings from SmartThings: every %s min, climate %s C", new["poll_minutes"],
                         new["climate_temp"])
                return
            if cmd == "climate_on":
                temp = float(self.k.get("climate_temp") or 21)
                await self._call("POST", f"/vehicles/{self.vin}/air-conditioning/start",
                                 {"targetTemperature": {"value": temp, "unit": "CELSIUS"}})
            elif cmd == "limit":
                await self._call("PUT", f"/vehicles/{self.vin}/charging/limit",
                                 {"targetStateOfChargeInPercent": int(value)})
            elif cmd in paths:
                await self._call("POST", f"/vehicles/{self.vin}{paths[cmd]}")
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[skoda] command %s %s", cmd, "" if value is None else value)
            # the car needs a moment to report back: look after 30 s, and once more 60 s later if the
            # change is not visible yet (each look costs one of the 20 requests per hour)
            self.expect = (cmd, value, 1)
            asyncio.get_running_loop().call_later(30, self.wake.set)
        return handler

    async def run(self) -> None:
        if not skoda_configured(self.k):
            log.info("[skoda] not configured")
            return
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            self.session = session
            while True:
                try:
                    await self._refresh()
                except PermissionError as ex:
                    log.error("[skoda] %s", ex)
                    REG.set_online("skoda-", False)
                    return
                except Exception as ex:  # noqa: BLE001
                    log.warning("[skoda] update failed: %s", ex)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.POLL)
                except asyncio.TimeoutError:
                    pass


# ---------------------------------------------------------------- iRobot account (favorites for the Roomba driver)

IROBOT: dict[str, Any] = {"robots": {}, "updated": 0, "error": None, "refresh": None}


def irobot_configured(r: dict[str, Any]) -> bool:
    return bool(r.get("username") and r.get("password") and not str(r.get("password")).startswith("PLAK_HIER"))


def favorites_for(raw: list[dict[str, Any]], blid: str) -> list[dict[str, Any]]:
    """Turn the account's favorites into local start commands for one robot."""
    out = []
    for f in raw or []:
        if not isinstance(f, dict) or f.get("deleted") or f.get("hidden"):
            continue
        cmds = [c for c in (f.get("commanddefs") or []) if isinstance(c, dict)]
        mine = [c for c in cmds if str(c.get("robot_id") or c.get("robotId") or blid) == blid]
        if not mine:
            continue
        c = mine[0]
        # "id" names the command inside the favorite; the robot does not need it
        params = {k: v for k, v in c.items()
                  if k not in ("id", "robot_id", "robotId", "command", "time", "initiator")}
        out.append({"name": f.get("name") or "Favorite", "id": f.get("favorite_id"),
                    "command": c.get("command") or "start", "params": params, "order": f.get("display_order")})
    out.sort(key=lambda x: (x["order"] is None, x["order"] or 0))
    return out


class IRobotCloud:
    REFRESH = 12 * 3600
    MIN_GAP = 600

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.r = cfg.get("irobot") or {}
        self.account = None
        self.wake = asyncio.Event()

    async def _fetch(self) -> None:
        raw = await self.account.classic_rest().get_favorites()
        robots = {}
        for blid, entry in (self.account.robots or {}).items():
            robots[blid] = {"name": getattr(entry, "name", None), "favorites": favorites_for(raw, blid)}
        IROBOT.update(robots=robots, updated=int(time.time()), error=None)
        log.info("[irobot] %d favorite(s) for %d robot(s)", sum(len(r["favorites"]) for r in robots.values()),
                 len(robots))

    async def run(self) -> None:
        if not irobot_configured(self.r):
            log.info("[irobot] not configured (optional: favorite names for the Roomba driver)")
            return
        import aiohttp
        from roombapy_prime import CloudAccount
        from roombapy_prime.auth import AuthCredentialsError, AuthRateLimitedError
        IROBOT["refresh"] = self.wake
        async with aiohttp.ClientSession() as session:
            last = 0.0
            while True:
                try:
                    if self.account is None:
                        self.account = await CloudAccount.login(session, self.r["username"], self.r["password"],
                                                                str(self.r.get("country") or "NL").upper())
                        log.info("[irobot] logged in, %d robot(s) on the account", len(self.account.robots or {}))
                    last = time.time()
                    await self._fetch()
                except AuthCredentialsError as ex:
                    IROBOT["error"] = "login refused (check e-mail and password)"
                    log.error("[irobot] login refused: %s. Not retrying; fix it on the setup page", ex)
                    return
                except AuthRateLimitedError as ex:
                    IROBOT["error"] = "iRobot asks to wait (too many logins)"
                    log.warning("[irobot] rate limited: %s (retry in 30 min)", ex)
                    self.account = None
                    await asyncio.sleep(1800)
                    continue
                except Exception as ex:  # noqa: BLE001
                    IROBOT["error"] = str(ex)[:200]
                    log.warning("[irobot] update failed: %s", ex)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.REFRESH)
                except asyncio.TimeoutError:
                    pass
                gap = self.MIN_GAP - (time.time() - last)  # refresh from the hub: at most every 10 minutes
                if gap > 0:
                    await asyncio.sleep(gap)


# ---------------------------------------------------------------- VELUX ACTIVE departure mode (lock / unlock)
# The windows themselves are controlled locally over HomeKit by the VELUX driver on the hub. The departure mode
# (away = all windows and blinds blocked) only exists in the VELUX/Netatmo cloud. Locking is a plain command,
# unlocking must be signed with a key that the gateway hands out locally (port 25050) after its button is pressed.
# Protocol as documented by ha-velux-active (github.com/Niek/ha-velux-active, MIT).

VELUX_NETATMO = "https://api.netatmo.com/"
VELUX_SYNC = "https://app.velux-active.com/syncapi/v1/"
VELUX_CLIENT = {"client_id": "5931426da127d981e76bdd3f", "client_secret": "6ae2d89d15e767ae5c56b456b452d319"}
VELUX_APP_VERSION = "791302006"
VELUX_PAIR: dict[str, Any] = {"state": None, "error": None, "start": None}


def velux_configured(v: dict[str, Any]) -> bool:
    return bool(v.get("username") and v.get("password"))


def velux_sign(hash_sign_key: str, message: str) -> str:
    """HMAC-SHA512 with the gateway key (base64), url-safe base64 like the VELUX app."""
    import hashlib
    import hmac
    raw = hash_sign_key.strip().replace("-", "+").replace("_", "/")
    key = base64.b64decode(raw + "=" * (-len(raw) % 4))
    digest = hmac.new(key, message.encode(), hashlib.sha512).digest()
    return base64.b64encode(digest).decode().replace("+", "-").replace("/", "_")


def velux_scenario_hash(hash_sign_key: str, scenario: str, ts: int, nonce: int, gateway: str) -> str:
    return velux_sign(hash_sign_key, f"scenario{scenario}{ts}{nonce}{gateway}")


def velux_retrieve_key(host: str, timeout: float = 180.0, port: int = 25050) -> tuple[str, str]:
    """Wait for the gateway's pairing listener (after its button is pressed) and fetch a signing key.
    Returns (sign_key_id, hash_sign_key), both url-safe base64. Blocking: run it in a thread."""
    import hashlib
    import struct
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives import serialization

    PING, PONG, CLOSE, SECURE = 0x0000, 0x0001, 0x0008, 0x0200
    ECDH_REQ, ECDH_RESP = 0x0201, 0x0202
    NONCE_REQ, NONCE_RESP, CHAL_REQ, CHAL_RESP, KEY_REQ, KEY_RESP = 0x020A, 0x020B, 0x020C, 0x020D, 0x020E, 0x020F

    def frame(t: int, payload: bytes = b"") -> bytes:
        return struct.pack("<HH", t, len(payload)) + payload

    def bump(n: bytearray) -> None:
        for i in range(11, 3, -1):
            if n[i] < 0xFF:
                n[i] += 1
                return
            n[i] = 0
        n[:] = bytes(12)

    def attempt() -> tuple[str, str]:
        s = socket.create_connection((host, port), timeout=10)
        s.settimeout(10)
        buf = bytearray()

        def recv() -> tuple[int, bytes]:
            while True:
                if len(buf) >= 4:
                    t, n = struct.unpack_from("<HH", buf)
                    if len(buf) >= 4 + n:
                        out = (t, bytes(buf[4:4 + n]))
                        del buf[:4 + n]
                        if t == PING:
                            s.sendall(frame(PONG))
                            continue
                        return out
                chunk = s.recv(4096)
                if not chunk:
                    raise ConnectionError("gateway closed the connection")
                buf.extend(chunk)

        try:
            s.sendall(frame(PING))
            if recv()[0] != PONG:
                raise ConnectionError("no pong")
            priv = x25519.X25519PrivateKey.generate()
            pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            s.sendall(frame(ECDH_REQ, b"\x01" + pub))
            t, gw_pub = recv()
            if t != ECDH_RESP:
                raise ConnectionError(f"unexpected frame {t:#06x}")
            if len(gw_pub) == 33 and gw_pub[0] in (0, 1):
                gw_pub = gw_pub[1:]
            shared = priv.exchange(x25519.X25519PublicKey.from_public_bytes(gw_pub))
            aead = ChaCha20Poly1305(hashlib.sha512(shared).digest()[:32])
            tx, rx = bytearray(12), bytearray(12)

            def send_secure(t: int, payload: bytes = b"") -> None:
                s.sendall(frame(SECURE, aead.encrypt(bytes(tx), frame(t, payload), None)))
                bump(tx)

            def recv_secure(want: int) -> bytes:
                t, p = recv()
                if t != SECURE:
                    raise ConnectionError(f"unexpected frame {t:#06x}")
                plain = aead.decrypt(bytes(rx), p, None)
                bump(rx)
                t, n = struct.unpack_from("<HH", plain)
                if t != want or len(plain) != 4 + n:
                    raise ConnectionError(f"unexpected secure frame {t:#06x}")
                return plain[4:]

            key_id = uuid.uuid4().bytes
            send_secure(KEY_REQ, b"\x00" + key_id)
            r = recv_secure(KEY_RESP)
            if not r or r[0] != 0 or len(r) != 33:
                raise PermissionError({1: "refused", 2: "bad key id", 3: "key table full"}.get(r[0] if r else -1,
                                                                                           "refused"))
            gw_key = r[1:]
            send_secure(NONCE_REQ, b"\x00")
            nonce = recv_secure(NONCE_RESP)
            send_secure(CHAL_REQ, key_id + hashlib.sha512(key_id + gw_key + nonce).digest())
            r = recv_secure(CHAL_RESP)
            if not r or r[0] != 0:
                raise PermissionError("gateway refused the key check")
            send_secure(CLOSE)
            return base64.urlsafe_b64encode(key_id).decode(), base64.urlsafe_b64encode(gw_key).decode()
        finally:
            s.close()

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
        except OSError:
            time.sleep(1)
            continue
        try:
            return attempt()
        except PermissionError:
            raise
        except Exception as ex:  # noqa: BLE001  (listener not ready yet: try again)
            last = ex
            time.sleep(1)
    raise TimeoutError(f"gateway did not open its pairing port ({last or 'button not pressed?'})")


class VeluxCloud:
    POLL = 60  # default, "poll_seconds" in config.json (setup page or SmartThings)

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.v = cfg.get("velux") or {}
        self.session = None
        self.wake = asyncio.Event()
        self.home: dict[str, Any] | None = None  # {"id", "timezone", "gateway", "name"}
        self.last_ts, self.last_nonce = 0, -1
        self.fast = 0  # fast polls left after a command

    # -- auth
    async def _tokens(self, data: dict[str, str]) -> None:
        body = {**VELUX_CLIENT, "app_version": VELUX_APP_VERSION, **data}
        async with self.session.post(VELUX_NETATMO + "oauth2/token", data=body) as r:
            raw = await r.json(content_type=None)
        if r.status >= 400 or "access_token" not in (raw or {}):
            err = (raw or {}).get("error") if isinstance(raw, dict) else None
            if r.status in (400, 401, 403) or err == "invalid_grant":
                raise PermissionError(f"login refused ({err or r.status})")
            raise RuntimeError(f"token request failed: HTTP {r.status}")
        t = {"access_token": raw["access_token"], "refresh_token": raw.get("refresh_token"),
             "expires_at": int(time.time()) + int(raw.get("expires_in") or raw.get("expire_in") or 3600)}
        self.v["tokens"] = t
        cfg = load_config()
        cfg.setdefault("velux", {})["tokens"] = t
        save_config(cfg)

    async def _access(self) -> str:
        t = self.v.get("tokens") or {}
        if t.get("access_token") and time.time() < t.get("expires_at", 0) - 60:
            return t["access_token"]
        if t.get("refresh_token"):
            try:
                await self._tokens({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
                return self.v["tokens"]["access_token"]
            except PermissionError:
                log.info("[velux] refresh token refused, logging in again")
        await self._tokens({"grant_type": "password", "username": self.v["username"], "password": self.v["password"],
                            "scope": "velux_scopes", "user_prefix": "velux"})
        return self.v["tokens"]["access_token"]

    async def _post(self, url: str, data: dict | None = None, body: dict | None = None) -> dict[str, Any]:
        for again in (True, False):
            headers = {"Authorization": "Bearer " + await self._access()}
            kw = {"json": body} if body is not None else {"data": data or {}}
            async with self.session.post(url, headers=headers, **kw) as r:
                text = await r.text()
            if r.status in (401, 403) and again:
                self.v.setdefault("tokens", {})["expires_at"] = 0  # token revoked: refresh once
                continue
            try:
                raw = json.loads(text) if text.strip() else {}
            except ValueError:
                raw = {"raw": text[:200]}
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(raw)[:200]}")
            errors = (raw.get("body") or {}).get("errors") if isinstance(raw.get("body"), dict) else None
            if errors:
                raise RuntimeError(f"VELUX refused: {errors}")
            return raw
        raise RuntimeError("unauthorized")

    # -- state
    async def _discover(self) -> None:
        raw = await self._post(VELUX_NETATMO + "api/homesdata")
        for h in (raw.get("body") or {}).get("homes") or []:
            gws = [m for m in h.get("modules") or [] if m.get("type") == "NXG"]
            if gws:
                gw = gws[0]
                self.home = {"id": h["id"], "timezone": h.get("timezone") or "Europe/Amsterdam",
                             "gateway": gw["id"], "name": gw.get("name") or h.get("name") or "VELUX"}
                log.info("[velux] home %s, gateway %s", h.get("name"), gw["id"])
                return
        raise RuntimeError("no VELUX gateway on this account")

    async def _refresh(self) -> None:
        if not self.home:
            await self._discover()
        raw = await self._post(VELUX_NETATMO + "api/homestatus", data={"home_id": self.home["id"]})
        mods = ((raw.get("body") or {}).get("home") or {}).get("modules") or []
        gw = next((m for m in mods if m.get("id") == self.home["gateway"]), None)
        if gw is None:
            REG.set_online("velux-", False)
            raise RuntimeError("gateway not in homestatus (offline?)")
        cfg = load_config().get("velux") or {}
        state = {"locked": bool(gw.get("locked")), "locking": bool(gw.get("locking")), "busy": bool(gw.get("busy")),
                 "canUnlock": bool(cfg.get("hash_sign_key") and cfg.get("sign_key_id")),
                 "pairing": VELUX_PAIR["state"]}
        REG.upsert("velux-" + self.home["gateway"], "velux_lock", "VELUX vertrekstand", state, True, self._command())
        if "is_raining" in gw:  # the gateway's rain sensor as its own device
            REG.upsert("velux-rain-" + self.home["gateway"], "rain", "VELUX regensensor",
                       {"raining": bool(gw.get("is_raining"))}, True, self._refresh_only())
        log.debug("[velux] locked %s, raining %s", state["locked"], gw.get("is_raining"))

    async def _scenario(self, modules: list[dict]) -> None:
        payload = {"app_type": "app_velux", "app_version": VELUX_APP_VERSION,
                   "home": {"id": self.home["id"], "timezone": self.home["timezone"], "modules": modules}}
        await self._post(VELUX_SYNC + "setstate", body=payload)

    def _refresh_only(self):
        async def handler(cmd: str, value: Any) -> None:
            if cmd != "refresh":
                raise ValueError(f"unsupported command {cmd}")
            self.wake.set()
        return handler

    def _command(self):
        async def handler(cmd: str, value: Any) -> None:
            if cmd == "settings":  # from the SmartThings device settings
                sec = poll_seconds((value or {}).get("poll_seconds"), int(self.v.get("poll_seconds") or 60))
                self.v["poll_seconds"] = sec
                save_poll_seconds("velux", sec)
                log.info("[velux] settings from SmartThings: every %s s", sec)
                return
            if cmd == "refresh":
                self.wake.set()
                return
            if cmd == "lock":
                await self._scenario([{"id": self.home["gateway"], "scenario": "away"}])
            elif cmd == "unlock":
                cfg = load_config().get("velux") or {}
                key, key_id = cfg.get("hash_sign_key"), cfg.get("sign_key_id")
                if not (key and key_id):
                    raise PermissionError("unlock needs the gateway key: press 'Gateway koppelen' on the setup page")
                if cfg.get("sign_key_gateway") and cfg["sign_key_gateway"] != self.home["gateway"]:
                    raise PermissionError("key belongs to another gateway: pair again")
                now = int(time.time())
                ts, nonce = (self.last_ts, self.last_nonce + 1) if now <= self.last_ts else (now, 0)
                self.last_ts, self.last_nonce = ts, nonce
                await self._scenario([{"id": self.home["gateway"], "nonce": nonce, "sign_key_id": key_id,
                                       "scenario": "home", "timestamp": ts,
                                       "hash_scenario": velux_scenario_hash(key, "home", ts, nonce,
                                                                            self.home["gateway"])}])
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[velux] %s sent", cmd)
            self.fast = 4  # the gateway applies it with a delay
            self.wake.set()
        return handler

    async def pair(self, host: str) -> None:
        """Ask the cloud to put the gateway in key mode, then fetch the key locally once its button is pressed."""
        VELUX_PAIR.update(state="waiting", error=None, start=int(time.time()))
        try:
            if not self.home:
                await self._discover()
            await self._post(VELUX_NETATMO + "api/setstate", body={
                "home": {"id": self.home["id"], "modules": [{"id": self.home["gateway"], "retrieve_key": True}]}})
            log.info("[velux] gateway in key mode: press the button on the gateway now")
            VELUX_PAIR["state"] = "press"
            key_id, key = await asyncio.to_thread(velux_retrieve_key, host)
            cfg = load_config()
            v = cfg.setdefault("velux", {})
            v.update(sign_key_id=key_id, hash_sign_key=key, sign_key_gateway=self.home["gateway"], gateway_ip=host)
            save_config(cfg)
            VELUX_PAIR.update(state="done", error=None)
            log.info("[velux] gateway paired, unlocking is possible now")
            self.wake.set()
        except Exception as ex:  # noqa: BLE001
            VELUX_PAIR.update(state="failed", error=str(ex)[:200])
            log.warning("[velux] pairing failed: %s", ex)

    async def run(self) -> None:
        if not velux_configured(self.v):
            log.info("[velux] not configured (optional: VELUX departure mode lock/unlock)")
            return
        import aiohttp
        VELUX["source"] = self
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            self.session = session
            while True:
                try:
                    await self._refresh()
                except PermissionError as ex:
                    log.error("[velux] %s. Not retrying; fix it on the setup page", ex)
                    VELUX["error"] = str(ex)
                    REG.set_online("velux-", False)
                    return
                except Exception as ex:  # noqa: BLE001
                    log.warning("[velux] update failed: %s", ex)
                    self.home = None if "no VELUX gateway" in str(ex) else self.home
                self.wake.clear()
                wait = 5 if self.fast > 0 else int(self.v.get("poll_seconds") or self.POLL)
                self.fast = max(0, self.fast - 1)
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass


VELUX: dict[str, Any] = {"source": None, "error": None}


# ---------------------------------------------------------------- Buienradar (rain radar forecast, NL and BE)
# Two hours ahead in 5 minute steps for a point. For a radius the bridge also reads points on a ring around it
# (and a half ring) and takes the heaviest rain per time step. Data: Buienradar (gpsgadget raintext).

BUIENRADAR_URL = "https://gpsgadget.buienradar.nl/data/raintext"
RAIN_THRESHOLD = 0.1  # mm/h counts as rain


PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"  # Dutch addresses (Kadaster, free)
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"  # worldwide, 15 minute steps (CC BY 4.0, no key)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"  # addresses worldwide (OpenStreetMap, ODbL)
RAIN_PROVIDERS = ("auto", "buienradar", "openmeteo")


def in_benelux(lat: float, lon: float) -> bool:
    """Area of the Buienradar radar forecast (the Netherlands, Belgium and a margin)."""
    return 49.4 <= lat <= 53.7 and 2.5 <= lon <= 7.3


def rain_provider(b: dict[str, Any]) -> str:
    p = b.get("provider") or "auto"
    if p == "auto":
        return "buienradar" if buienradar_configured(b) and in_benelux(float(b["lat"]), float(b["lon"])) else "openmeteo"
    return p


def buienradar_enabled(b: dict[str, Any]) -> bool:
    return bool(b.get("enabled")) or b.get("lat") is not None


def buienradar_configured(b: dict[str, Any]) -> bool:
    return b.get("lat") is not None and b.get("lon") is not None


def normalize_postcode(pc: Any) -> str | None:
    import re
    t = re.sub(r"\s+", "", str(pc or "")).upper()
    return t if re.fullmatch(r"\d{4}[A-Z]{2}", t) else None


def house_number(n: Any) -> int | None:
    import re
    m = re.match(r"\s*(\d+)", str(n or ""))
    return int(m.group(1)) if m else None


def parse_location(text: Any) -> tuple[float, float] | None:
    """'52.35, 5.62' or '52.35 5.62' -> (52.35, 5.62); None when not a valid location."""
    import re
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", str(text or ""))
    if len(nums) != 2:
        return None
    lat, lon = (float(n.replace(",", ".")) for n in nums)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def buienradar_settings(v: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Validated settings from the device or the setup page. Keys in v: postcode + housenumber (NL), address (any
    country, free text), location ('lat, lon', wins over both), radius_m (0..25000), threshold (mm/h), provider
    (auto, buienradar, openmeteo). Missing keys keep the current value; empty text clears it. lat/lon are filled in
    later from the postcode or the address."""
    out = {k: current.get(k) for k in ("postcode", "housenumber", "address", "location", "lat", "lon", "place")}
    out["provider"] = current.get("provider", "auto")
    if "address" in v:
        out["address"] = str(v.get("address") or "").strip()[:200] or None
    if v.get("provider") in RAIN_PROVIDERS:
        out["provider"] = v["provider"]
    out["radius_m"] = current.get("radius_m", int(float(current.get("radius_km") or 0) * 1000))
    out["threshold"] = current.get("threshold", RAIN_THRESHOLD)
    if "postcode" in v:
        out["postcode"] = normalize_postcode(v.get("postcode"))
    if "housenumber" in v:
        out["housenumber"] = house_number(v.get("housenumber"))
    if "location" in v:
        loc = parse_location(v.get("location"))
        out["location"] = f"{loc[0]}, {loc[1]}" if loc else None
    for key, lo, hi, cast in (("radius_m", 0, 25000, int), ("threshold", 0.05, 5.0, float)):
        try:
            if v.get(key) not in (None, ""):
                out[key] = max(lo, min(hi, cast(float(str(v[key]).replace(",", ".")))))
        except ValueError:
            pass
    loc = parse_location(out["location"]) if out["location"] else None
    if loc:
        out["lat"], out["lon"], out["place"] = round(loc[0], 4), round(loc[1], 4), out["location"]
    elif (not ((out["postcode"] and out["housenumber"]) or out["address"]) or current.get("location")
          or (out["postcode"], out["housenumber"], out["address"])
          != (current.get("postcode"), current.get("housenumber"), current.get("address"))):
        out["lat"] = out["lon"] = out["place"] = None  # look the address up again
    return out


async def geocode_address(session, address: str) -> tuple[float, float, str] | None:
    """Free text address anywhere -> (lat, lon, name) via OpenStreetMap Nominatim (once per address change)."""
    async with session.get(NOMINATIM_URL, params={"q": address, "format": "jsonv2", "limit": "1"},
                           headers={"User-Agent": f"stoeckie-bridge/{VERSION} (SmartThings Edge bridge)"}) as r:
        if r.status != 200:
            raise RuntimeError(f"Nominatim HTTP {r.status}")
        data = await r.json(content_type=None)
    if not data:
        return None
    d = data[0]
    return round(float(d["lat"]), 5), round(float(d["lon"]), 5), str(d.get("display_name") or address)[:120]


def parse_openmeteo(data: Any, now: dt.datetime | None = None) -> list[list[float]]:
    """Open-Meteo minutely_15 precipitation (mm per 15 min, one or more locations) -> mm/h per 5 minutes from now."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for loc in data if isinstance(data, list) else [data]:
        m = (loc or {}).get("minutely_15") or {}
        times, rain = m.get("time") or [], m.get("precipitation") or []
        rates: list[float] = []
        for t, mm in zip(times, rain):
            start = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
            for k in range(3):
                if start + dt.timedelta(minutes=5 * (k + 1)) > now:  # skip 5 minute steps that are over
                    rates.append(round(float(mm or 0) * 4, 2))
        out.append(rates[:24])
    return out


async def geocode_nl(session, postcode: str, number: int) -> tuple[float, float, str] | None:
    """Dutch postcode + house number -> (lat, lon, address) via the PDOK Locatieserver."""
    params = [("q", "*"), ("fq", f"postcode:{postcode}"), ("fq", f"huisnummer:{number}"), ("fq", "type:adres"),
              ("rows", "1"), ("fl", "weergavenaam,centroide_ll")]
    async with session.get(PDOK_URL, params=params) as r:
        if r.status != 200:
            raise RuntimeError(f"PDOK HTTP {r.status}")
        data = await r.json(content_type=None)
    docs = (data.get("response") or {}).get("docs") or []
    if not docs:
        return None
    import re
    m = re.match(r"POINT\(([-\d.]+) ([-\d.]+)\)", docs[0].get("centroide_ll") or "")
    if not m:
        return None
    return round(float(m.group(2)), 5), round(float(m.group(1)), 5), docs[0].get("weergavenaam") or postcode


def rain_points(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    """Center plus rings at the radius (and half of it): 8 directions each, rounded like Buienradar (2 decimals)."""
    import math
    pts = [(round(lat, 2), round(lon, 2))]
    radius_km = radius_m / 1000
    rings = [radius_km] + ([radius_km / 2] if radius_km >= 3 else []) if radius_km > 0 else []
    for r in rings:
        for k in range(8):
            a = math.radians(45 * k)
            dlat = r * math.cos(a) / 111.32
            dlon = r * math.sin(a) / (111.32 * math.cos(math.radians(lat)))
            p = (round(lat + dlat, 2), round(lon + dlon, 2))
            if p not in pts:
                pts.append(p)
    return pts


def parse_raintext(text: str) -> list[float]:
    """Buienradar raintext ('077|10:35' per line) -> mm/h per 5 minutes."""
    out = []
    for line in text.splitlines():
        v = line.split("|")[0].strip()
        if not v.isdigit():
            continue
        n = int(v)
        out.append(0.0 if n == 0 else round(10 ** ((n - 109) / 32), 2))
    return out


def rain_summary(rates: list[float], threshold: float = RAIN_THRESHOLD, lang: str = "nl") -> dict[str, Any]:
    """Now, minutes until rain, how long it rains, intensity class and a short summary (Dutch or English)."""
    if not rates:
        return {}
    wet = [r >= threshold for r in rates]
    horizon = len(rates) * 5
    now = rates[0]
    if wet[0]:
        until = 0
        start = 0
    else:
        start = next((i for i, w in enumerate(wet) if w), None)
        until = 999 if start is None else start * 5
    lasts = 0
    if start is not None:
        end = next((i for i in range(start, len(wet)) if not wet[i]), len(wet))
        lasts = (end - start) * 5
    peak = max(rates[start:start + lasts // 5] or [0]) if start is not None else 0.0
    total = round(sum(rates) / 12, 1)  # mm in the next two hours

    def level(r: float) -> str:
        if r < threshold:
            return "none"
        if r < 2.5:
            return "light"
        if r < 10:
            return "moderate"
        if r < 50:
            return "heavy"
        return "violent"
    intensity = level(now)
    if intensity == "none" and until <= 15:
        intensity = "possiblePrecipitation"
    more = "+" if start is not None and start * 5 + lasts >= horizon else ""
    if lang == "nl":
        if wet[0]:
            text = f"Regent nu ({now:g} mm/u), nog {lasts}{more} min"
        elif start is not None:
            text = f"Regen over {until} min, {lasts}{more} min lang, max {peak:g} mm/u"
        else:
            text = f"Droog, geen regen verwacht ({horizon // 60} uur)"
    else:
        if wet[0]:
            text = f"Raining now ({now:g} mm/h), {lasts}{more} more min"
        elif start is not None:
            text = f"Rain in {until} min, for {lasts}{more} min, up to {peak:g} mm/h"
        else:
            text = f"Dry, no rain expected ({horizon // 60} h)"
    return {"raining": bool(wet[0]), "rainRate": round(now, 2), "minutesUntilRain": until, "minutesOfRain": lasts,
            "peakRate": round(peak, 2), "totalMm": total, "intensity": intensity, "summary": text,
            "horizonMinutes": horizon}


class Buienradar:
    POLL = 300  # Buienradar refreshes every 5 minutes

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.b = cfg.get("buienradar") or {}
        self.session = None
        self.wake = asyncio.Event()
        self.error: str | None = None

    async def _fetch(self, lat: float, lon: float) -> list[float]:
        async with self.session.get(BUIENRADAR_URL, params={"lat": f"{lat:.2f}", "lon": f"{lon:.2f}"},
                                    headers={"User-Agent": "stoeckie-bridge (SmartThings)"}) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            return parse_raintext(await r.text())

    async def _fetch_openmeteo(self, pts: list[tuple[float, float]]) -> list[list[float]]:
        """All points in one request (Open-Meteo accepts lists of coordinates)."""
        params = {"latitude": ",".join(f"{p[0]:.4f}" for p in pts), "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
                  "minutely_15": "precipitation", "forecast_minutely_15": "9", "timezone": "UTC"}
        async with self.session.get(OPENMETEO_URL, params=params,
                                    headers={"User-Agent": f"stoeckie-bridge/{VERSION}"}) as r:
            if r.status != 200:
                raise RuntimeError(f"Open-Meteo HTTP {r.status}")
            return parse_openmeteo(await r.json(content_type=None))

    def _settings_state(self) -> dict[str, Any]:
        b = self.b
        return {"postcode": b.get("postcode"), "housenumber": b.get("housenumber"), "address": b.get("address"),
                "place": b.get("place"),
                "lat": b.get("lat"), "lon": b.get("lon"), "radiusM": b.get("radius_m", 0),
                "threshold": b.get("threshold", RAIN_THRESHOLD)}

    async def _locate(self) -> None:
        """Postcode + house number (NL) or an address (anywhere) -> lat/lon (once, stored in config.json)."""
        b = self.b
        if buienradar_configured(b):
            return
        if b.get("postcode") and b.get("housenumber"):
            found = await geocode_nl(self.session, b["postcode"], int(b["housenumber"]))
            what = f"{b['postcode']} {b['housenumber']}"
        elif b.get("address"):
            found = await geocode_address(self.session, b["address"])
            what = b["address"]
        else:
            return
        if not found:
            self.error = f"Adres niet gevonden / address not found: {what}"
            return
        b["lat"], b["lon"], b["place"] = found
        cfg = load_config()
        cfg.setdefault("buienradar", {}).update({k: b[k] for k in ("lat", "lon", "place")})
        save_config(cfg)
        log.info("[buienradar] location from the address: %s", b["place"])

    async def _refresh(self) -> None:
        self.error = None
        await self._locate()
        if not buienradar_configured(self.b):
            summary = self.error or ("Geen locatie: vul postcode en huisnummer of adres in (instellingen van dit apparaat)"
                                     " / No location: enter your address or coordinates in the device settings")
            REG.upsert("buienradar", "rainradar", "Buienradar", {"summary": summary, **self._settings_state()}, True,
                       self._command())
            return
        lat, lon = float(self.b["lat"]), float(self.b["lon"])
        radius = float(self.b.get("radius_m") or 0)
        pts = rain_points(lat, lon, radius)
        provider = rain_provider(self.b)
        if provider == "openmeteo":
            series = [s for s in await self._fetch_openmeteo(pts) if s]
        else:
            sem = asyncio.Semaphore(4)

            async def one(p):
                async with sem:
                    return await self._fetch(*p)
            series = [s for s in await asyncio.gather(*(one(p) for p in pts), return_exceptions=True)
                      if isinstance(s, list) and s]
        if not series:
            raise RuntimeError(f"no data from {provider}")
        n = min(len(s) for s in series)
        rates = [max(s[i] for s in series) for i in range(n)]  # heaviest rain anywhere in the radius
        lang = "nl" if in_benelux(lat, lon) else "en"
        state = rain_summary(rates, float(self.b.get("threshold") or RAIN_THRESHOLD), lang)
        state.update(points=len(series), provider=provider, **self._settings_state())
        name = "Buienradar" if provider == "buienradar" else "Rain radar"
        REG.upsert("buienradar", "rainradar", name, state, True, self._command())
        log.info("[buienradar] %s (%d point(s), radius %g m)", state["summary"], len(series), radius)

    def apply_settings(self, v: dict[str, Any]) -> bool:
        """Store new settings; True when something changed (then the location is looked up again)."""
        cfg = load_config()
        cur = cfg.setdefault("buienradar", {})
        new = buienradar_settings(v, cur)
        changed = any(cur.get(k) != new.get(k) for k in new)
        if changed:
            cur.update(new)
            cur["enabled"] = True
            cur.pop("radius_km", None)
            save_config(cfg)
            self.b = dict(cur)
            log.info("[buienradar] settings: %s %s / %s, radius %s m, from %s mm/h", new.get("postcode"),
                     new.get("housenumber"), new.get("location"), new.get("radius_m"), new.get("threshold"))
        return changed

    def _command(self):
        async def handler(cmd: str, value: Any) -> None:
            if cmd == "refresh":
                self.wake.set()
                return
            if cmd == "settings":  # from the SmartThings device settings
                if self.apply_settings(value if isinstance(value, dict) else {}):
                    self.wake.set()
                return
            raise ValueError(f"unsupported command {cmd}")
        return handler

    async def run(self) -> None:
        if not buienradar_enabled(self.b):
            log.info("[buienradar] off (optional: switch it on in the Stoeckie Bridge device or on the setup page)")
            return
        if "radius_m" not in self.b and self.b.get("radius_km") is not None:
            self.b["radius_m"] = int(float(self.b["radius_km"]) * 1000)
        import aiohttp
        BUIENRADAR["source"] = self
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            self.session = session
            while True:
                try:
                    await self._refresh()
                except Exception as ex:  # noqa: BLE001
                    log.warning("[buienradar] update failed: %s", ex)
                    REG.set_online("buienradar", False)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.POLL)
                except asyncio.TimeoutError:
                    pass


BUIENRADAR: dict[str, Any] = {"source": None}


# ---------------------------------------------------------------- HTTPS for the setup page (self-signed)

def _local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; picks the LAN interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("169.254."))
    except OSError:
        pass
    return sorted(ips)


def setup_tls(data_dir: Path):
    """SSL context for the setup page with a self-signed certificate (made once, kept in the data folder).
    Returns (context, SHA-256 fingerprint) or (None, error)."""
    import ssl
    cert, key = data_dir / "setup-cert.pem", data_dir / "setup-key.pem"
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        if not (cert.exists() and key.exists()):
            k = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Stoeckie Bridge " + socket.gethostname())])
            alt = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in _local_ips()]
            now = dt.datetime.now(dt.timezone.utc)
            c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
                 .not_valid_after(now + dt.timedelta(days=3650))
                 .add_extension(x509.SubjectAlternativeName(alt), critical=False)
                 .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                 .sign(k, hashes.SHA256()))
            key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
            cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
            log.info("[https] new self-signed certificate for %s", ", ".join(_local_ips()))
        c = x509.load_pem_x509_certificate(cert.read_bytes())
        fp = c.fingerprint(hashes.SHA256()).hex(":").upper()
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key))
        return ctx, fp
    except Exception as ex:  # noqa: BLE001
        return None, str(ex)


# ---------------------------------------------------------------- LAN HTTP API

SETUP_HTML = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Stoeckie Bridge setup</title>
<style>body{font:15px system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 16px;color:#222}
label{display:block;margin:14px 0 4px;font-weight:600}input,textarea,select{width:100%;box-sizing:border-box;padding:8px;
font:13px ui-monospace,monospace}textarea{height:90px}button{margin-top:18px;padding:10px 18px;font-size:15px}
.st{padding:10px;border-radius:6px;background:#f2f2f2;margin:12px 0}h3{margin-top:36px}</style></head><body>
<h2>Stoeckie Bridge</h2><div class="st">{status}</div>
<p>Alles wat je hier invult wordt alleen lokaal in config.json op deze pc opgeslagen.</p>
<h3>AEG / Electrolux</h3>
<p>Maak op <b>developer.electrolux.one</b> (inloggen met je AEG account) een API key aan en genereer
een access token en refresh token.</p>
<form method="post"><input type="hidden" name="section" value="aeg">
<label>API key</label><input name="api_key" autocomplete="off" required>
<label>Access token</label><textarea name="access_token" required></textarea>
<label>Refresh token</label><textarea name="refresh_token" required></textarea>
<button type="submit">AEG opslaan en verbinden</button></form>
<h3>Fujitsu airco (FGLair)</h3>
<p>E-mail en wachtwoord van de FGLair app.</p>
<form method="post"><input type="hidden" name="section" value="fglair">
<label>E-mail</label><input name="username" type="email" autocomplete="off" required>
<label>Wachtwoord</label><input name="password" type="password" autocomplete="off" required>
<label>Regio</label><select name="region"><option value="eu">Europa</option><option value="default">Overig</option>
<option value="cn">China</option></select>
<button type="submit">FGLair opslaan en verbinden</button></form>
<form method="post"><input type="hidden" name="section" value="fglair_settings">
<label>Airco's bijwerken elke (seconden, 30 tot 600)</label>
<input name="poll_seconds" type="number" min="30" max="600" value="{fglair_poll}" required>
<button type="submit">Instelling opslaan</button></form>
<p>Ook in te stellen bij elke airco in de SmartThings app (Instellingen).</p>
<h3>Škoda</h3>
<p>Maak in de MyŠkoda app (8.16 of nieuwer) een API key aan: <b>Instellingen, Smart Home, API-sleutels</b>, en kies je auto.
Het VIN (17 tekens) staat in de app bij de auto en op het kentekenbewijs.</p>
<form method="post"><input type="hidden" name="section" value="skoda">
<label>API key</label><input name="api_key" autocomplete="off" required>
<label>VIN</label><input name="vin" maxlength="17" minlength="17" required>
<button type="submit">Škoda opslaan en verbinden</button></form>
<form method="post"><input type="hidden" name="section" value="skoda_settings">
<label>Auto bijwerken elke (minuten, minimaal 5; Škoda staat 20 opvragingen per uur toe)</label>
<input name="poll_minutes" type="number" min="5" max="60" value="{skoda_poll}" required>
<label>Temperatuur voor airco aan (°C)</label><input name="climate_temp" value="{skoda_temp}" required>
<button type="submit">Instellingen opslaan</button></form>
<p>Deze instellingen staan ook bij de auto in de SmartThings app (Instellingen).</p>
<h3>iRobot (optioneel)</h3>
<p>E-mail en wachtwoord van de iRobot app. De bridge leest alleen de namen en kamers van je favorieten,
twee keer per dag, voor de Roomba driver op de hub. De robot zelf wordt lokaal bediend.</p>
<form method="post"><input type="hidden" name="section" value="irobot">
<label>E-mail</label><input name="username" type="email" autocomplete="off" required>
<label>Wachtwoord</label><input name="password" type="password" autocomplete="off" required>
<label>Land (2 letters)</label><input name="country" value="NL" maxlength="2" required>
<button type="submit">iRobot opslaan en verbinden</button></form>
<h3>Buienradar (optioneel)</h3>
<p>Regen nu en de komende 2 uur bij je huis, met straal. In Nederland en België via Buienradar, elders via Open-Meteo. Ook in te stellen bij het Buienradar apparaat in SmartThings
(aanzetten: instelling Buienradar bij het Stoeckie Bridge apparaat). Laatste wijziging telt.</p>
<form method="post"><input type="hidden" name="section" value="buienradar">
<label>Postcode</label><input name="postcode" value="{rain_pc}" placeholder="1234AB">
<label>Huisnummer</label><input name="housenumber" value="{rain_nr}" placeholder="12">
<label>Of adres (elk land: straat huisnummer, plaats, land)</label><input name="address" value="{rain_addr}" placeholder="Rue de la Loi 16, Brussel, België">
<label>Of breedtegraad, lengtegraad</label><input name="location" value="{rain_loc}" placeholder="52.35, 5.62">
<label>Bron</label><select name="provider"><option value="auto"{rain_p_auto}>Automatisch (Buienradar in NL/BE, anders Open-Meteo)</option>
<option value="buienradar"{rain_p_buienradar}>Buienradar (NL/BE, radar, 5 minuten)</option>
<option value="openmeteo"{rain_p_openmeteo}>Open-Meteo (wereldwijd, 15 minuten)</option></select>
<label>Straal in meters (0 = alleen dit adres, max 25000)</label><input name="radius_m" type="number" min="0" max="25000" value="{rain_radius}">
<label>Regen vanaf (mm/u)</label><input name="threshold" value="{rain_thr}">
<button type="submit">Buienradar opslaan</button></form>
<h3>VELUX ACTIVE vertrekstand (optioneel)</h3>
<p>Voor vergrendelen en ontgrendelen (vertrekstand: alle ramen en zonwering geblokkeerd) vanuit SmartThings.
De ramen zelf blijven lokaal via HomeKit lopen. E-mail en wachtwoord van de VELUX ACTIVE app.</p>
<form method="post"><input type="hidden" name="section" value="velux">
<label>E-mail</label><input name="username" type="email" autocomplete="off" required>
<label>Wachtwoord</label><input name="password" type="password" autocomplete="off" required>
<button type="submit">VELUX opslaan en verbinden</button></form>
<p>Ontgrendelen moet ondertekend worden met een sleutel van de gateway. Eenmalig: vul het IP van de VELUX gateway in,
klik op koppelen, wacht tot het lampje op de gateway knippert en druk dan binnen 3 minuten op de knop van de gateway.</p>
<form method="post"><input type="hidden" name="section" value="velux_pair">
<label>IP van de VELUX gateway</label><input name="gateway_ip" value="{velux_ip}" required>
<button type="submit">Gateway koppelen</button></form>
<form method="post"><input type="hidden" name="section" value="velux_settings">
<label>Vertrekstand en regensensor bijwerken elke (seconden, 30 tot 600)</label>
<input name="poll_seconds" type="number" min="30" max="600" value="{velux_poll}" required>
<button type="submit">Instelling opslaan</button></form>
<p>Ook in te stellen bij VELUX vertrekstand in de SmartThings app (Instellingen).</p></body></html>"""


def make_app(bridge_id: str, on_setup=None, https_port: int | None = None) -> web.Application:
    app = web.Application(middlewares=[])

    def local_only(req) -> bool:
        """Setup page: only from this pc (or STBRIDGE_SETUP_ALLOW), only addressed by IP or localhost (blocks
        DNS rebinding) and never posted from another site (blocks cross-site requests from a browser)."""
        try:
            ip = ipaddress.ip_address(req.remote or "")
        except ValueError:
            return False
        if not (ip.is_loopback or any(ip in net for net in SETUP_ALLOW)):
            return False
        host = (req.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if host != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return False
        origin = req.headers.get("Origin")
        if origin and origin != "null":
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc.lower() != (req.host or "").lower():
                return False
        elif origin == "null":
            return False
        return True

    @web.middleware
    async def guard(req, handler):
        # the LAN API is meant for the hub: refuse anything that is not from a private or local address
        if req.path.startswith("/stbridge/"):
            try:
                ip = ipaddress.ip_address(req.remote or "")
            except ValueError:
                ip = None
            if ip is None or not (ip.is_private or ip.is_loopback or ip.is_link_local):
                return web.json_response({"error": "forbidden"}, status=403)
        resp = await handler(req)
        if req.path.startswith("/setup"):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Frame-Options"] = "DENY"
            resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
        return resp

    def status_text() -> str:
        cfg = load_config()
        e, f = cfg.get("electrolux") or {}, cfg.get("fglair") or {}
        lines = []
        devs = [d for d in REG.devices.values() if d["type"] == "dryer"]
        if not aeg_configured(e):
            lines.append("AEG: nog niet ingesteld.")
        elif devs:
            lines.append("AEG verbonden: " + ", ".join(f"{d['name']} ({d['state'].get('applianceState')})" for d in devs))
        else:
            lines.append("AEG ingesteld, nog geen droger gevonden (zie bridge.log).")
        acs = [d for d in REG.devices.values() if d["type"] == "aircon"]
        if not fglair_configured(f):
            lines.append("FGLair: nog niet ingesteld.")
        elif acs:
            lines.append("FGLair verbonden: " + ", ".join(
                f"{d['name']} ({d['state'].get('mode')}, {d['state'].get('setpoint')} C)" for d in acs))
        else:
            lines.append("FGLair ingesteld, nog geen airco gevonden (zie bridge.log).")
        k = cfg.get("skoda") or {}
        cars = [d for d in REG.devices.values() if d["type"] == "car"]
        if skoda_configured(k):
            if cars:
                lines.append("Škoda verbonden: " + ", ".join(
                    f"{d['name']} ({d['state'].get('battery')}%, sleutel geldig tot {d['state'].get('keyExpires') or '?'})"
                    for d in cars))
            else:
                lines.append("Škoda ingesteld, nog geen gegevens (zie bridge.log).")
        b = cfg.get("buienradar") or {}
        if buienradar_enabled(b):
            rd = REG.devices.get("buienradar")
            lines.append("Buienradar: " + (rd["state"].get("summary", "") if rd else "bezig met ophalen")
                         + f" ({b.get('place') or 'geen locatie'}, straal {b.get('radius_m', 0)} m)")
        v = cfg.get("velux") or {}
        if velux_configured(v):
            locks = [d for d in REG.devices.values() if d["type"] == "velux_lock"]
            if VELUX.get("error"):
                lines.append("VELUX: " + str(VELUX["error"]))
            elif locks:
                st = locks[0]["state"]
                lines.append("VELUX verbonden: " + ("vergrendeld (vertrekstand)" if st.get("locked") else "ontgrendeld")
                             + ("" if st.get("canUnlock") else ", ontgrendelen kan pas na Gateway koppelen"))
            else:
                lines.append("VELUX ingesteld, bezig met verbinden.")
            p = VELUX_PAIR.get("state")
            if p == "press":
                lines.append("VELUX gateway: druk NU op de knop van de gateway (lampje knippert). Ververs deze pagina.")
            elif p == "waiting":
                lines.append("VELUX gateway: koppelen wordt gestart...")
            elif p == "done":
                lines.append("VELUX gateway gekoppeld: ontgrendelen werkt.")
            elif p == "failed":
                lines.append("VELUX koppelen mislukt: " + str(VELUX_PAIR.get("error")))
        r = cfg.get("irobot") or {}
        if irobot_configured(r):
            if IROBOT.get("error"):
                lines.append("iRobot: " + str(IROBOT["error"]))
            elif IROBOT.get("updated"):
                lines.append("iRobot verbonden: " + ", ".join(
                    f"{v.get('name') or k}: {len(v['favorites'])} favoriet(en)" for k, v in IROBOT["robots"].items()))
            else:
                lines.append("iRobot ingesteld, bezig met ophalen.")
        return "<br>".join(html.escape(x) for x in lines)

    def plain_from_lan(req) -> bool:
        """Setup page over plain http from another computer: credentials would cross the LAN unencrypted."""
        try:
            loop = ipaddress.ip_address(req.remote or "").is_loopback
        except ValueError:
            loop = False
        return https_port is not None and not req.secure and not loop

    async def setup_get(req):
        if not local_only(req):
            return web.Response(status=403, text="setup alleen op deze pc (localhost)")
        if plain_from_lan(req):
            host = (req.host or "").rsplit(":", 1)[0]
            raise web.HTTPFound(f"https://{host}:{https_port}/setup")
        k = load_config().get("skoda") or {}
        rb = load_config().get("buienradar") or {}
        page = (SETUP_HTML.replace("{status}", status_text())
                .replace("{skoda_poll}", html.escape(str(k.get("poll_minutes", 10))))
                .replace("{skoda_temp}", html.escape(str(k.get("climate_temp", 21))))
                .replace("{fglair_poll}", html.escape(str((load_config().get("fglair") or {}).get("poll_seconds", 60))))
                .replace("{velux_poll}", html.escape(str((load_config().get("velux") or {}).get("poll_seconds", 60))))
                .replace("{rain_pc}", html.escape(str(rb.get("postcode") or "")))
                .replace("{rain_nr}", html.escape(str(rb.get("housenumber") or "")))
                .replace("{rain_loc}", html.escape(str(rb.get("location") or "")))
                .replace("{rain_addr}", html.escape(str(rb.get("address") or "")))
                .replace("{rain_p_auto}", " selected" if rb.get("provider", "auto") == "auto" else "")
                .replace("{rain_p_buienradar}", " selected" if rb.get("provider") == "buienradar" else "")
                .replace("{rain_p_openmeteo}", " selected" if rb.get("provider") == "openmeteo" else "")
                .replace("{rain_radius}", html.escape(str(rb.get("radius_m", int(float(rb.get("radius_km") or 0) * 1000)))))
                .replace("{rain_thr}", html.escape(str(rb.get("threshold", RAIN_THRESHOLD))))
                .replace("{velux_ip}", html.escape(str((load_config().get("velux") or {}).get("gateway_ip", "")))))
        return web.Response(text=page, content_type="text/html")

    async def setup_post(req):
        if not local_only(req):
            return web.Response(status=403, text="setup alleen op deze pc (localhost)")
        if plain_from_lan(req):
            return web.Response(status=403, text="gebruik https voor de setup pagina")
        form = await req.post()
        cfg = load_config()
        section = str(form.get("section", "aeg"))
        if section == "skoda":
            k = cfg.setdefault("skoda", {})
            k["api_key"] = "".join(str(form.get("api_key", "")).split())
            k["vin"] = "".join(str(form.get("vin", "")).split()).upper()
            save_config(cfg)
            log.info("[setup] Škoda key saved")
        elif section == "skoda_settings":
            k = cfg.setdefault("skoda", {})
            k.update(skoda_settings(form.get("poll_minutes"), form.get("climate_temp"), k))
            save_config(cfg)
            log.info("[setup] Škoda settings: every %s min, climate %s C", k["poll_minutes"], k["climate_temp"])
            section = "skoda"
        elif section == "irobot":
            r = cfg.setdefault("irobot", {})
            r["username"] = str(form.get("username", "")).strip()
            r["password"] = str(form.get("password", ""))
            r["country"] = str(form.get("country", "NL")).strip().upper()[:2] or "NL"
            save_config(cfg)
            log.info("[setup] iRobot login saved")
        elif section in ("fglair_settings", "velux_settings"):
            name = section.split("_")[0]
            sec = poll_seconds(form.get("poll_seconds"), 60)
            save_poll_seconds(name, sec)
            log.info("[setup] %s: every %s s", name, sec)
            # the running source picks it up at its next update; restart it so it applies now
            section = name
        elif section == "buienradar":
            b = cfg.setdefault("buienradar", {})
            v = {k: form.get(k, "") for k in ("postcode", "housenumber", "address", "location", "radius_m", "threshold",
                                              "provider")}
            new = buienradar_settings(v, b)
            if not (new.get("location") or (new.get("postcode") and new.get("housenumber")) or new.get("address")):
                return web.Response(status=400, text="vul postcode en huisnummer, een adres of breedtegraad, lengtegraad in")
            b.update(new)
            b["enabled"] = True
            b.pop("radius_km", None)
            save_config(cfg)
            log.info("[setup] Buienradar: %s %s / %s, radius %s m", b.get("postcode"), b.get("housenumber"),
                     b.get("location"), b.get("radius_m"))
        elif section == "velux":
            v = cfg.setdefault("velux", {})
            v["username"] = str(form.get("username", "")).strip()
            v["password"] = str(form.get("password", ""))
            v.pop("tokens", None)
            save_config(cfg)
            VELUX["error"] = None
            log.info("[setup] VELUX login saved")
        elif section == "velux_pair":
            ip = str(form.get("gateway_ip", "")).strip()
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return web.Response(status=400, text="geen geldig IP adres")
            src = VELUX.get("source")
            if src is None or src.session is None:
                return web.Response(status=409, text="Stel eerst de VELUX login in (en wacht tot hij verbonden is).")
            if VELUX_PAIR.get("state") not in ("waiting", "press"):
                asyncio.get_running_loop().create_task(src.pair(ip))
            await asyncio.sleep(3)
            raise web.HTTPFound("/setup")
        elif section == "fglair":
            f = cfg.setdefault("fglair", {})
            f["username"] = str(form.get("username", "")).strip()
            f["password"] = str(form.get("password", ""))
            f["region"] = str(form.get("region", "eu"))
            save_config(cfg)
            log.info("[setup] FGLair login saved")
        else:
            e = cfg.setdefault("electrolux", {})
            for k in ("api_key", "access_token", "refresh_token"):
                e[k] = "".join(str(form.get(k, "")).split())
            save_config(cfg)
            log.info("[setup] AEG credentials saved")
        if on_setup:
            on_setup(section)
        await asyncio.sleep(4)
        raise web.HTTPFound("/setup")

    async def info(_req):
        return web.json_response({"service": "stoeckie-bridge", "id": bridge_id, "version": VERSION,
                                  "name": socket.gethostname()})

    async def devices(_req):
        return web.json_response({"devices": list(REG.devices.values()), "time": int(time.time())})

    async def irobot(req):
        blid = req.match_info["blid"]
        if req.query.get("refresh") and IROBOT.get("refresh"):
            IROBOT["refresh"].set()
        robot = IROBOT["robots"].get(blid)
        if robot is None:
            return web.json_response({"error": "unknown robot or iRobot not set up", "updated": IROBOT["updated"]},
                                     status=404)
        return web.json_response({"name": robot["name"], "favorites": robot["favorites"],
                                  "updated": IROBOT["updated"]})

    async def buienradar_set(req):
        """Switch the rain radar on/off from the SmartThings bridge device ({"enabled": true}); optional settings."""
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
        cfg = load_config()
        b = cfg.setdefault("buienradar", {})
        changed = False
        if "enabled" in body and bool(body["enabled"]) != bool(b.get("enabled")):
            b["enabled"] = bool(body["enabled"])
            changed = True
        settings = {k: body[k] for k in ("postcode", "housenumber", "address", "location", "radius_m", "threshold",
                                         "provider") if k in body}
        if settings:
            new = buienradar_settings(settings, b)
            if any(b.get(k) != new.get(k) for k in new):
                b.update(new)
                b.pop("radius_km", None)
                changed = True
        if changed:
            if not b.get("enabled"):
                b.pop("lat", None)  # off means off, also when a location was set before
                b.pop("lon", None)
            save_config(cfg)
            log.info("[buienradar] %s from SmartThings", "on" if b.get("enabled") else "off")
            if on_setup:
                on_setup("buienradar")
            if not b.get("enabled"):
                REG.devices.pop("buienradar", None)
        return web.json_response({"ok": True, "enabled": bool(b.get("enabled"))})

    async def command(req):
        dev_id = req.match_info["dev_id"]
        handler = REG.handlers.get(dev_id)
        if not handler:
            return web.json_response({"error": "unknown device"}, status=404)
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
        try:
            await handler(str(body.get("cmd", "")), body.get("value"))
        except Exception as ex:  # noqa: BLE001
            log.error("command %s on %s failed: %s", body, dev_id, ex)
            return web.json_response({"error": str(ex)}, status=500)
        return web.json_response({"ok": True, "device": REG.devices.get(dev_id)})

    app.middlewares.append(guard)
    app.router.add_get("/setup", setup_get)
    app.router.add_post("/setup", setup_post)
    async def root(_req):
        raise web.HTTPFound("/setup")

    app.router.add_get("/", root)
    app.router.add_get("/stbridge/info", info)
    app.router.add_get("/stbridge/devices", devices)
    app.router.add_post("/stbridge/devices/{dev_id}/command", command)
    app.router.add_get("/stbridge/irobot/{blid}", irobot)
    app.router.add_post("/stbridge/buienradar", buienradar_set)
    return app


async def demo_devices() -> None:
    """Fake devices for testing the Edge driver without real hardware (--demo)."""
    pos = {"v": 0}

    async def win(cmd, value):
        pos["v"] = {"open": 100, "close": 0}.get(cmd, int(value or 0) if cmd == "level" else pos["v"])
        REG.upsert("demo-window", "window", "Demo dakraam", {"position": pos["v"], "target": pos["v"],
                                                             "positionState": "stopped"})

    async def dry(cmd, value):
        pass

    ac = {"power": "on", "mode": "heat", "setpoint": 21.0, "temperature": 19.5, "outdoorTemperature": 8.0,
          "fan": "auto", "swing": "off", "supportedModes": ["auto", "cool", "dry", "fan", "heat"],
          "supportedFans": ["quiet", "low", "medium", "high", "auto"],
          "supportedSwings": ["off", "vertical"], "setpointRange": [16.0, 30.0], "model": "DEMO"}

    async def aircon(cmd, value):
        if cmd == "on":
            ac["power"] = "on"
            ac["mode"] = ac.get("last_mode", "heat")
        elif cmd == "off":
            ac["last_mode"], ac["power"], ac["mode"] = ac["mode"], "off", "off"
        elif cmd == "mode":
            ac["power"], ac["mode"] = "on", str(value)
        elif cmd == "setpoint":
            ac["setpoint"] = float(value)
        elif cmd in ("fan", "swing"):
            ac[cmd] = str(value)
        REG.upsert("demo-aircon", "aircon", "Demo airco", ac, True, aircon)

    await win("refresh", None)
    REG.handlers["demo-window"] = win
    await aircon("refresh", None)
    car = {"battery": 64, "electricRange": 38, "totalRange": 610, "charging": "READY_FOR_CHARGING",
           "chargePowerKw": 0, "plug": "CONNECTED", "atHome": True, "doors": "CLOSED", "windows": "CLOSED",
           "locked": "YES", "climate": "OFF", "odometer": 41234, "targetSoc": 100}

    async def demo_car(cmd, value):
        if cmd == "on":
            car.update(charging="CHARGING", chargePowerKw=3.6)
        elif cmd == "off":
            car.update(charging="READY_FOR_CHARGING", chargePowerKw=0)
        elif cmd == "climate_on":
            car["climate"] = "ON"
        elif cmd == "climate_off":
            car["climate"] = "OFF"
        REG.upsert("demo-car", "car", "Demo auto", car, True, demo_car)

    await demo_car("refresh", None)
    velux = {"locked": True, "canUnlock": True}
    REG.upsert("demo-rain", "rain", "Demo regensensor", {"raining": False}, True, dry)

    async def demo_velux(cmd, value):
        if cmd in ("lock", "unlock"):
            velux["locked"] = cmd == "lock"
        REG.upsert("demo-velux", "velux_lock", "Demo VELUX vertrekstand", velux, True, demo_velux)

    await demo_velux("refresh", None)
    rain = rain_summary([0.0] * 4 + [0.5, 1.2, 0.8] + [0.0] * 17)
    REG.upsert("demo-rainradar", "rainradar", "Demo Buienradar", rain, True, dry)
    t0 = time.time()
    while True:
        el = int(time.time() - t0) % 180
        reported = {"applianceState": "RUNNING" if el < 120 else "END_OF_CYCLE",
                    "cyclePhase": "DRY" if el < 90 else ("COOL" if el < 120 else "UNAVAILABLE"),
                    "timeToEnd": max(0, 120 - el), "userSelections": {"programUID": "COTTON_PR_COTTONSECO"},
                    "doorState": "CLOSED", "remoteControl": "ENABLED"}
        REG.upsert("demo-dryer", "dryer", "Demo droger", dryer_state(reported, "connected"), True, dry)
        await asyncio.sleep(5)


def seed_from_env(cfg: dict[str, Any]) -> None:
    """First start in Docker: take credentials from env vars. Afterwards config.json (with refreshed tokens) wins."""
    e = cfg.get("electrolux") or {}
    env = {k: os.environ.get("ELECTROLUX_" + k.upper()) for k in ("api_key", "access_token", "refresh_token")}
    if all(env.values()) and not aeg_configured(e):
        cfg.setdefault("electrolux", {}).update(env)
        save_config(cfg)
        log.info("[setup] AEG credentials taken from environment")
    f = cfg.get("fglair") or {}
    user, pw = os.environ.get("FGLAIR_USERNAME"), os.environ.get("FGLAIR_PASSWORD")
    if user and pw and not fglair_configured(f):
        cfg.setdefault("fglair", {}).update({"username": user, "password": pw,
                                             "region": os.environ.get("FGLAIR_REGION", "eu")})
        save_config(cfg)
        log.info("[setup] FGLair login taken from environment")


async def serve(args) -> None:
    cfg = load_config()
    seed_from_env(cfg)
    bridge_id = cfg.get("bridge_id")
    if not bridge_id:
        bridge_id = cfg["bridge_id"] = uuid.uuid4().hex[:12]
        save_config(cfg)
    port = int(cfg.get("port", 8787))
    sources = {"aeg": Electrolux, "fglair": FGLair, "skoda": Skoda, "irobot": IRobotCloud, "velux": VeluxCloud,
               "buienradar": Buienradar}
    tasks: dict[str, asyncio.Task] = {}

    def start(section: str) -> None:
        if section not in sources:
            return
        if tasks.get(section):
            tasks[section].cancel()
        for dev_id in [d for d in REG.devices if d.startswith(section + "-")]:
            REG.devices.pop(dev_id, None)
            REG.handlers.pop(dev_id, None)
        tasks[section] = asyncio.create_task(guarded(section, sources[section](load_config()).run))

    https_port = int(cfg.get("https_port", 8443))
    tls, fp = (None, "demo") if args.demo else setup_tls(DATA)
    runner = web.AppRunner(make_app(bridge_id, None if args.demo else start, https_port if tls else None),
                           access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Stoeckie Bridge %s listening on port %d (id %s)", VERSION, port, bridge_id)
    if tls:
        await web.TCPSite(runner, "0.0.0.0", https_port, ssl_context=tls).start()
        log.info("[https] setup page on https://<this pc>:%d/setup, certificate SHA-256 %s", https_port, fp)
    elif not args.demo:
        log.warning("[https] not available (%s); setup page only on http", fp)

    async def guarded(name, coro):
        while True:
            try:
                await coro()
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("[%s] crashed, restart in 60 s", name)
                await asyncio.sleep(60)

    if args.demo:
        asyncio.create_task(demo_devices())
    else:
        for section in sources:
            start(section)
    await asyncio.Event().wait()


# ---------------------------------------------------------------- CLI

async def check_fglair() -> None:
    """Quick test of the FGLair login in config.json."""
    cfg = load_config()
    if not fglair_configured(cfg.get("fglair") or {}):
        print("FGLair: nog niet ingesteld.")
        return
    import aiohttp
    fg = FGLair(cfg)
    async with aiohttp.ClientSession() as session:
        fg.api = fg._new_api(session)
        await fg.api.async_sign_in()
        await fg._sync()
    print(f"FGLair OK, {len(fg.devices)} airco('s):")
    for d in REG.devices.values():
        if d["type"] == "aircon":
            s = d["state"]
            print(f"  {d['name']} model={s.get('model')} online={d['online']} stand={s.get('mode')} "
                  f"setpoint={s.get('setpoint')} binnen={s.get('temperature')} buiten={s.get('outdoorTemperature')} "
                  f"fan={s.get('fan')} modes={s.get('supportedModes')}")


async def check_all() -> None:
    for name, fn in (("AEG", check_aeg), ("FGLair", check_fglair)):
        try:
            await fn()
        except Exception as ex:  # noqa: BLE001
            print(f"{name} FOUT: {ex}")


async def check_aeg() -> None:
    """Quick test of the Electrolux credentials in config.json."""
    e = (load_config().get("electrolux") or {})
    if not aeg_configured(e):
        print("AEG: nog niet ingesteld.")
        return
    from electrolux_group_developer_sdk.auth.token_manager import TokenManager
    from electrolux_group_developer_sdk.client.appliance_client import ApplianceClient
    cfg = load_config()
    holder = Electrolux(cfg)
    tm = TokenManager(e["access_token"], e["refresh_token"], e["api_key"], on_token_update=holder._on_tokens)
    client = ApplianceClient(tm, external_user_agent=f"StoeckieBridge/{VERSION}")
    apps = await client.get_appliances()
    print(f"AEG OK, {len(apps)} apparaat/apparaten:")
    for a in apps:
        st = await client.get_appliance_state(a.applianceId)
        rep = (st.properties or {}).get("reported", {}) or {}
        print(f"  {a.applianceName} type={a.applianceType} verbinding={st.connectionState} "
              f"status={rep.get('applianceState')} fase={rep.get('cyclePhase')} resttijd={rep.get('timeToEnd')}")


def setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None:  # pythonw has no console
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("aiohttp.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    p = argparse.ArgumentParser(description="Stoeckie Bridge")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--demo", action="store_true", help="fake devices, for testing the driver")
    p.add_argument("--check", action="store_true", help="test the AEG and FGLair logins, list appliances and exit")
    args = p.parse_args()
    setup_logging(args.verbose)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.check:
        asyncio.run(check_all())
    else:
        asyncio.run(serve(args))


if __name__ == "__main__":
    main()
