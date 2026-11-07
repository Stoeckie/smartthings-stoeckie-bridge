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
  * Home Connect (optional, local): Bosch / Siemens / Neff dishwashers, ovens and more over their own local
    websocket (library homeconnect-websocket, Python 3.13+). Keys and descriptions come once from the Home Connect
    cloud with the Home Connect Profile Downloader; put its zips in the homeconnect folder next to config.json.
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
      dishwasher / oven (Home Connect): on | off | stop | pause | resume (oven) | refresh
      rainradar: refresh | settings {postcode, housenumber, address, location, radius_m, threshold, provider}
               (state: raining, rainRate mm/h, minutesUntilRain, minutesOfRain, intensity, summary)
  POST /stbridge/buienradar               body {"enabled": true} (from the bridge device)
  GET  /stbridge/irobot/<blid>[?refresh=1] -> {"name", "favorites": [{name, id, command, params}], "updated"}
  POST /stbridge/srp                      body {"nonce", "box"} (signed and encrypted, paired hubs only)
      One SRP step of a HomeKit pairing for an Edge driver on the hub (e.g. Somfy (HomeKit local)): the hub
      needs minutes for it, some accessories give up sooner. The bridge only computes; it never talks to the
      accessory and keeps nothing.

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
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import logging
import logging.handlers
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

VERSION = "2026-11-07d"
HERE = Path(__file__).resolve().parent
# Data folder: next to the script on Windows, /data in the Docker image (STBRIDGE_DATA)
DATA = Path(os.environ.get("STBRIDGE_DATA") or HERE)
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA / "config.json"
LOG_FILE = DATA / "bridge.log"
# Extra networks allowed to open the setup page, e.g. "192.168.2.0/24" (Docker without host networking)
SETUP_ALLOW = [ipaddress.ip_network(n.strip(), strict=False)
               for n in os.environ.get("STBRIDGE_SETUP_ALLOW", "").split(",") if n.strip()]


def in_container() -> bool:
    return (os.environ.get("STBRIDGE_IN_DOCKER") == "1" or os.path.exists("/.dockerenv")
            or os.path.exists("/run/.containerenv"))


def container_gateway(route_file: str = "/proc/net/route", sys_net: str = "/sys/class/net"):
    """In Docker with a bridge network (port mapping, Docker Desktop), a browser on the Docker host reaches the
    container from the network's gateway, not from 127.0.0.1. Return that gateway so the host can open the setup
    page. Only when the default route goes over a virtual (veth) interface: with host networking the default
    gateway is the real LAN router, which must not be allowed. LAN clients keep their own source address and still
    need STBRIDGE_SETUP_ALLOW."""
    try:
        with open(route_file, encoding="ascii") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) < 3 or f[1] != "00000000":
                    continue
                iface, gw_hex = f[0], f[2]
                ifindex = Path(sys_net, iface, "ifindex").read_text().strip()
                iflink = Path(sys_net, iface, "iflink").read_text().strip()
                if ifindex == iflink:  # a real interface: host networking
                    return None
                gw = ipaddress.ip_address(int(gw_hex, 16).to_bytes(4, "little"))
                return gw if gw.is_private else None
    except (OSError, ValueError, StopIteration):
        return None
    return None


SETUP_GATEWAY = (container_gateway() if in_container() and os.environ.get("STBRIDGE_SETUP_ALLOW_GATEWAY", "1") != "0"
                 else None)


def setup_source_allowed(ip) -> bool:
    return ip.is_loopback or ip == SETUP_GATEWAY or any(ip in net for net in SETUP_ALLOW)

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
    has_min = min_heat_writable(dev)
    min_on = has_min and bool(safe(lambda: dev.get_property_value(MIN_HEAT_PROP), 0))
    modes = [m for m in (_name(x) for x in safe(lambda: dev.supported_op_modes, [])) if m not in ("off", "on")]
    if has_min:
        modes.append("minheat")  # the FGLair app shows Minimum Heat as an operation mode
    if min_on:
        mode = "minheat"
    vnum = vdir_count(dev)
    vdir = safe(lambda: int(dev.get_property_value(VDIR_PROP)), None) if vnum else None
    return {
        "power": "off" if mode == "off" else "on",
        "mode": mode,                                   # off on auto cool dry fan heat minheat
        "setpoint": safe(lambda: dev.set_temp),
        "temperature": safe(lambda: dev.sensed_temp),   # None on models without a room sensor
        "outdoorTemperature": safe(lambda: dev.outdoor_temperature),
        "fan": safe(lambda: _name(dev.fan_speed)),
        "swing": safe(lambda: SWING_NAMES.get(dev.swing_mode.name, "off"), "off"),
        "supportedModes": modes,
        "supportedFans": [_name(x) for x in safe(lambda: dev.supported_fan_speeds, [])],
        "supportedSwings": [SWING_NAMES.get(x.name, "off") for x in safe(lambda: dev.supported_swing_modes, [])],
        "setpointRange": [lo, hi],
        "model": getattr(dev, "oem_model_number", None),
        # Minimum Heat (frost protection, about 10 C): a separate min_heat property in the cloud, shown as
        # mode "minheat" above. None when the unit has no such property.
        "minHeat": min_on if has_min else None,
        # louver position 1..vDirCount when swing is off (None: unit has no positions)
        "vDir": vdir if vdir and 1 <= vdir <= vnum else None,
        "vDirCount": vnum or None,
    }


# Property name used by the FGLair cloud for Minimum Heat. Not in ayla-iot-unofficial (it only has the
# capability bit OP_MIN_HEAT); the bridge logs every unit's property names once so this can be checked.
MIN_HEAT_PROP = "min_heat"
# Vertical airflow direction (louver position when swing is off): position 1..count, written as raw properties
# because the library has no setter for it.
VDIR_PROP, VNUM_PROP, VSWING_PROP = "af_vertical_direction", "af_vertical_num_dir", "af_vertical_swing"
VDIR_MAX = 8


def min_heat_writable(dev) -> bool:
    """True when the unit has a Minimum Heat property that can be switched (some units only report it)."""
    try:
        prop = dev.properties_full.get(MIN_HEAT_PROP)
    except Exception:  # noqa: BLE001
        return False
    return bool(prop) and not prop.get("read_only")


def vdir_count(dev) -> int:
    """Number of louver positions the unit reports, 0 if it has none."""
    try:
        props = dev.properties_full
        if VDIR_PROP not in props or VNUM_PROP not in props:
            return 0
        return max(0, min(VDIR_MAX, int(dev.get_property_value(VNUM_PROP) or 0)))
    except Exception:  # noqa: BLE001
        return 0


def describe_unit(dev) -> str:
    """One log line per unit: property names and the min heat capability bit (no values)."""
    try:
        names = sorted(str(k) for k in dev.properties_full.keys())
    except Exception:  # noqa: BLE001
        names = []
    try:
        from ayla_iot_unofficial.fujitsu_consts import Capability
        cap = "yes" if dev.has_capability(Capability.OP_MIN_HEAT) else "no"
    except Exception:  # noqa: BLE001
        cap = "?"
    try:
        mh = dev.properties_full.get(MIN_HEAT_PROP)
        mh_txt = "none" if not mh else ("read only" if mh.get("read_only") else "writable")
    except Exception:  # noqa: BLE001
        mh_txt = "?"
    return f"min heat capability {cap}, min_heat property {mh_txt}, properties: {', '.join(names)}"


class FGLair:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.f = cfg.get("fglair") or {}
        self.api = None
        self.devices: dict[str, Any] = {}  # dsn -> FujitsuHVAC
        self.skipped: set[str] = set()
        self.lock = asyncio.Lock()
        self.last_sense: dict[str, float] = {}  # dsn -> time of the last "refresh" write to the adapter
        self.described: set[str] = set()         # units whose property names are logged already

    # The library writes "refresh" = 1 to the adapter on every update so it reads the room temperature again
    # from the indoor unit. Every minute that keeps the adapter busy (the FGLair app then shows "information
    # acquisition not completed"), so the bridge only does that every REFRESH_EVERY seconds and otherwise just
    # reads the cloud values.
    REFRESH_EVERY = 900

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
            name = getattr(dev, "name", None) or "Air conditioner"
        REG.upsert("fglair-" + dsn, "aircon", name, aircon_state(dev), dev.is_online(), self._command(dsn))

    async def _refresh(self, dsn: str) -> None:
        dev = self.devices[dsn]
        if dev.is_online():
            from ayla_iot_unofficial.fujitsu_hvac import FujitsuHVAC
            await super(FujitsuHVAC, dev).async_update()  # read only, no write to the adapter
            if dsn not in self.described:
                self.described.add(dsn)
                log.info("[fglair] unit %s: %s", dsn, describe_unit(dev))
            if time.time() - self.last_sense.get(dsn, 0) >= self.REFRESH_EVERY:
                self.last_sense[dsn] = time.time()
                await dev.refresh_sensed_temp()
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

    @staticmethod
    async def _min_heat(dev, on: bool, strict: bool = False) -> None:
        """Switch Minimum Heat; when switching it off, only write if it is on (other units: no-op)."""
        if not min_heat_writable(dev):
            if on or strict:
                raise ValueError("this unit has no Minimum Heat that can be switched from the cloud")
            return
        if not on and not dev.get_property_value(MIN_HEAT_PROP):
            return
        await dev.async_set_property_value(MIN_HEAT_PROP, 1 if on else 0)

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
                    await self._min_heat(dev, False)
                    await dev.async_set_op_mode(OpMode.OFF)
                elif cmd == "mode" and str(value).lower() == "minheat":
                    await self._min_heat(dev, True)
                elif cmd == "mode":
                    await self._min_heat(dev, False)  # another mode ends Minimum Heat
                    await dev.async_set_op_mode(OpMode[str(value).upper()])
                elif cmd == "setpoint":
                    await dev.async_set_set_temp(float(value))
                elif cmd == "fan":
                    await dev.async_set_fan_speed(FanSpeed[str(value).upper()])
                elif cmd == "vdir":
                    n, count = int(value), vdir_count(dev)
                    if not 1 <= n <= count:
                        raise ValueError(f"airflow direction must be 1..{count}")
                    if dev.get_property_value(VSWING_PROP):  # a fixed position ends vertical swing
                        await dev.async_set_property_value(VSWING_PROP, 0)
                    await dev.async_set_property_value(VDIR_PROP, n)
                elif cmd == "minheat":  # older drivers (2026-11-06b) used a separate switch
                    await self._min_heat(dev, str(value).lower() in ("on", "1", "true"), strict=True)
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

# --- HomeKit pair-setup SRP helper ------------------------------------------------------------------------
# SRP-6a (RFC 5054 3072-bit group, SHA-512) with the HomeKit conventions, same as srp.lua in the Edge drivers.
SRP_N = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
    "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE6"
    "49286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3BE39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7D"
    "B3970F85A6E1E4C7ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF", 16)
SRP_G = 5
_SRP_LAST = {"t": 0.0}


def srp_homekit_client(salt: bytes, b_pub: bytes, pin: str) -> tuple[bytes, bytes, bytes]:
    """Client side of HomeKit pair-setup M3: returns (A, M1 proof, session key K)."""
    def h(*parts: bytes) -> bytes:
        return hashlib.sha512(b"".join(parts)).digest()

    def pad(v: int) -> bytes:
        return v.to_bytes(384, "big")

    n_bytes = pad(SRP_N)
    big_b = int.from_bytes(b_pub, "big")
    if big_b % SRP_N == 0:
        raise ValueError("invalid accessory key")
    k = int.from_bytes(h(n_bytes, pad(SRP_G)), "big")
    x = int.from_bytes(h(salt, h(b"Pair-Setup:" + pin.encode())), "big")
    a = int.from_bytes(secrets.token_bytes(32), "big")
    big_a = pow(SRP_G, a, SRP_N)
    u = int.from_bytes(h(pad(big_a), pad(big_b)), "big")
    if u == 0:
        raise ValueError("invalid accessory key")
    base = (big_b - k * pow(SRP_G, x, SRP_N)) % SRP_N
    key = h(pad(pow(base, a + u * x, SRP_N)))
    group = bytes(p ^ q for p, q in zip(h(n_bytes), h(bytes([SRP_G]))))
    proof = h(group, h(b"Pair-Setup"), salt, pad(big_a), pad(big_b), key)
    return pad(big_a), proof, key


def srp_box_key(token: str, direction: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA512(), length=32, salt=b"stbridge-srp",
                info=direction.encode()).derive(token.encode())


# --- LAN API authentication -------------------------------------------------------------------------
# After pairing, every request from the hub carries an HMAC-SHA512 signature made with a shared token
# (X-Stbridge-Time, X-Stbridge-Sig) and every answer is signed back. The token itself never crosses the
# LAN again after pairing, a replayed request is refused, and a device pretending to be the bridge cannot
# produce a valid answer. Until a hub is paired the API stays open (old drivers keep working).
PAIRING: dict[str, Any] = {"until": 0.0}
PAIR_WINDOW = 300          # seconds the "Pair" button keeps pairing open
SIG_MAX_AGE = 300          # seconds a signed request stays valid (hub and pc clocks use NTP)
_SEEN_SIGS: dict[str, float] = {}


def api_token(cfg: dict[str, Any] | None = None) -> str:
    """The shared secret for hub requests; created on first use and kept in config.json."""
    cfg = cfg if cfg is not None else load_config()
    tok = cfg.get("api_token")
    if not tok:
        tok = cfg["api_token"] = secrets.token_hex(32)
        save_config(cfg)
    return tok


def api_paired(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("api_paired") and cfg.get("api_token"))


def sign(token: str, msg: bytes | str) -> str:
    return hmac.new(token.encode(), msg if isinstance(msg, bytes) else msg.encode(), hashlib.sha512).hexdigest()


def request_message(method: str, path: str, ts: str, body: bytes) -> bytes:
    return f"{method}\n{path}\n{ts}\n".encode() + body


def check_request_sig(token: str, method: str, path: str, ts: str, sig: str, body: bytes,
                      now: float | None = None) -> str | None:
    """None when the signature is valid and fresh, else the reason."""
    now = time.time() if now is None else now
    try:
        age = abs(now - float(ts))
    except (TypeError, ValueError):
        return "bad time"
    if age > SIG_MAX_AGE:
        return "request too old (check the clock)"
    if not hmac.compare_digest(sign(token, request_message(method, path, ts, body)), str(sig).lower()):
        return "bad signature"
    for k in [k for k, t in _SEEN_SIGS.items() if now - t > SIG_MAX_AGE * 2]:
        del _SEEN_SIGS[k]
    if sig in _SEEN_SIGS:
        return "replayed request"
    _SEEN_SIGS[sig] = now
    return None


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
        REG.upsert("velux-" + self.home["gateway"], "velux_lock", "VELUX departure mode", state, True, self._command())
        if "is_raining" in gw:  # the gateway's rain sensor as its own device
            REG.upsert("velux-rain-" + self.home["gateway"], "rain", "VELUX rain sensor",
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
                    raise PermissionError("unlock needs the gateway key: press 'Pair gateway' on the setup page")
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


def rain_summary(rates: list[float], threshold: float = RAIN_THRESHOLD, lang: str = "en") -> dict[str, Any]:
    """Now, minutes until rain, how long it rains, intensity class and a short summary (English)."""
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
    # Always English: device texts are not translated by the SmartThings app, so one language for everyone.
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
            self.error = f"Address not found: {what}"
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
            summary = self.error or "No location: enter your address or coordinates in the device settings"
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
        state = rain_summary(rates, float(self.b.get("threshold") or RAIN_THRESHOLD))
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


# ---------------------------------------------------------------- Home Connect (Bosch / Siemens / Neff / Gaggenau), local

HC_DIR = DATA / "homeconnect"
HC_TYPES = {"Dishwasher": "dishwasher", "Oven": "oven", "Washer": "washer", "Dryer": "hcdryer",
            "WasherDryer": "washer", "CoffeeMaker": "coffee", "Refrigerator": "fridge", "FridgeFreezer": "fridge"}
HC_OPERATION = {"Inactive": "inactive", "Ready": "ready", "DelayedStart": "delayedStart", "Run": "run",
                "Pause": "pause", "ActionRequired": "actionRequired", "Finished": "finished", "Error": "error",
                "Aborting": "aborting"}
# Home Connect program name -> key of the SmartThings capability silvervalley08660.applianceProgram (labels EN/NL in
# tools/presentation/gen_homeconnect.py). Programs not listed here are not offered in SmartThings.
_HC_DW = "Dishcare.Dishwasher.Program."
_HC_OV = "Cooking.Oven.Program."
HC_PROGRAMS = {
    _HC_DW + "Eco50": "eco50", _HC_DW + "Auto1": "auto1", _HC_DW + "Auto2": "auto2", _HC_DW + "Auto3": "auto3",
    _HC_DW + "Intensiv70": "intensive70", _HC_DW + "Intensiv45": "intensive45", _HC_DW + "Quick45": "quick45",
    _HC_DW + "Quick65": "quick65", _HC_DW + "Super60": "super60", _HC_DW + "Glas40": "glass40",
    _HC_DW + "NightWash": "night", _HC_DW + "MachineCare": "machineCare", _HC_DW + "PreRinse": "preRinse",
    "BSH.Common.Program.Favorite.001": "favorite",
    _HC_OV + "HeatingMode.HotAir": "hotAir", _HC_OV + "HeatingMode.HotAirEco": "hotAirEco",
    _HC_OV + "HeatingMode.TopBottomHeating": "topBottom", _HC_OV + "HeatingMode.TopBottomHeatingEco": "topBottomEco",
    _HC_OV + "HeatingMode.HotAirGrilling": "hotAirGrill", _HC_OV + "HeatingMode.PizzaSetting": "pizza",
    _HC_OV + "HeatingMode.SlowCook": "slowCook", _HC_OV + "HeatingMode.BottomHeating": "bottomHeat",
    _HC_OV + "HeatingMode.KeepWarm": "keepWarm", _HC_OV + "HeatingMode.PreheatOvenware": "preheatOvenware",
    _HC_OV + "HeatingMode.FrozenHeatupSpecial": "frozenFood", _HC_OV + "HeatingMode.GrillLargeArea": "grillLarge",
    _HC_OV + "HeatingMode.GrillSmallArea": "grillSmall", _HC_OV + "HeatingMode.SabbathProgramme": "sabbath",
    _HC_OV + "MicroCombiModes.HotAir": "microHotAir", _HC_OV + "MicroCombiModes.TopBottomHeating": "microTopBottom",
    _HC_OV + "MicroCombiModes.HotAirGrilling": "microHotAirGrill",
    _HC_OV + "MicroCombiModes.GrillLargeArea": "microGrillLarge",
    _HC_OV + "MicroCombiModes.GrillSmallArea": "microGrillSmall",
    _HC_OV + "Microwave.90Watt": "micro90", _HC_OV + "Microwave.180Watt": "micro180",
    _HC_OV + "Microwave.360Watt": "micro360", _HC_OV + "Microwave.600Watt": "micro600",
    _HC_OV + "Microwave.900Watt": "micro900", _HC_OV + "Microwave.Max": "microMax",
    _HC_OV + "Cleaning.Drying": "drying",
}
HC_PROGRAM_NAME = {v: k for k, v in HC_PROGRAMS.items()}
HC_DEFAULT = {"dishwasher": {"program": "eco50"}, "oven": {"program": "hotAir", "setpoint": 180, "duration": 0}}
HC_SETPOINT = "Cooking.Oven.Option.SetpointTemperature"
HC_DURATION = "BSH.Common.Option.Duration"


def homeconnect_profiles(folder: Path = HC_DIR) -> list[dict[str, Any]]:
    """Profiles from the Home Connect Profile Downloader ("Home Assistant - Home Connect Local" zips) in folder.
    Each: {haId, type, brand, model, connectionType, key, iv, xml: (description, feature mapping)}.
    The zips hold the appliance keys: they stay in this folder (same protection as config.json)."""
    import zipfile
    out = []
    if not folder.exists():
        return out
    for z in sorted(folder.glob("*.zip")):
        try:
            with zipfile.ZipFile(z) as zf:
                for n in zf.namelist():
                    if not n.endswith(".json"):
                        continue
                    info = json.loads(zf.read(n))
                    if not info.get("key") or not info.get("haId"):
                        continue
                    info["xml"] = (zf.read(info["deviceDescriptionFileName"]).decode("utf-8"),
                                   zf.read(info["featureMappingFileName"]).decode("utf-8"))
                    out.append(info)
        except Exception as ex:  # noqa: BLE001
            log.warning("[homeconnect] %s: not a usable profile (%s)", z.name, ex)
    return out


def hc_default_host(prof: dict[str, Any]) -> str:
    """mDNS name of an appliance: haId as is when it already starts with the brand, else brand-type-haId."""
    ha_id, brand = str(prof.get("haId")), str(prof.get("brand") or "")
    if brand and ha_id.upper().startswith(brand.upper() + "-"):
        return f"{ha_id}.local"
    return f"{brand}-{prof.get('type', '')}-{ha_id}.local".lower()


def homeconnect_configured(h: dict[str, Any] | None = None) -> bool:
    return any(HC_DIR.glob("*.zip")) if HC_DIR.exists() else False


def _hc_val(group: dict, name: str) -> Any:
    e = group.get(name)
    if e is None:
        return None
    try:
        return e.value
    except Exception:  # noqa: BLE001
        return None


def hc_programs(app) -> list[str]:
    """Keys of the programs this appliance offers that can be started (not "select only", like pyrolysis)."""
    out = []
    for name, prog in (getattr(app, "programs", None) or {}).items():
        key = HC_PROGRAMS.get(name)
        if not key or key in out:
            continue
        if str(getattr(prog, "execution", "selectandstart")).lower().endswith("selectonly"):
            continue
        if getattr(prog, "available", None) is False:
            continue
        out.append(key)
    return out


def _hc_prog_key(prog) -> str | None:
    try:
        return HC_PROGRAMS.get(prog.name) if prog else None
    except Exception:  # noqa: BLE001
        return None


def hc_start_options(prog, kind: str, sel: dict[str, Any]) -> dict[int, Any]:
    """Options for /ro/activeProgram: the writable options with a known value, plus (oven) the chosen temperature
    and, when set, the cooking time. Keyed by option uid, as the library sends them."""
    opts: dict[int, Any] = {}
    for o in getattr(prog, "_options", None) or []:
        name = getattr(o, "name", "")
        if kind == "oven":
            if name == HC_SETPOINT and sel.get("setpoint"):
                opts[o.uid] = int(sel["setpoint"])
            elif name == HC_DURATION and int(sel.get("duration") or 0) > 0:
                opts[o.uid] = int(sel["duration"]) * 60
            continue
        try:
            writable = str(getattr(o, "access", "")).lower() == "readwrite"
            val = o.value_shadow
        except Exception:  # noqa: BLE001
            continue
        if writable and val is not None and name != "BSH.Common.Option.StartInRelative":
            opts[o.uid] = val
    return opts


def hc_state(app, kind: str, sel: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten a homeconnect_websocket HomeAppliance into what the Edge driver needs."""
    sel = sel or {}
    power = _hc_val(app.settings, "BSH.Common.Setting.PowerState")
    op = _hc_val(app.status, "BSH.Common.Status.OperationState")
    door = _hc_val(app.status, "BSH.Common.Status.DoorState")
    active = None
    try:
        ap = app.active_program
        active = ap.name if ap else None
    except Exception:  # noqa: BLE001
        active = None
    running = op in ("Run", "Pause", "DelayedStart", "ActionRequired")
    remaining = _hc_val(app.options, "BSH.Common.Option.RemainingProgramTime") if running else None
    progress = _hc_val(app.options, "BSH.Common.Option.ProgramProgress") if running else None
    st = {
        "power": "on" if power == "On" else "off",
        "operation": HC_OPERATION.get(str(op), str(op).lower() if op else "inactive"),
        "door": str(door).lower() if door else None,                 # open closed locked
        "program": None,
        "remainingTime": int(remaining) if isinstance(remaining, (int, float)) else None,   # seconds
        "completionTime": _iso_in(float(remaining)) if isinstance(remaining, (int, float)) and running else None,
        "progress": int(progress) if isinstance(progress, (int, float)) else None,           # percent
        "remoteStartAllowed": bool(_hc_val(app.status, "BSH.Common.Status.RemoteControlStartAllowed")),
        "remoteControlActive": bool(_hc_val(app.status, "BSH.Common.Status.RemoteControlActive")),
    }
    # program: the running one, else the one selected on the appliance, else the one chosen in SmartThings
    key = HC_PROGRAMS.get(active) if active and running else None
    if not key:
        try:
            key = _hc_prog_key(app.selected_program)
        except Exception:  # noqa: BLE001
            key = None
    st["program"] = key or sel.get("program") or "none"
    st["programs"] = hc_programs(app)
    if kind == "oven":
        t = _hc_val(app.status, "Cooking.Oven.Status.CurrentCavityTemperature")
        sp = _hc_val(app.options, HC_SETPOINT)
        st["temperature"] = float(t) if isinstance(t, (int, float)) else None
        st["setpoint"] = float(sp) if isinstance(sp, (int, float)) and running else float(sel.get("setpoint") or 180)
        st["duration"] = int(sel.get("duration") or 0)    # minutes, 0 = no timer
    return st


class HomeConnect:
    """Home Connect appliances over their local websocket (library homeconnect-websocket, Python 3.13+).
    Keys and descriptions come from the Home Connect Profile Downloader zips in the homeconnect folder;
    the appliance address from config ("homeconnect": {"hosts": {haId: ip}}), homeconnect/hosts.json or the mDNS
    name (give the appliances a fixed IP in the router)."""

    POLL = 5

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.h = dict(cfg.get("homeconnect") or {})
        # appliance addresses: config "homeconnect.hosts", or hosts.json next to the profile zips
        hosts = dict(self.h.get("hosts") or {})
        try:
            hosts.update(json.loads((HC_DIR / "hosts.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.h["hosts"] = hosts
        self.apps: dict[str, Any] = {}
        self.kinds: dict[str, str] = {}
        self.wake: dict[str, asyncio.Event] = {}
        # program, oven temperature and cooking time chosen in SmartThings, per appliance (kept in config.json)
        self.sel: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (self.h.get("selection") or {}).items()}

    def selection(self, ha_id: str) -> dict[str, Any]:
        cur = self.sel.setdefault(ha_id, {})
        for k, v in HC_DEFAULT.get(self.kinds.get(ha_id, ""), {}).items():
            cur.setdefault(k, v)
        return cur

    def _save_selection(self) -> None:
        cfg = load_config()
        cfg.setdefault("homeconnect", {})["selection"] = self.sel
        save_config(cfg)

    async def _start(self, ha_id: str, app, key: str | None) -> None:
        sel = self.selection(ha_id)
        op = _hc_val(app.status, "BSH.Common.Status.OperationState")
        if op == "Pause" and not key:
            c = app.commands.get("BSH.Common.Command.ResumeProgram")
            if c is not None:
                await c.execute(1)
                return
        if not _hc_val(app.status, "BSH.Common.Status.RemoteControlStartAllowed"):
            raise ValueError("remote start is not allowed: switch on Remote Start on the appliance first")
        if key:
            sel["program"] = key
        key = sel.get("program")
        prog = app.programs.get(HC_PROGRAM_NAME.get(key or "", "")) if key and key != "none" else None
        if prog is None:
            try:
                prog = app.selected_program
            except Exception:  # noqa: BLE001
                prog = None
        if prog is None:
            raise ValueError(f"program {key} not available on this appliance")
        opts = hc_start_options(prog, self.kinds.get(ha_id, ""), sel)
        await prog.start(opts, override_options=True)
        log.info("[homeconnect] %s start %s (%d options)", ha_id, _hc_prog_key(prog) or prog.name, len(opts))

    def _app_id(self) -> str:
        cfg = load_config()
        h = cfg.setdefault("homeconnect", {})
        if not h.get("app_id"):
            h["app_id"] = secrets.token_hex(8)
            save_config(cfg)
        return h["app_id"]

    def _command(self, ha_id: str):
        async def handler(cmd: str, value: Any) -> None:
            app = self.apps.get(ha_id)
            if app is None:
                raise ValueError("appliance not connected")
            if cmd == "refresh":
                return
            if cmd in ("on", "off"):
                e = app.settings.get("BSH.Common.Setting.PowerState")
                if e is None:
                    raise ValueError("no power setting")
                values = list((e.enum or {}).values())
                target = "On" if cmd == "on" else ("Off" if "Off" in values else "Standby")
                await e.set_value(target)
            elif cmd == "program":
                key = str(value or "")
                if key not in HC_PROGRAM_NAME:
                    raise ValueError(f"unknown program {key}")
                self.selection(ha_id)["program"] = key
                self._save_selection()
                prog = app.programs.get(HC_PROGRAM_NAME[key])
                if prog is not None and _hc_val(app.status, "BSH.Common.Status.RemoteControlActive"):
                    try:  # also show it on the appliance; not possible on every appliance or in every state
                        await prog.select()
                    except Exception as ex:  # noqa: BLE001
                        log.debug("[homeconnect] %s select %s: %s", ha_id, key, ex)
            elif cmd == "setpoint":
                self.selection(ha_id)["setpoint"] = max(30, min(300, int(round(float(value)))))
                self._save_selection()
            elif cmd == "duration":
                self.selection(ha_id)["duration"] = max(0, min(1440, int(round(float(value)))))
                self._save_selection()
            elif cmd == "start":
                if value and str(value) not in HC_PROGRAM_NAME:
                    raise ValueError(f"unknown program {value}")
                await self._start(ha_id, app, str(value) if value else None)
                if value:
                    self._save_selection()
            elif cmd in ("stop", "pause", "resume"):
                name = {"stop": "BSH.Common.Command.AbortProgram", "pause": "BSH.Common.Command.PauseProgram",
                        "resume": "BSH.Common.Command.ResumeProgram"}[cmd]
                c = app.commands.get(name)
                if c is None:
                    raise ValueError(f"{cmd} not supported by this appliance")
                await c.execute(1)
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[homeconnect] %s %s %s", ha_id, cmd, "" if value is None else value)
            ev = self.wake.get(ha_id)
            if ev is not None:
                ev.set()
        return handler

    async def _run_one(self, prof: dict[str, Any], app_id: str, session) -> None:
        from homeconnect_websocket import HomeAppliance, parse_device_description
        ha_id = prof["haId"]
        kind = HC_TYPES.get(prof.get("type"), "appliance")
        self.kinds[ha_id] = kind
        wake = self.wake[ha_id] = asyncio.Event()
        dev_id = f"homeconnect-{ha_id}"
        name = f"{prof.get('brand', '').title()} {prof.get('type', 'Appliance')}".strip()
        host = (self.h.get("hosts") or {}).get(ha_id) or hc_default_host(prof)
        desc = parse_device_description(*prof["xml"])
        state = {"connected": False}

        async def on_conn(new_state) -> None:
            up = str(new_state).lower() == "connected"
            state["connected"] = up
            REG.set_online(dev_id, up)
            log.info("[homeconnect] %s (%s): %s", name, host, str(new_state).split(".")[-1].lower())

        app = HomeAppliance(desc, host, "Stoeckie Bridge", app_id, prof["key"], prof.get("iv") or None,
                            session=session, logger=log, reconect=True, connection_callback=on_conn)
        REG.upsert(dev_id, kind, name, {"model": prof.get("vib"), "brand": prof.get("brand")}, online=False,
                   handler=self._command(ha_id))
        while True:
            try:
                await app.connect()
                break
            except Exception as ex:  # noqa: BLE001
                log.warning("[homeconnect] %s (%s): connect failed: %s, retry in 60 s", name, host, ex)
                await asyncio.sleep(60)
        self.apps[ha_id] = app
        last = None
        while True:
            try:
                st = hc_state(app, kind, self.selection(ha_id))
                if st != last:
                    REG.upsert(dev_id, kind, name, st, online=state["connected"])
                    last = st
                else:
                    REG.set_online(dev_id, state["connected"])
            except Exception as ex:  # noqa: BLE001
                log.debug("[homeconnect] %s: state: %s", name, ex)
            try:  # next check after POLL seconds, or right after a command
                await asyncio.wait_for(wake.wait(), self.POLL)
                await asyncio.sleep(0.5)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    async def run(self) -> None:
        profiles = homeconnect_profiles()
        if not profiles:
            return
        if sys.version_info < (3, 13):
            log.warning("[homeconnect] %d profile(s) found, but local Home Connect needs Python 3.13 or newer "
                        "(this is %d.%d): run the installer again", len(profiles), *sys.version_info[:2])
            return
        import aiohttp
        app_id = self._app_id()
        log.info("[homeconnect] %d appliance(s): %s", len(profiles),
                 ", ".join(f"{p.get('type')} {p.get('vib')} ({p.get('connectionType')})" for p in profiles))
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*(self._run_one(p, app_id, session) for p in profiles))


# ---------------------------------------------------------------- Samsung appliances (OCF over CoAP-DTLS), local

SS_DIR = DATA / "samsung"
# The client identity the factory ACL of compatible Samsung appliances grants access to (a public constant, see
# github.com/QuiteYellow/SmartThings-Local docs/certificates.md). The appliance checks only this UUID in the
# certificate subject; the key and the self-signed certificate are made here and never leave this folder.
SS_CLIENT_UUID = "ab0b0ac4-aae9-4958-a04d-8ec36fe1b2f9"
SS_PORTS = [5684] + list(range(49152, 49161))


def samsung_cert(folder: Path | None = None) -> tuple[Path, Path]:
    """Client certificate for local Samsung access (made once): RSA 2048, self-signed, UUID in OU, CN and SAN."""
    folder = folder or SS_DIR
    cert_p, key_p = folder / "client_fullchain.pem", folder / "client.key"
    if cert_p.exists() and key_p.exists():
        return cert_p, key_p
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    folder.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    u = SS_CLIENT_UUID
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, f"uuid:{u}"),
                      x509.NameAttribute(NameOID.COMMON_NAME, f"urn:uuid:{u}")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.SubjectAlternativeName([
                x509.UniformResourceIdentifier(f"urn:uuid:{u}"), x509.UniformResourceIdentifier(f"uri:uuid:{u}"),
                x509.UniformResourceIdentifier(f"uuid:{u}"), x509.DNSName(u)]), critical=False)
            .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p


# Washer course codes per course table (from /st/washercourse/vs/0), from the localthings project
# (github.com/mbillow/localthings, MIT), confirmed there by reading back courses chosen on the machines.
SS_WASHER_COURSES = {
    "Table_00": {"01": "Normal", "55": "Whites", "57": "Self Clean+", "5b": "Cotton", "5c": "Extra Speed", "5d": "Super Eco Wash", "5e": "Delicates", "5f": "Baby Care", "60": "Outdoor", "61": "Dark Garment", "63": "Drum Clean", "64": "Rinse + Spin", "65": "Wool", "66": "Bedding", "67": "Synthetics", "68": "E Cotton", "6b": "Spin Only", "6c": "Denim", "70": "Heavy Duty", "71": "Bedding", "72": "Sanitize", "73": "Rinse + Spin", "74": "Active Wear", "75": "Delicates", "77": "Perm Press", "78": "Quick Wash", "91": "Self Clean", "94": "Towels", "9c": "Heavy Duty", "9d": "Rinse + Spin", "9e": "Perm Press", "9f": "Quick Wash", "a2": "Delicates", "a3": "Active Wear", "a5": "Bedding", "a9": "Sanitize", "aa": "Whites", "ab": "Eco Cold", "b1": "Self Clean+", "ba": "Drain/Spin", "d0": "Cotton", "d1": "E Cotton", "d2": "Synthetics", "d3": "Delicates", "d4": "Rinse + Spin", "d5": "Drum Clean", "d6": "Bedding", "d7": "Outdoor Care", "d8": "Wool", "da": "Super Eco Wash", "db": "Super Speed", "dc": "15' Quick Wash", "e4": "Anti Allergen"},
    "Table_02": {"01": "Normal", "02": "Extra Heavy Duty", "03": "Super Eco Wash", "04": "Quick Wash", "05": "Wool/Lingerie", "06": "Bedding", "07": "Outdoor", "08": "Rinse+Spin", "09": "Drum Clean", "0a": "Towels", "0b": "Boil Wash", "0c": "Baby Care", "0d": "Spin Only", "0e": "Cloudy Day", "0f": "Pure Wash", "10": "Spin Dry", "11": "Summer Bedding", "12": "Cottons", "13": "Black Cottons", "14": "Delicate Underwear", "15": "Activewear", "16": "Blouses", "17": "Downloaded", "18": "Soft Bubble", "19": "AI Wash", "1a": "Shirts", "1b": "Cotton", "1c": "Eco 40-60", "1d": "Super Speed", "1e": "15' Quick Wash", "1f": "Intense Cold", "20": "Hygiene Steam", "21": "Colors", "22": "Wool", "23": "Outdoor", "24": "Bedding", "25": "Synthetics", "26": "Delicates", "27": "Rinse+Spin", "28": "Drain/Spin", "29": "Drum Clean+", "2a": "Jeans", "2b": "AI Wash", "2d": "Silent Wash", "2e": "Baby Care", "2f": "Activewear", "30": "Cloudy Day", "32": "Shirts", "33": "Towels", "34": "Mixed", "35": "E Cotton", "36": "Wash+Dry", "37": "Air Wash", "38": "Cotton Dry", "39": "Synthetics Dry", "3a": "Drum Clean", "51": "Super Speed", "52": "Eco Cold", "53": "Heavy Duty", "54": "Towels", "55": "Activewear", "56": "Bedding", "57": "Delicates", "5a": "Steam Sanitize", "5b": "Small Load", "5e": "Rinse+Spin", "60": "Self Clean+", "64": "Steam Whites", "65": "Colors", "66": "Denim", "69": "AI Wash", "6a": "Wool", "6b": "Denim", "6c": "Blouses", "6d": "Delicates", "6e": "Active Wear", "6f": "Bedding", "70": "Towels", "71": "Quick Wash", "72": "Shirts", "73": "Sanitize", "74": "Drum Clean", "75": "Outdoor", "76": "Baby Care", "77": "Cottons", "78": "Rinse + Spin", "79": "Spin Only", "7c": "Whites", "7d": "Bedding/Waterproof", "7e": "Self-Clean", "7f": "Wool/Delicate", "85": "Steam Normal", "86": "Deep Wash", "87": "Download", "88": "Pet Care", "8b": "Quick Wash", "8c": "AI OptiWash", "8f": "Intense Cold", "96": "Less Microfiber", "a0": "15' Quick Wash", "b0": "Mixed Load", "58": "Wool", "5c": "Outdoor", "5d": "Power Rinse", "5f": "Spin Only", "63": "Power Steam", "67": "Steam Allergen", "68": "Steam Bulky"},
}
# Course label (above) -> key of the SmartThings capability silvervalley08660.applianceProgram (EN/NL labels in
# tools/presentation/gen_homeconnect.py). Courses without a key here are not offered in SmartThings.
SS_COURSE_KEY = {
    "Cotton": "cotton", "Cottons": "cotton", "E Cotton": "eCotton", "Eco 40-60": "eco4060",
    "Synthetics": "synthetics", "Delicates": "delicates", "Wool": "wool", "Bedding": "bedding",
    "Outdoor": "outdoor", "Outdoor Care": "outdoor", "Baby Care": "babyCare", "Anti Allergen": "antiAllergen",
    "15' Quick Wash": "quick15", "Quick Wash": "quickWash", "Super Speed": "superSpeed",
    "Super Eco Wash": "superEco", "Mixed Load": "mixed", "Mixed": "mixed", "Towels": "towels", "Denim": "denim",
    "Jeans": "denim", "Shirts": "shirts", "Active Wear": "activeWear", "Activewear": "activeWear",
    "Colors": "colors", "Whites": "whites", "AI Wash": "aiWash", "Hygiene Steam": "hygieneSteam",
    "Rinse + Spin": "rinseSpin", "Rinse+Spin": "rinseSpin", "Spin Only": "spinOnly", "Drain/Spin": "drainSpin",
    "Drum Clean": "drumClean", "Drum Clean+": "drumClean",
}
# Table_00 course BD is not in the table above; on EU washers it is Eco 40-60 (fixed 40 degrees, 3 rinses)
SS_EXTRA = {"Table_00": {"bd": "Eco 40-60"}}


def ss_course_codes(course: dict[str, Any], edit: dict[str, Any] | None = None) -> list[str]:
    """Course codes the washer offers: editCourseList when filled in, else the first byte of each record of
    supportedOptions (a 1 nibble header with the record width, then fixed width records), checked like the
    localthings project does: distinct codes that include the selected course."""
    raw = (edit or {}).get("x.com.samsung.da.editCourseList")
    if isinstance(raw, str) and "_" in raw:
        body = raw.split("_", 1)[1]
        codes = [body[i:i + 2] for i in range(0, len(body) - 1, 2)]
        if codes:
            return codes
    raw = course.get("x.com.samsung.da.supportedOptions")
    hexstr = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(hexstr, str) or len(hexstr) < 3 or (len(hexstr) - 1) % 2:
        return []
    body = hexstr[1:]
    total = len(body) // 2
    current = ss_option(course.get(SS_OPTIONS), "Course")

    def split(k: int) -> list[str] | None:
        if k < 1 or total % k or total // k < 2:
            return None
        firsts = [body[i * k * 2:i * k * 2 + 2] for i in range(total // k)]
        if len(set(firsts)) != len(firsts):
            return None
        if current is not None and current.lower() not in {f.lower() for f in firsts}:
            return None
        return firsts

    try:
        stated = split(1 + 2 * int(hexstr[0], 16))
    except ValueError:
        stated = None
    if stated:
        return stated
    for k in range(1, total + 1):
        got = split(k)
        if got:
            return got
    return []


SS_JOB = {"none": "none", "wash": "wash", "rinse": "rinse", "spin": "spin", "finish": "finish",
          "prewash": "preWash", "weightsensing": "weightSensing", "delaywash": "delayWash", "airwash": "airWash",
          "drying": "drying", "cooling": "cooling", "wrinkleprevent": "wrinklePrevent", "aiwash": "aIWash",
          "airinse": "aIRinse", "aispin": "aISpin", "freezeprotection": "freezeProtection"}
SS_OPTIONS = "x.com.samsung.da.options"


def ss_option(options: Any, prefix: str) -> str | None:
    """'<prefix>_<value>' from an options[] array."""
    for o in options or []:
        if isinstance(o, str) and o.startswith(prefix + "_"):
            return o.split("_", 1)[1]
    return None


def ss_seconds(raw: Any) -> int | None:
    """'HH:MM:SS' or 'MM:SS' -> seconds."""
    if not isinstance(raw, str):
        return None
    try:
        parts = [int(x) for x in raw.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        parts = [0] + parts
    if len(parts) != 3:
        return None
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def ss_course_label(code: str | None, table: str | None) -> str | None:
    if not code:
        return None
    c = code.lower()
    return ((SS_WASHER_COURSES.get(table or "") or {}).get(c) or (SS_EXTRA.get(table or "") or {}).get(c)
            or f"Course {code.upper()}")


def ss_programs(res: dict[str, Any]) -> dict[str, str]:
    """{capability key: course code} for the courses of this washer that SmartThings can show."""
    table = (res.get("/st/washercourse/vs/0") or {}).get("x.com.samsung.da.st.courseTable")
    out: dict[str, str] = {}
    for code in ss_course_codes(res.get("/course/vs/0") or {}, res.get("/wm/editcourse/vs/0")):
        key = SS_COURSE_KEY.get(ss_course_label(code, table) or "")
        if key and key not in out:
            out[key] = code
    return out


def ss_washer_state(res: dict[str, Any]) -> dict[str, Any]:
    """Flatten the washer resources (CBOR decoded, keyed by href) into what the Edge driver needs."""
    op = res.get("/operational/state/vs/0") or {}
    pw = res.get("/power/vs/0") or {}
    p0 = res.get("/power/0") or {}
    rc = res.get("/remotectrl/vs/0") or {}
    course = res.get("/course/vs/0") or {}
    table = (res.get("/st/washercourse/vs/0") or {}).get("x.com.samsung.da.st.courseTable")
    if "x.com.samsung.da.power" in pw:
        power = str(pw.get("x.com.samsung.da.power")).lower() == "on"
    elif "value" in p0:
        power = bool(p0.get("value"))
    else:
        power = bool(op)
    st = str(op.get("x.com.samsung.da.state") or "").lower()
    progress = str(op.get("x.com.samsung.da.progress") or "None")
    running = st in ("run", "running") and progress != "Finish"
    paused = st in ("pause", "paused")
    finished = progress == "Finish"
    remaining = ss_seconds(op.get("x.com.samsung.da.remainingTime")) if (running or paused) else None
    pct = op.get("x.com.samsung.da.progressPercentage")
    try:
        pct = int(pct) if (running or paused) and pct is not None else None
    except (TypeError, ValueError):
        pct = None
    code = ss_option(course.get(SS_OPTIONS), "Course")
    progs = ss_programs(res)
    key = next((k for k, c in progs.items() if code and c.lower() == code.lower()), None)
    return {
        "program": key or ("none" if not progs else None),
        "programs": list(progs),
        "power": "on" if power else "off",
        "operation": "run" if running else ("pause" if paused else ("finished" if finished else "inactive")),
        "job": "finish" if finished else (SS_JOB.get(progress.lower(), "none") if (running or paused) else "none"),
        "remainingTime": remaining,
        "completionTime": _iso_in(float(remaining)) if remaining and running else None,
        "progress": pct,
        "remoteStartAllowed": str(rc.get("x.com.samsung.da.remoteControlEnabled")).lower() == "true",
        "course": ss_course_label(code, table),
        "courseCode": code,
    }


# what a washer that does not answer any more (switched off) shows, when it was not washing
SS_OFF = {"power": "off", "remoteStartAllowed": False, "operation": "inactive", "job": "none", "remainingTime": None,
          "completionTime": None, "progress": None}


class Samsung:
    """Samsung appliances on the LAN (OCF over CoAP-DTLS, library smartthings-local). Washers for now.
    Appliances come from config ("samsung": {"appliances": [{"host": ip, "type": "washer", "name": ...}]}) or
    samsung/appliances.json; give them a fixed IP. A washer switched off sleeps its wifi: it then shows as off."""

    POLL = 15          # power, remote control
    HOT_EVERY = 4      # operational state (this washer answers slowly when asked every second)
    PING = 25          # CoAP ping, keeps the session alive
    LIVENESS = 60      # no answer at all this long: new session
    RETRY = 30         # wait before a new session after a failure
    # small resources every POLL seconds; the course resource is large (several CoAP blocks) and the firmware
    # stops answering for a while when it is read too often, so it is read at connect, every COURSE_EVERY seconds
    # and right after a command
    # The firmware drops a DTLS session that is idle for about 10 s (it then also ignores a new handshake for a
    # while), so the small operational state is read every HOT seconds, like the smartthings-local bridge demo.
    HOT = (("operational", "state", "vs", "0"),)
    RESOURCES = (("power", "vs", "0"), ("remotectrl", "vs", "0"))
    COURSE = (("course", "vs", "0"),)
    COURSE_EVERY = 300
    ONCE = (("st", "washercourse", "vs", "0"),)

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        apps = list((cfg.get("samsung") or {}).get("appliances") or [])
        try:
            apps += json.loads((SS_DIR / "appliances.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.appliances = [a for a in apps if isinstance(a, dict) and a.get("host") and a.get("type", "washer") != "tv"]
        self.sessions: dict[str, Any] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.programs: dict[str, dict[str, str]] = {}   # host -> {capability key: course code}
        self.wake: dict[str, asyncio.Event] = {}
        self.executors: dict[str, Any] = {}

    async def _call(self, host: str, fn, *args):
        """Run a blocking library call on this appliance's own thread: the DTLS session is made, read and written
        from one thread only."""
        ex = self.executors.get(host)
        if ex is None:
            import concurrent.futures
            ex = self.executors[host] = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="samsung")
        return await asyncio.get_running_loop().run_in_executor(ex, lambda: fn(*args))

    @staticmethod
    def dev_id(host: str) -> str:
        return "samsung-" + host.replace(".", "-")

    def _open(self, host: str):
        """Find the DTLS port and open an authenticated session (blocking, run in a thread)."""
        from smartthings_local.protocol import dtls_probe
        from smartthings_local.protocol.auth import CertificateAuth
        from smartthings_local.protocol.dtls_session import DtlsCoapSession
        cert_p, key_p = samsung_cert()
        live = dtls_probe.probe_dtls_ports(host, SS_PORTS)
        port = live.selected_port or (live.responder_ports[0] if live.responder_ports else None)
        if not port:
            raise ConnectionError("no answer (switched off?)")
        # a fixed local UDP port per appliance (like the smartthings-local bridge demo): a reconnect then comes from
        # the same peer address, so the appliance replaces the old association instead of waiting for it to expire
        local = 26849 + sorted(a["host"] for a in self.appliances).index(host) if any(
            a["host"] == host for a in self.appliances) else None
        sess = DtlsCoapSession(host, port, auth=CertificateAuth.from_files(str(cert_p), str(key_p)), local_port=local)
        sess.connect(timeout=12.0)
        sess.start_reader()
        return sess, port

    @staticmethod
    def _read(sess, paths, timeout: float = 4.0) -> dict[str, Any]:
        """GET each path; a path that times out is skipped (the firmware sometimes wedges on one resource for a
        while). Raises only when nothing answered, so one slow resource does not tear the session down."""
        import cbor2
        out: dict[str, Any] = {}
        errors = 0
        last: Exception | None = None
        for path in paths:
            try:
                code, body = sess.get(list(path), timeout=timeout)
            except Exception as ex:  # noqa: BLE001
                errors += 1
                last = ex
                log.debug("[samsung] GET /%s: %r", "/".join(path), ex)
                if "closed" in str(ex).lower():
                    raise
                continue
            if code == 0x45 and body:
                try:
                    out["/" + "/".join(path)] = cbor2.loads(body)
                except Exception:  # noqa: BLE001
                    pass
        if paths and errors == len(paths):
            raise last or ConnectionError("no answer")
        return out

    @staticmethod
    def _write(sess, path, body: dict[str, Any]) -> None:
        import cbor2
        code, _ = sess.post(list(path), cbor2.dumps(body))
        if code >> 5 != 2:
            raise ValueError(f"appliance refused ({code >> 5}.{code & 0x1F:02d}); starting, pausing and changing "
                             "the course need Smart Control switched on at the washer")

    def _command(self, host: str):
        async def handler(cmd: str, value: Any) -> None:
            sess = self.sessions.get(host)
            if cmd == "refresh":
                return
            if sess is None:
                raise ValueError("washer not reachable (switched off?)")
            op = ("operational", "state", "vs", "0")
            body = {"run": {"x.com.samsung.da.state": "Run"}, "start": {"x.com.samsung.da.state": "Run"},
                    "program": None,
                    "resume": {"x.com.samsung.da.state": "Run"}, "pause": {"x.com.samsung.da.state": "Pause"},
                    "stop": {"x.com.samsung.da.state": "Ready"}}.get(cmd)
            async with self.locks[host]:
                if cmd in ("program", "start") and value:
                    code = self.programs.get(host, {}).get(str(value))
                    if not code:
                        raise ValueError(f"program {value} not available on this washer")
                    await self._call(host, self._write, sess, ("course", "vs", "0"), {SS_OPTIONS: [f"Course_{code}"]})
                    if cmd == "program":
                        body = None
                if body is not None:
                    await self._call(host, self._write, sess, op, body)
                elif cmd == "program":
                    pass
                elif cmd in ("on", "off"):
                    try:
                        await self._call(host, self._write, sess, ("power", "vs", "0"),
                                                {"x.com.samsung.da.power": "On" if cmd == "on" else "Off"})
                    except ValueError:
                        await self._call(host, self._write, sess, ("power", "0"), {"value": cmd == "on"})
                else:
                    raise ValueError(f"unsupported command {cmd}")
            log.info("[samsung] %s %s %s", host, cmd, "" if value is None else value)
            ev = self.wake.get(host)
            if ev is not None:
                ev.set()
        return handler

    @staticmethod
    def _read_soft(sess, paths, timeout: float) -> dict[str, Any]:
        """Like _read, but never raises for timeouts: a GET the firmware does not answer is just skipped."""
        try:
            return Samsung._read(sess, paths, timeout)
        except Exception as ex:  # noqa: BLE001
            if "closed" in str(ex).lower():
                raise
            return {}

    async def _run_one(self, a: dict[str, Any]) -> None:
        """Like the smartthings-local bridge demo: the small operational state every second (2 s timeout), the
        rest at a slower pace, and a GET that times out is skipped instead of tearing the session down (the
        firmware sometimes stops answering for a few seconds, and a torn down session makes it ignore new
        handshakes for a while). Only after LIVENESS seconds without any answer is the session closed and made
        again; a CoAP ping every PING seconds keeps it alive."""
        host = str(a["host"])
        kind = str(a.get("type") or "washer")
        name = str(a.get("name") or "Samsung Washer")
        dev_id = self.dev_id(host)
        self.locks[host] = asyncio.Lock()
        wake = self.wake[host] = asyncio.Event()
        handler = self._command(host)
        # offline and without a state until the first answer: a bridge restart then does not show the washer as
        # switched off with no course for a moment
        REG.upsert(dev_id, kind, name, {}, online=False, handler=handler)
        once: dict[str, Any] = {}
        cache: dict[str, Any] = {}
        last, fails, logged = None, 0, False
        while True:
            sess = self.sessions.get(host)
            if sess is None:
                try:
                    sess, port = await self._call(host, self._open, host)
                except Exception as ex:  # noqa: BLE001
                    fails += 1
                    if fails <= 3 or fails % 30 == 0:
                        log.info("[samsung] %s (%s): %r", name, host, ex)
                    if fails >= 2:
                        # no answer: switched off (its wifi sleeps) or another local client holds the only session.
                        # The device shows offline. When it was not washing (or did not answer at all since the
                        # bridge started), it also shows off and without remote control (a switched off washer can not
                        # be started); when the connection drops during a wash the last state stays.
                        if not last or last.get("operation") not in ("run", "pause"):
                            last = {**(last or {}), **SS_OFF}
                            REG.upsert(dev_id, kind, name, last, online=False)
                        else:
                            REG.set_online(dev_id, False)
                    await asyncio.sleep(self.RETRY if fails < 10 else 2 * self.RETRY)
                    continue
                self.sessions[host] = sess
                REG.set_online(dev_id, True)
                log.info("[samsung] %s (%s): connected on port %s", name, host, port)
                fails = 0
                now = time.monotonic()
                ok_at, warm_at, course_at, ping_at = now, 0.0, 0.0, now
                timeouts = 0
            try:
                now = time.monotonic()
                cmd = wake.is_set()
                wake.clear()
                async with self.locks[host]:
                    fresh = await self._call(host, self._read_soft, sess, self.HOT, 4.0)
                    if now - warm_at >= self.POLL or cmd:
                        fresh.update(await self._call(host, self._read_soft, sess, self.RESOURCES, 4.0))
                        warm_at = now
                    if not once:
                        once = await self._call(host, self._read_soft, sess, self.ONCE, 4.0)
                    if now - course_at >= self.COURSE_EVERY or cmd or "/course/vs/0" not in cache:
                        got = await self._call(host, self._read_soft, sess, self.COURSE, 20.0)
                        if got:
                            fresh.update(got)
                            course_at = now
                        else:
                            course_at = now - self.COURSE_EVERY + 30   # try again in 30 s
                    if now - ping_at >= self.PING:
                        ping_at = now
                        try:
                            await self._call(host, sess.ping)
                        except Exception:  # noqa: BLE001
                            pass
                if fresh:
                    ok_at = now
                    if timeouts >= 3:  # single skipped reads are normal for this firmware; only log real gaps
                        log.info("[samsung] %s: answering again after %d skipped read(s)", name, timeouts)
                    timeouts = 0
                else:
                    timeouts += 1
                    if timeouts in (3, 10, 30):
                        log.info("[samsung] %s: no answer to %d read(s) in a row", name, timeouts)
                if now - ok_at > self.LIVENESS:
                    raise ConnectionError(f"no answer for {self.LIVENESS} s")
                if fresh:
                    res = {**cache, **fresh, **once}
                    cache = res
                    if not logged:  # once, to learn this model's resources (no secrets in them)
                        logged = True
                        log.info("[samsung] %s resources: %s", name, _ss_short(res, 3000))
                    st = ss_washer_state(res)
                    self.programs[host] = ss_programs(res)
                    if st != last:
                        REG.upsert(dev_id, kind, name, st, online=True)
                        last = st
            except Exception as ex:  # noqa: BLE001
                log.info("[samsung] %s (%s): %r, new session", name, host, ex)
                try:
                    await self._call(host, sess.close)
                except Exception:  # noqa: BLE001
                    pass
                self.sessions.pop(host, None)
                await asyncio.sleep(self.RETRY)
                continue
            try:  # next read after HOT_EVERY seconds, or right after a command
                await asyncio.wait_for(wake.wait(), self.HOT_EVERY)
                await asyncio.sleep(0.5)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        if not self.appliances:
            return
        try:
            import smartthings_local  # noqa: F401
        except ImportError:
            log.warning("[samsung] %d appliance(s) configured, but smartthings-local is not installed: run the "
                        "installer again", len(self.appliances))
            return
        log.info("[samsung] %d appliance(s): %s", len(self.appliances),
                 ", ".join(f"{a.get('type', 'washer')} {a['host']}" for a in self.appliances))
        await asyncio.gather(*(self._run_one(a) for a in self.appliances))


# ---------------------------------------------------------------- Samsung TV (Tizen, 2016 and later), local

TV_INPUTS = {"HDMI1": "KEY_HDMI1", "HDMI2": "KEY_HDMI2", "HDMI3": "KEY_HDMI3", "HDMI4": "KEY_HDMI4",
             "digitalTv": "KEY_DTV"}
TV_RC = "/upnp/control/RenderingControl1"


def tv_soap(action: str, args: str = "") -> tuple[dict[str, str], str]:
    """Headers and body of a UPnP RenderingControl call (volume and mute; the TV's own DMR service, port 9197)."""
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1"><InstanceID>0</InstanceID>'
            f'<Channel>Master</Channel>{args}</u:{action}></s:Body></s:Envelope>')
    headers = {"Content-Type": 'text/xml; charset="utf-8"',
               "SOAPACTION": f'"urn:schemas-upnp-org:service:RenderingControl:1#{action}"'}
    return headers, body


def tv_xml_value(xml: str, tag: str) -> str | None:
    i = xml.find(f"<{tag}>")
    j = xml.find(f"</{tag}>", i)
    return xml[i + len(tag) + 2:j].strip() if i >= 0 and j > i else None


def wol_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + raw * 16


class SamsungTV:
    """Samsung TVs on the LAN: power state from http://tv:8001/api/v2/, volume and mute over UPnP (port 9197),
    keys (power, input, channel) over the TV's remote control websocket (wss, port 8002). The first key makes the TV
    ask "Allow" on screen; the token it then gives is kept in config.json (samsungtv.tokens). Power on from standby
    sends the power key and a wake-on-LAN packet (needs "Power on with mobile" / network standby on the TV).
    TVs come from samsung/appliances.json (type "tv", host, mac, name) or config "samsungtv": {"tvs": [...]}."""

    POLL = 5
    NAME = "Stoeckie Bridge"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        tvs = list((cfg.get("samsungtv") or {}).get("tvs") or [])
        try:
            tvs += [a for a in json.loads((SS_DIR / "appliances.json").read_text(encoding="utf-8"))
                    if isinstance(a, dict) and a.get("type") == "tv"]
        except (OSError, ValueError):
            pass
        self.tvs = [t for t in tvs if isinstance(t, dict) and t.get("host")]
        self.tokens: dict[str, str] = dict((cfg.get("samsungtv") or {}).get("tokens") or {})
        self.session = None
        self.wake: dict[str, asyncio.Event] = {}
        self.last: dict[str, dict[str, Any]] = {}
        self.pair_now: dict[str, bool] = {}

    @staticmethod
    def dev_id(host: str) -> str:
        return "samsungtv-" + host.replace(".", "-")

    def _save_token(self, host: str, token: str) -> None:
        self.tokens[host] = token
        cfg = load_config()
        cfg.setdefault("samsungtv", {}).setdefault("tokens", {})[host] = token
        save_config(cfg)
        log.info("[samsungtv] %s: paired", host)

    async def _info(self, host: str) -> dict[str, Any] | None:
        import aiohttp
        try:
            async with self.session.get(f"http://{host}:8001/api/v2/", timeout=aiohttp.ClientTimeout(total=3)) as r:
                return (await r.json(content_type=None)).get("device") or {}
        except Exception:  # noqa: BLE001
            return None

    async def _soap(self, host: str, action: str, args: str = "") -> str | None:
        import aiohttp
        headers, body = tv_soap(action, args)
        try:
            async with self.session.post(f"http://{host}:9197{TV_RC}", data=body.encode(), headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=3)) as r:
                return await r.text() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def _keys(self, host: str, keys: list[str], wait_pair: float = 30.0) -> None:
        """Send remote control keys over the websocket (pairs the first time: the TV asks "Allow")."""
        import ssl as _ssl
        import aiohttp
        name = base64.b64encode(self.NAME.encode()).decode()
        url = f"wss://{host}:8002/api/v2/channels/samsung.remote.control?name={name}"
        if self.tokens.get(host):
            url += f"&token={self.tokens[host]}"
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE   # the TV has a self-signed certificate; this is the LAN only
        async with self.session.ws_connect(url, ssl=ctx, timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                           heartbeat=None) as ws:
            deadline = time.monotonic() + wait_pair
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ValueError("the TV did not answer: accept 'Allow' on the TV screen and try again")
                msg = await ws.receive(timeout=left)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    raise ValueError(f"TV closed the connection ({getattr(msg.type, 'name', msg.type)}, "
                                     f"{ws.exception() or msg.extra or ''}); Allow not chosen in time?")
                ev = json.loads(msg.data)
                if ev.get("event") == "ms.channel.connect":
                    tok = (ev.get("data") or {}).get("token")
                    if tok and tok != self.tokens.get(host):
                        self._save_token(host, str(tok))
                    break
                if ev.get("event") == "ms.channel.unauthorized":
                    raise ValueError("not allowed on the TV (Settings, General, External Device Manager, "
                                     "Device Connection Manager: allow Stoeckie Bridge)")
            for k in keys:
                await ws.send_str(json.dumps({"method": "ms.remote.control", "params": {
                    "Cmd": "Click", "DataOfCmd": k, "Option": "false", "TypeOfRemote": "SendRemoteKey"}}))
                await asyncio.sleep(0.4)

    def _wol(self, mac: str) -> None:
        pkt = wol_packet(mac)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(3):
                sk.sendto(pkt, ("255.255.255.255", 9))

    def _command(self, tv: dict[str, Any]):
        host = str(tv["host"])

        async def handler(cmd: str, value: Any) -> None:
            st = self.last.get(host) or {}
            on = st.get("power") == "on"
            if cmd == "refresh":
                if not self.tokens.get(host):
                    self.pair_now[host] = True
            elif cmd == "on":
                if not on:
                    if tv.get("mac"):
                        self._wol(str(tv["mac"]))
                    try:
                        await self._keys(host, ["KEY_POWER"], 10)
                    except Exception as ex:  # noqa: BLE001  (deep standby: only wake-on-LAN can wake it)
                        log.info("[samsungtv] %s on: %s", host, ex)
            elif cmd == "off":
                if on:
                    await self._keys(host, ["KEY_POWER"])
            elif cmd == "volume":
                v = max(0, min(100, int(round(float(value)))))
                if await self._soap(host, "SetVolume", f"<DesiredVolume>{v}</DesiredVolume>") is None:
                    raise ValueError("TV did not accept the volume (switched off?)")
            elif cmd in ("volumeUp", "volumeDown"):
                await self._keys(host, ["KEY_VOLUP" if cmd == "volumeUp" else "KEY_VOLDOWN"])
            elif cmd == "mute":
                m = 1 if str(value).lower() in ("1", "true", "muted", "on") else 0
                if await self._soap(host, "SetMute", f"<DesiredMute>{m}</DesiredMute>") is None:
                    raise ValueError("TV did not accept mute (switched off?)")
            elif cmd == "input":
                key = TV_INPUTS.get(str(value))
                if not key:
                    raise ValueError(f"unknown input {value}")
                await self._keys(host, [key])
                st["input"] = str(value)
            elif cmd in ("channelUp", "channelDown"):
                await self._keys(host, ["KEY_CHUP" if cmd == "channelUp" else "KEY_CHDOWN"])
            elif cmd == "key":
                k = str(value or "").upper()
                if not k.startswith("KEY_") or not k[4:].replace("_", "").isalnum():
                    raise ValueError("key must look like KEY_HOME")
                await self._keys(host, [k])
            else:
                raise ValueError(f"unsupported command {cmd}")
            log.info("[samsungtv] %s %s %s", host, cmd, "" if value is None else value)
            ev = self.wake.get(host)
            if ev is not None:
                ev.set()
        return handler

    async def _read(self, tv: dict[str, Any]) -> dict[str, Any]:
        host = str(tv["host"])
        info = await self._info(host)
        power = "on" if info and str(info.get("PowerState", "on")).lower() == "on" else "off"
        st: dict[str, Any] = {"power": power, "model": (info or {}).get("modelName"),
                              "input": (self.last.get(host) or {}).get("input"),
                              "inputs": list(TV_INPUTS), "paired": bool(self.tokens.get(host))}
        if power == "on":
            vol = tv_xml_value(await self._soap(host, "GetVolume") or "", "CurrentVolume")
            mute = tv_xml_value(await self._soap(host, "GetMute") or "", "CurrentMute")
            st["volume"] = int(vol) if vol and vol.isdigit() else None
            st["mute"] = None if mute is None else mute == "1"
        return st

    async def _run_one(self, tv: dict[str, Any]) -> None:
        host = str(tv["host"])
        name = str(tv.get("name") or "TV")
        dev_id = self.dev_id(host)
        wake = self.wake[host] = asyncio.Event()
        REG.upsert(dev_id, "tv", name, {"power": "off", "inputs": list(TV_INPUTS)}, online=True,
                   handler=self._command(tv))
        asked_at, tries = -1e9, 0
        while True:
            try:
                st = await self._read(tv)
                if st.get("model") is None and (self.last.get(host) or {}).get("model"):
                    st["model"] = self.last[host]["model"]
                if st != self.last.get(host):
                    self.last[host] = st
                    REG.upsert(dev_id, "tv", name, st, online=True)
                now = time.monotonic()
                if (st["power"] == "on" and not self.tokens.get(host) and tries < 5
                        and (now - asked_at > 120 or self.pair_now.pop(host, False))):
                    # the TV shows "Allow" for the bridge for about 30 s; ask again every 2 minutes (at most 5
                    # times per start) or right away after Refresh on the device
                    asked_at, tries = now, tries + 1
                    log.info("[samsungtv] %s: asking the TV for access, choose Allow on the TV screen", name)
                    try:
                        await self._keys(host, [], 45)
                    except Exception as ex:  # noqa: BLE001
                        log.info("[samsungtv] %s: pairing: %s", name, ex)
            except Exception as ex:  # noqa: BLE001
                log.info("[samsungtv] %s: %r", name, ex)
            try:
                await asyncio.wait_for(wake.wait(), self.POLL)
                await asyncio.sleep(1)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    async def run(self) -> None:
        if not self.tvs:
            return
        import aiohttp
        log.info("[samsungtv] %d TV(s): %s", len(self.tvs), ", ".join(t["host"] for t in self.tvs))
        async with aiohttp.ClientSession() as self.session:
            await asyncio.gather(*(self._run_one(t) for t in self.tvs))


def _ss_short(v: Any, n: int = 600) -> str:
    t = json.dumps(v, default=lambda o: o.hex() if isinstance(o, (bytes, bytearray)) else str(o))
    return t if len(t) <= n else t[:n] + "..."


def samsung_probe(host: str) -> int:
    """Read-only check whether a Samsung appliance accepts a local session: finds the DTLS port, does one
    handshake with the client certificate and reads a few state resources (never /oic/sec/*)."""
    import cbor2
    from smartthings_local.protocol import dtls_probe, ocf_discovery
    from smartthings_local.protocol.auth import CertificateAuth
    from smartthings_local.protocol.dtls_session import DtlsCoapSession
    cert_p, key_p = samsung_cert()
    print(f"[samsung] {host}: client certificate {cert_p}")
    ports: list[int] = []
    try:
        disc = ocf_discovery.discover_ocf_secure_ports(host)
        print(f"[samsung] OCF directory (udp 5683): {disc!r}")
        ports = [int(x) for x in (getattr(disc, "ports", None) or []) if isinstance(x, int)]
    except Exception as ex:  # noqa: BLE001
        print(f"[samsung] OCF directory: {ex!r}")
    live = dtls_probe.probe_dtls_ports(host, ports + [p for p in SS_PORTS if p not in ports])
    print(f"[samsung] DTLS probe: outcome={live.outcome} selected={live.selected_port} "
          f"responders={live.responder_ports}")
    port = live.selected_port or (live.responder_ports[0] if live.responder_ports else None)
    if not port:
        print("[samsung] no DTLS server found (appliance off, asleep or not on this network)")
        return 1
    try:
        diag = dtls_probe.diagnose_dtls_handshake(host, port, cert_path=str(cert_p), key_path=str(key_p))
        print(f"[samsung] handshake: {diag!r}")
    except Exception as ex:  # noqa: BLE001
        print(f"[samsung] handshake diagnostic: {ex!r}")
    sess = DtlsCoapSession(host, port, auth=CertificateAuth.from_files(str(cert_p), str(key_p)))
    ok = 0
    try:
        sess.connect(timeout=12.0)
        sess.start_reader()
        for path in (["oic", "d"], ["operational", "state", "vs", "0"], ["power", "vs", "0"], ["power", "0"],
                     ["remotectrl", "vs", "0"], ["course", "vs", "0"], ["st", "washercourse", "vs", "0"],
                     ["washer", "vs", "0"], ["kidslock", "vs", "0"]):
            try:
                code, body = sess.get(path)
                try:
                    val = cbor2.loads(body) if body else None
                except Exception:  # noqa: BLE001
                    val = body
                print(f"[samsung] GET /{'/'.join(path)}: {code} {_ss_short(val)}")
                if str(code) in ("2.05", "69") or getattr(code, "name", "") == "CONTENT":
                    ok += 1
            except Exception as ex:  # noqa: BLE001
                print(f"[samsung] GET /{'/'.join(path)}: {ex!r}")
    except Exception as ex:  # noqa: BLE001
        print(f"[samsung] session: {ex!r}")
    finally:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"[samsung] result: {ok} resource(s) read")
    return 0 if ok else 2


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

SETUP_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Stoeckie Bridge setup</title>
<style>body{font:15px system-ui,sans-serif;max-width:640px;margin:32px auto;padding:0 16px;color:#222}
label{display:block;margin:14px 0 4px;font-weight:600}input,textarea,select{width:100%;box-sizing:border-box;padding:8px;
font:13px ui-monospace,monospace}textarea{height:90px}button{margin-top:18px;padding:10px 18px;font-size:15px}
.st{padding:10px;border-radius:6px;background:#f2f2f2;margin:12px 0}h3{margin-top:36px}</style></head><body>
<h2>Stoeckie Bridge</h2><div class="st">{status}</div>
<p>Everything you enter here is stored only locally, in config.json on this computer.</p>
<h3>Pair the SmartThings hub</h3>
<p>After pairing, the bridge only accepts signed requests from the paired hub. Press Pair; the hub pairs by itself
within 5 minutes (driver Stoeckie Bridge 2026-11-02b or newer).</p>
<form method="post"><input type="hidden" name="section" value="api_pair">
<button type="submit">Pair (open for 5 minutes)</button></form>
<form method="post"><input type="hidden" name="section" value="api_new_token">
<button type="submit">New token (pair all hubs again)</button></form>
<h3>AEG / Electrolux</h3>
<p>On <b>developer.electrolux.one</b> (sign in with your AEG / Electrolux account) create an API key and generate
an access token and a refresh token.</p>
<form method="post"><input type="hidden" name="section" value="aeg">
<label>API key</label><input name="api_key" autocomplete="off" required>
<label>Access token</label><textarea name="access_token" required></textarea>
<label>Refresh token</label><textarea name="refresh_token" required></textarea>
<button type="submit">Save AEG and connect</button></form>
<h3>Fujitsu air conditioners (FGLair)</h3>
<p>Email and password of the FGLair app.</p>
<form method="post"><input type="hidden" name="section" value="fglair">
<label>Email</label><input name="username" type="email" autocomplete="off" required>
<label>Password</label><input name="password" type="password" autocomplete="off" required>
<label>Region</label><select name="region"><option value="eu">Europe</option><option value="default">Other (Americas, Asia)</option>
<option value="cn">China</option></select>
<button type="submit">Save FGLair and connect</button></form>
<form method="post"><input type="hidden" name="section" value="fglair_settings">
<label>Update the air conditioners every (seconds, 30 to 600)</label>
<input name="poll_seconds" type="number" min="30" max="600" value="{fglair_poll}" required>
<button type="submit">Save setting</button></form>
<p>Also available per air conditioner in the SmartThings app (Settings).</p>
<h3>Škoda</h3>
<p>In the MyŠkoda app (8.16 or newer) create an API key: <b>Settings, Smart Home, API keys</b>, and choose your car.
The VIN (17 characters) is shown in the app with the car and on the registration document.</p>
<form method="post"><input type="hidden" name="section" value="skoda">
<label>API key</label><input name="api_key" autocomplete="off" required>
<label>VIN</label><input name="vin" maxlength="17" minlength="17" required>
<button type="submit">Save Škoda and connect</button></form>
<form method="post"><input type="hidden" name="section" value="skoda_settings">
<label>Update the car every (minutes, at least 5; Škoda allows 20 requests per hour)</label>
<input name="poll_minutes" type="number" min="5" max="60" value="{skoda_poll}" required>
<label>Temperature for climate on (°C)</label><input name="climate_temp" value="{skoda_temp}" required>
<button type="submit">Save settings</button></form>
<p>These settings are also available with the car in the SmartThings app (Settings).</p>
<h3>iRobot (optional)</h3>
<p>Email and password of the iRobot app. The bridge only reads the names and rooms of your favorites,
twice a day, for the Roomba driver on the hub. The robot itself is controlled locally.</p>
<form method="post"><input type="hidden" name="section" value="irobot">
<label>Email</label><input name="username" type="email" autocomplete="off" required>
<label>Password</label><input name="password" type="password" autocomplete="off" required>
<label>Country (2 letters)</label><input name="country" value="NL" maxlength="2" required>
<button type="submit">Save iRobot and connect</button></form>
<h3>Rain radar (optional)</h3>
<p>Rain now and in the next 2 hours around your home, with a radius. Buienradar in the Netherlands and Belgium,
Open-Meteo everywhere else. Also available on the Buienradar device in SmartThings
(switch it on with the setting Buienradar on the Stoeckie Bridge device). The last change wins.</p>
<form method="post"><input type="hidden" name="section" value="buienradar">
<label>Postcode (NL)</label><input name="postcode" value="{rain_pc}" placeholder="1234AB">
<label>House number (NL)</label><input name="housenumber" value="{rain_nr}" placeholder="12">
<label>Or address (any country: street number, city, country)</label><input name="address" value="{rain_addr}" placeholder="Rue de la Loi 16, Brussels, Belgium">
<label>Or latitude, longitude</label><input name="location" value="{rain_loc}" placeholder="52.37, 4.89">
<label>Source</label><select name="provider"><option value="auto"{rain_p_auto}>Automatic (Buienradar in NL/BE, otherwise Open-Meteo)</option>
<option value="buienradar"{rain_p_buienradar}>Buienradar (NL/BE, radar, 5 minutes)</option>
<option value="openmeteo"{rain_p_openmeteo}>Open-Meteo (worldwide, 15 minutes)</option></select>
<label>Radius in meters (0 = only this address, max 25000)</label><input name="radius_m" type="number" min="0" max="25000" value="{rain_radius}">
<label>Rain from (mm/h)</label><input name="threshold" value="{rain_thr}">
<button type="submit">Save rain radar</button></form>
<h3>VELUX ACTIVE departure mode (optional)</h3>
<p>To lock and unlock (departure mode: all windows and blinds blocked) from SmartThings.
The windows themselves keep running locally over HomeKit. Email and password of the VELUX ACTIVE app.</p>
<form method="post"><input type="hidden" name="section" value="velux">
<label>Email</label><input name="username" type="email" autocomplete="off" required>
<label>Password</label><input name="password" type="password" autocomplete="off" required>
<button type="submit">Save VELUX and connect</button></form>
<p>Unlocking must be signed with a key from the gateway. Once: enter the IP of the VELUX gateway,
click Pair gateway, wait until the light on the gateway blinks and then press the button on the gateway within 3 minutes.</p>
<form method="post"><input type="hidden" name="section" value="velux_pair">
<label>IP of the VELUX gateway</label><input name="gateway_ip" value="{velux_ip}" required>
<button type="submit">Pair gateway</button></form>
<form method="post"><input type="hidden" name="section" value="velux_settings">
<label>Update departure mode and rain sensor every (seconds, 30 to 600)</label>
<input name="poll_seconds" type="number" min="30" max="600" value="{velux_poll}" required>
<button type="submit">Save setting</button></form>
<p>Also available on VELUX departure mode in the SmartThings app (Settings).</p></body></html>"""


def make_app(bridge_id: str, on_setup=None, https_port: int | None = None) -> web.Application:
    app = web.Application(middlewares=[])

    def local_only(req) -> bool:
        """Setup page: only from this pc (or STBRIDGE_SETUP_ALLOW), only addressed by IP or localhost (blocks
        DNS rebinding) and never posted from another site (blocks cross-site requests from a browser)."""
        try:
            ip = ipaddress.ip_address(req.remote or "")
        except ValueError:
            return False
        if not setup_source_allowed(ip):
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

    def lan_api_refusal(req) -> web.Response | None:
        """The LAN API is only for the hub: a private address, addressed by IP (no DNS rebinding), never from a
        web page (browsers send Origin) and only JSON (a cross-site form or text/plain post is refused)."""
        try:
            ip = ipaddress.ip_address(req.remote or "")
        except ValueError:
            ip = None
        if ip is None or not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return web.json_response({"error": "forbidden"}, status=403)
        host = (req.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if host != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError:
                return web.json_response({"error": "address the bridge by IP"}, status=403)
        if "Origin" in req.headers or req.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            return web.json_response({"error": "no browser requests"}, status=403)
        if req.method == "POST" and req.content_type != "application/json":
            return web.json_response({"error": "JSON only"}, status=415)
        return None

    @web.middleware
    async def guard(req, handler):
        if req.path.startswith("/stbridge/"):
            refusal = lan_api_refusal(req)
            if refusal is not None:
                return refusal
            cfg = load_config()
            token = cfg.get("api_token") if api_paired(cfg) else None
            sig = req.headers.get("X-Stbridge-Sig")
            if token and sig:
                body = await req.read()
                why = check_request_sig(token, req.method, req.raw_path, req.headers.get("X-Stbridge-Time", ""),
                                        sig, body)
                if why:
                    log.warning("[api] refused %s %s from %s: %s", req.method, req.path, req.remote, why)
                    return web.json_response({"error": why}, status=401)
                resp = await handler(req)
                if isinstance(resp, web.Response) and isinstance(resp.body, (bytes, bytearray)):
                    resp.headers["X-Stbridge-Sig"] = sign(token, sig.lower().encode() + b"\n" + bytes(resp.body))
                return resp
            # open even after pairing: discovery info, the pairing call itself, and the read-only iRobot favorites
            # that the Roomba driver (another driver, no token) fetches
            open_paths = req.path in ("/stbridge/info", "/stbridge/pair") or (
                req.method == "GET" and req.path.startswith("/stbridge/irobot/"))
            if token and not open_paths:
                return web.json_response({"error": "pairing required"}, status=401)
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
            lines.append("AEG: not set up yet.")
        elif devs:
            lines.append("AEG connected: " + ", ".join(f"{d['name']} ({d['state'].get('applianceState')})" for d in devs))
        else:
            lines.append("AEG set up, no dryer found yet (see bridge.log).")
        acs = [d for d in REG.devices.values() if d["type"] == "aircon"]
        if not fglair_configured(f):
            lines.append("FGLair: not set up yet.")
        elif acs:
            lines.append("FGLair connected: " + ", ".join(
                f"{d['name']} ({d['state'].get('mode')}, {d['state'].get('setpoint')} C)" for d in acs))
        else:
            lines.append("FGLair set up, no air conditioner found yet (see bridge.log).")
        k = cfg.get("skoda") or {}
        cars = [d for d in REG.devices.values() if d["type"] == "car"]
        if skoda_configured(k):
            if cars:
                lines.append("Škoda connected: " + ", ".join(
                    f"{d['name']} ({d['state'].get('battery')}%, key valid until {d['state'].get('keyExpires') or '?'})"
                    for d in cars))
            else:
                lines.append("Škoda set up, no data yet (see bridge.log).")
        b = cfg.get("buienradar") or {}
        if buienradar_enabled(b):
            rd = REG.devices.get("buienradar")
            lines.append("Rain radar: " + (rd["state"].get("summary", "") if rd else "fetching")
                         + f" ({b.get('place') or 'no location'}, radius {b.get('radius_m', 0)} m)")
        hcs = [d for d in REG.devices.values() if d["id"].startswith("homeconnect-")]
        if hcs:
            lines.append("Home Connect (local): " + ", ".join(
                f"{d['name']} ({'connected' if d.get('online') else 'not connected'}, {d['state'].get('operation', '?')})"
                for d in hcs))
        elif homeconnect_configured():
            lines.append("Home Connect: profiles found, starting (needs Python 3.13, see bridge.log).")
        v = cfg.get("velux") or {}
        if velux_configured(v):
            locks = [d for d in REG.devices.values() if d["type"] == "velux_lock"]
            if VELUX.get("error"):
                lines.append("VELUX: " + str(VELUX["error"]))
            elif locks:
                st = locks[0]["state"]
                lines.append("VELUX connected: " + ("locked (departure mode)" if st.get("locked") else "unlocked")
                             + ("" if st.get("canUnlock") else ", unlocking needs Pair gateway first"))
            else:
                lines.append("VELUX set up, connecting.")
            p = VELUX_PAIR.get("state")
            if p == "press":
                lines.append("VELUX gateway: press the button on the gateway NOW (light blinks). Then reload this page.")
            elif p == "waiting":
                lines.append("VELUX gateway: starting to pair...")
            elif p == "done":
                lines.append("VELUX gateway paired: unlocking works.")
            elif p == "failed":
                lines.append("VELUX pairing failed: " + str(VELUX_PAIR.get("error")))
        r = cfg.get("irobot") or {}
        if irobot_configured(r):
            if IROBOT.get("error"):
                lines.append("iRobot: " + str(IROBOT["error"]))
            elif IROBOT.get("updated"):
                lines.append("iRobot connected: " + ", ".join(
                    f"{v.get('name') or k}: {len(v['favorites'])} favorite(s)" for k, v in IROBOT["robots"].items()))
            else:
                lines.append("iRobot set up, fetching.")
        if PAIRING["until"] > time.time():
            lines.append(f"SmartThings pairing is open ({int(PAIRING['until'] - time.time())} s left): "
                         "the hub pairs at its next poll.")
        elif api_paired(cfg):
            lines.append("SmartThings hub paired: the LAN API only accepts signed requests.")
        else:
            lines.append("NOTE: no SmartThings hub paired yet, the LAN API is open to the whole LAN. "
                         "Press Pair below.")
        return "<br>".join(html.escape(x) for x in lines)

    def plain_from_lan(req) -> bool:
        """Setup page over plain http from another computer: credentials would cross the LAN unencrypted.
        The Docker gateway is the Docker host itself (the traffic never leaves that machine)."""
        try:
            ip = ipaddress.ip_address(req.remote or "")
            loop = ip.is_loopback or (SETUP_GATEWAY is not None and ip == SETUP_GATEWAY)
        except ValueError:
            loop = False
        return https_port is not None and not req.secure and not loop

    async def setup_get(req):
        if not local_only(req):
            return web.Response(status=403, text="setup only from this computer (localhost) or an address in STBRIDGE_SETUP_ALLOW")
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
            return web.Response(status=403, text="setup only from this computer (localhost) or an address in STBRIDGE_SETUP_ALLOW")
        if plain_from_lan(req):
            return web.Response(status=403, text="use https for the setup page")
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
                return web.Response(status=400, text="enter a postcode and house number, an address, or latitude, longitude")
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
        elif section == "api_pair":
            api_token(cfg)
            PAIRING["until"] = time.time() + PAIR_WINDOW
            log.info("[setup] pairing with SmartThings open for %d s", PAIR_WINDOW)
            raise web.HTTPFound("/setup")
        elif section == "api_new_token":
            if cfg.get("api_token"):
                cfg["api_token_prev"] = cfg["api_token"]
            cfg["api_token"] = secrets.token_hex(32)
            cfg["api_paired"] = True
            save_config(cfg)
            PAIRING["until"] = time.time() + PAIR_WINDOW
            log.info("[setup] new API token, pairing open for %d s", PAIR_WINDOW)
            raise web.HTTPFound("/setup")
        elif section == "velux_pair":
            ip = str(form.get("gateway_ip", "")).strip()
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                return web.Response(status=400, text="not a valid IP address")
            src = VELUX.get("source")
            if src is None or src.session is None:
                return web.Response(status=409, text="Set up the VELUX login first (and wait until it is connected).")
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
                                  "name": socket.gethostname(), "paired": api_paired()})

    async def pair(req):
        """Hand the token to a hub, only while "Pair" on the setup page is open (5 minutes, one hub)."""
        if time.time() > PAIRING["until"]:
            return web.json_response({"error": "pairing closed: press Pair on the bridge setup page"},
                                     status=403)
        cfg = load_config()
        tok = api_token(cfg)
        cfg["api_paired"] = True
        save_config(cfg)
        PAIRING["until"] = 0.0
        log.info("[api] hub %s paired", req.remote)
        # a hub that already has a (previous) token only accepts the new one with this proof, so a device on the
        # LAN that pretends to be the bridge cannot slip it a token of its own
        proof = sign(cfg.get("api_token_prev") or tok, tok.encode())
        return web.json_response({"token": tok, "id": bridge_id, "proof": proof})

    async def srp(req):
        """HomeKit pair-setup helper for a paired hub. The request must be signed (checked by guard) and is
        encrypted with a key derived from the shared token, so the HomeKit code never crosses the LAN in the clear."""
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        cfg = load_config()
        if not api_paired(cfg) or not req.headers.get("X-Stbridge-Sig"):
            return web.json_response({"error": "pairing required"}, status=401)
        if time.time() - _SRP_LAST["t"] < 5:
            return web.json_response({"error": "busy, try again in a few seconds"}, status=429)
        _SRP_LAST["t"] = time.time()
        tok = cfg["api_token"]
        try:
            body = await req.json()
            plain = ChaCha20Poly1305(srp_box_key(tok, "request")).decrypt(
                bytes.fromhex(body["nonce"]), bytes.fromhex(body["box"]), None)
            d = json.loads(plain)
            salt, b_pub, pin = bytes.fromhex(d["salt"]), bytes.fromhex(d["B"]), str(d["pin"])
            if not (0 < len(salt) <= 64 and 0 < len(b_pub) <= 384) or len(pin) != 10 or \
                    not (pin[:3] + pin[4:6] + pin[7:]).isdigit() or pin[3] != "-" or pin[6] != "-":
                raise ValueError("bad input")
            t0 = time.time()
            big_a, proof, key = await asyncio.get_running_loop().run_in_executor(
                None, srp_homekit_client, salt, b_pub, pin)
        except Exception as err:  # noqa: BLE001
            log.warning("[api] SRP helper refused a request from %s: %s", req.remote, type(err).__name__)
            return web.json_response({"error": "bad request"}, status=400)
        log.info("[api] SRP step for a HomeKit pairing computed for %s in %.2f s", req.remote, time.time() - t0)
        nonce = secrets.token_bytes(12)
        out = json.dumps({"A": big_a.hex(), "M1": proof.hex(), "K": key.hex()}).encode()
        box = ChaCha20Poly1305(srp_box_key(tok, "response")).encrypt(nonce, out, None)
        return web.json_response({"nonce": nonce.hex(), "box": box.hex()})

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
    app.router.add_post("/stbridge/pair", pair)
    app.router.add_post("/stbridge/srp", srp)
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
        REG.upsert("demo-window", "window", "Demo roof window", {"position": pos["v"], "target": pos["v"],
                                                             "positionState": "stopped"})

    async def dry(cmd, value):
        pass

    ac = {"power": "on", "mode": "heat", "setpoint": 21.0, "temperature": 19.5, "outdoorTemperature": 8.0,
          "fan": "auto", "swing": "off", "supportedModes": ["auto", "cool", "dry", "fan", "heat"],
          "supportedFans": ["quiet", "low", "medium", "high", "auto"],
          "supportedSwings": ["off", "vertical"], "setpointRange": [16.0, 30.0], "model": "DEMO",
          "minHeat": False, "vDir": 2, "vDirCount": 4}
    ac["supportedModes"].append("minheat")

    async def aircon(cmd, value):
        if cmd == "on":
            ac["power"] = "on"
            ac["mode"] = ac.get("last_mode", "heat")
        elif cmd == "off":
            ac["last_mode"], ac["power"], ac["mode"] = ac["mode"], "off", "off"
        elif cmd == "mode":
            ac["power"], ac["mode"] = "on", str(value)
            ac["minHeat"] = ac["mode"] == "minheat"
        elif cmd == "setpoint":
            ac["setpoint"] = float(value)
        elif cmd in ("fan", "swing"):
            ac[cmd] = str(value)
        elif cmd == "vdir" and ac["vDirCount"] and 1 <= int(value) <= ac["vDirCount"]:
            ac["vDir"], ac["swing"] = int(value), "off"
        elif cmd == "minheat":
            ac["minHeat"] = str(value).lower() in ("on", "1", "true")
            ac["power"], ac["mode"] = "on", "minheat" if ac["minHeat"] else "heat"
        if cmd == "off":
            ac["minHeat"] = False
        REG.upsert("demo-aircon", "aircon", "Demo air conditioner", ac, True, aircon)

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
        REG.upsert("demo-car", "car", "Demo car", car, True, demo_car)

    await demo_car("refresh", None)
    velux = {"locked": True, "canUnlock": True}
    REG.upsert("demo-rain", "rain", "Demo rain sensor", {"raining": False}, True, dry)

    async def demo_velux(cmd, value):
        if cmd in ("lock", "unlock"):
            velux["locked"] = cmd == "lock"
        REG.upsert("demo-velux", "velux_lock", "Demo VELUX departure mode", velux, True, demo_velux)

    await demo_velux("refresh", None)
    rain = rain_summary([0.0] * 4 + [0.5, 1.2, 0.8] + [0.0] * 17)
    REG.upsert("demo-rainradar", "rainradar", "Demo rain radar", rain, True, dry)
    dish = {"power": "on", "operation": "run", "door": "closed", "program": "eco50", "remainingTime": 3600,
            "completionTime": _iso_in(3600), "progress": 40, "remoteStartAllowed": False,
            "remoteControlActive": True, "programs": ["eco50", "auto2", "quick45", "machineCare"]}
    oven = {"power": "on", "operation": "run", "door": "closed", "program": "hotAir", "remainingTime": 1200,
            "completionTime": _iso_in(1200), "progress": 20, "remoteStartAllowed": False,
            "remoteControlActive": True, "temperature": 165.0, "setpoint": 180.0, "duration": 0,
            "programs": ["hotAir", "topBottom", "pizza", "micro600"]}

    async def demo_hc(dev, st, cmd, value):
        if cmd in ("on", "off"):
            st["power"] = cmd
        elif cmd == "stop":
            st.update(operation="inactive", remainingTime=None, completionTime=None, progress=None)
        elif cmd in ("pause", "resume"):
            st["operation"] = "pause" if cmd == "pause" else "run"
        elif cmd == "program" and value in st.get("programs", []):
            st["program"] = value
        elif cmd == "setpoint":
            st["setpoint"] = float(value)
        elif cmd == "duration":
            st["duration"] = int(value)
        elif cmd == "start":
            if value in st.get("programs", []):
                st["program"] = value
            st["operation"] = "run"
        REG.upsert(dev, dev.split("-")[1], "Demo " + dev.split("-")[1], st, True,
                   lambda c, v, d=dev, s_=st: demo_hc(d, s_, c, v))

    washer = {"power": "on", "operation": "run", "job": "wash", "remainingTime": 2400,
              "completionTime": _iso_in(2400), "progress": 35, "remoteStartAllowed": True, "course": "Cotton",
              "courseCode": "d0", "program": "cotton", "programs": ["cotton", "eco4060", "quick15", "wool"]}

    async def demo_washer(cmd, value):
        if cmd in ("on", "off"):
            washer["power"] = cmd
        elif cmd in ("run", "start", "resume"):
            washer["operation"] = "run"
        elif cmd == "pause":
            washer["operation"] = "pause"
        if cmd in ("program", "start") and value in washer["programs"]:
            washer["program"] = value
        elif cmd == "stop":
            washer.update(operation="inactive", job="none", remainingTime=None, completionTime=None, progress=None)
        REG.upsert("demo-washer", "washer", "Demo washer", washer, True, demo_washer)

    await demo_washer("refresh", None)
    await demo_hc("demo-dishwasher", dish, "refresh", None)
    await demo_hc("demo-oven", oven, "refresh", None)
    t0 = time.time()
    while True:
        el = int(time.time() - t0) % 180
        reported = {"applianceState": "RUNNING" if el < 120 else "END_OF_CYCLE",
                    "cyclePhase": "DRY" if el < 90 else ("COOL" if el < 120 else "UNAVAILABLE"),
                    "timeToEnd": max(0, 120 - el), "userSelections": {"programUID": "COTTON_PR_COTTONSECO"},
                    "doorState": "CLOSED", "remoteControl": "ENABLED"}
        REG.upsert("demo-dryer", "dryer", "Demo dryer", dryer_state(reported, "connected"), True, dry)
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
               "buienradar": Buienradar, "homeconnect": HomeConnect,
               "samsung": Samsung, "samsungtv": SamsungTV}
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
    if SETUP_GATEWAY is not None:
        log.info("[setup] running in a container: setup page also allowed from the Docker host (%s)", SETUP_GATEWAY)
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
        print("FGLair: not set up yet.")
        return
    import aiohttp
    fg = FGLair(cfg)
    async with aiohttp.ClientSession() as session:
        fg.api = fg._new_api(session)
        await fg.api.async_sign_in()
        await fg._sync()
    print(f"FGLair OK, {len(fg.devices)} air conditioner(s):")
    for d in REG.devices.values():
        if d["type"] == "aircon":
            s = d["state"]
            print(f"  {d['name']} model={s.get('model')} online={d['online']} mode={s.get('mode')} "
                  f"setpoint={s.get('setpoint')} indoor={s.get('temperature')} outdoor={s.get('outdoorTemperature')} "
                  f"fan={s.get('fan')} modes={s.get('supportedModes')}")


async def check_all() -> None:
    for name, fn in (("AEG", check_aeg), ("FGLair", check_fglair)):
        try:
            await fn()
        except Exception as ex:  # noqa: BLE001
            print(f"{name} ERROR: {ex}")


async def check_aeg() -> None:
    """Quick test of the Electrolux credentials in config.json."""
    e = (load_config().get("electrolux") or {})
    if not aeg_configured(e):
        print("AEG: not set up yet.")
        return
    from electrolux_group_developer_sdk.auth.token_manager import TokenManager
    from electrolux_group_developer_sdk.client.appliance_client import ApplianceClient
    cfg = load_config()
    holder = Electrolux(cfg)
    tm = TokenManager(e["access_token"], e["refresh_token"], e["api_key"], on_token_update=holder._on_tokens)
    client = ApplianceClient(tm, external_user_agent=f"StoeckieBridge/{VERSION}")
    apps = await client.get_appliances()
    print(f"AEG OK, {len(apps)} appliance(s):")
    for a in apps:
        st = await client.get_appliance_state(a.applianceId)
        rep = (st.properties or {}).get("reported", {}) or {}
        print(f"  {a.applianceName} type={a.applianceType} connection={st.connectionState} "
              f"status={rep.get('applianceState')} phase={rep.get('cyclePhase')} timeToEnd={rep.get('timeToEnd')}")


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
    p.add_argument("--samsung-probe", metavar="IP", help="check if a Samsung appliance accepts a local session")
    args = p.parse_args()
    if args.samsung_probe:
        sys.exit(samsung_probe(args.samsung_probe))
    setup_logging(args.verbose)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.check:
        asyncio.run(check_all())
    else:
        asyncio.run(serve(args))


if __name__ == "__main__":
    main()
