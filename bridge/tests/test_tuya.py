"""Tuya (Smart Life), local: DP mapping by function code, scale, state, commands, polling and offline, cloud
fetch and scan with a fake tinytuya (no network, no devices)."""
import asyncio
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

KEY = "a1b2c3d4e5f6g7h8"          # a local key: must never show up in a log or on the setup page
SECRET = "S3cr3tS3cr3tS3cr3tS3cr3tS3cr3t00"

# a floor heating thermostat (category wk) as tinytuya's Cloud.getdevices(include_map=True) gives it
THERMO_MAP = {
    "1": {"code": "switch", "type": "Boolean", "values": {}},
    "2": {"code": "temp_set", "type": "Integer", "values": {"unit": "°C", "min": 50, "max": 350, "scale": 1, "step": 5}},
    "3": {"code": "temp_current", "type": "Integer", "values": {"unit": "°C", "min": -300, "max": 1000, "scale": 1}},
    "4": {"code": "mode", "type": "Enum", "values": {"range": ["auto", "manual", "holiday"]}},
    "5": {"code": "eco", "type": "Boolean", "values": {}},
    "6": {"code": "child_lock", "type": "Boolean", "values": {}},
    "19": {"code": "upper_temp", "type": "Integer", "values": {"min": 50, "max": 350, "scale": 1, "step": 5}},
    "26": {"code": "lower_temp", "type": "Integer", "values": {"min": 50, "max": 350, "scale": 1, "step": 5}},
    "36": {"code": "valve_state", "type": "Enum", "values": {"range": ["open", "close"]}},
    "102": {"code": "floor_temp", "type": "Integer", "values": {"min": 0, "max": 990, "scale": 1}},
}
THERMO_DPS = {"1": True, "2": 215, "3": 198, "4": "manual", "5": False, "6": False, "19": 300, "26": 100,
              "36": "open", "102": 243}
# an infrared panel (category qn): on/off, room temperature, set temperature in whole degrees
PANEL_MAP = {
    "1": {"code": "switch", "type": "Boolean", "values": {}},
    "2": {"code": "temp_set", "type": "Integer", "values": {"min": 5, "max": 40, "scale": 0, "step": 1}},
    "3": {"code": "temp_current", "type": "Integer", "values": {"min": -20, "max": 60, "scale": 0}},
}


def roles(mapping):
    return sb.tuya_roles(sb.tuya_functions(mapping))


def test_mapping_by_function_code():
    r = roles(THERMO_MAP)
    assert r["switch"]["dp"] == "1" and r["temp_set"]["dp"] == "2" and r["temp_floor"]["dp"] == "102"
    assert r["child_lock"]["dp"] == "6" and r["work_state"]["code"] == "valve_state"
    # other DP numbers, other codes for the same thing: still found by code
    other = {"16": {"code": "temp_set", "type": "Integer", "values": {"scale": 0}},
             "24": {"code": "temp_current", "type": "Integer", "values": {"scale": 0}},
             "101": {"code": "temp_current_external", "type": "Integer", "values": {"scale": 1}},
             "40": {"code": "lock", "type": "Boolean", "values": {}}}
    r = roles(other)
    assert r["temp_set"]["dp"] == "16" and r["temp_floor"]["dp"] == "101" and r["child_lock"]["dp"] == "40"
    assert "switch" not in r
    # the cloud specification format (status and functions lists, values as JSON text)
    spec = {"functions": [{"code": "temp_set", "dp_id": 2, "type": "Integer",
                           "values": '{"unit":"℃","min":50,"max":350,"scale":1,"step":5}'}],
            "status": [{"code": "temp_current", "dp_id": 3, "type": "Integer", "values": '{"scale":1}'},
                       {"code": "temp_set", "dp_id": 2, "type": "Integer", "values": "{}"}]}
    f = sb.tuya_functions(spec)
    assert f["temp_set"]["dp"] == "2" and f["temp_set"]["values"]["scale"] == 1 and f["temp_current"]["dp"] == "3"
    # no mapping at all: DP 1 is on/off; a "dps" override in config sets the rest
    r = sb.tuya_roles({}, {"temp_set": {"dp": 2, "scale": 1, "min": 50, "max": 350, "step": 5}, "bogus": 9})
    assert r["switch"]["dp"] == "1" and r["temp_set"]["dp"] == "2" and r["temp_set"]["values"]["scale"] == 1
    assert "bogus" not in r


def test_scale_and_state():
    r = roles(THERMO_MAP)
    t = r["temp_set"]
    assert sb.tuya_decode_temp(215, t) == 21.5                      # scale 1: value / 10
    assert sb.tuya_decode_temp(20, roles(PANEL_MAP)["temp_set"]) == 20.0
    assert sb.tuya_decode_temp(True, t) is None and sb.tuya_decode_temp("x", t) is None
    st = sb.tuya_state("tuya_thermostat", r, THERMO_DPS)
    assert st["power"] == "on" and st["temperature"] == 19.8 and st["setpoint"] == 21.5
    assert st["floorTemperature"] == 24.3 and st["hasFloor"] and st["hasChildLock"] and st["childLock"] is False
    # range from the spec (5 to 35), narrowed by the limits set on the thermostat (10 to 30), step 0.5
    assert st["setpointRange"] == [10.0, 30.0] and st["setpointStep"] == 0.5
    assert st["mode"] == "heat" and st["supportedModes"] == ["off", "auto", "heat", "away", "eco"]
    assert st["operatingState"] == "heating" and st["operatingStateFrom"] == "device"
    assert sb.tuya_state("tuya_thermostat", r, dict(THERMO_DPS, **{"36": "close"}))["operatingState"] == "idle"
    assert sb.tuya_state("tuya_thermostat", r, dict(THERMO_DPS, **{"5": True}))["mode"] == "eco"
    assert sb.tuya_state("tuya_thermostat", r, dict(THERMO_DPS, **{"4": "auto"}))["mode"] == "auto"
    off = sb.tuya_state("tuya_thermostat", r, dict(THERMO_DPS, **{"1": False}))
    assert off["mode"] == "off" and off["operatingState"] == "idle"
    # Fahrenheit thermostat (temp_unit_convert f): shown in °C
    rf = roles(dict(THERMO_MAP, **{"20": {"code": "temp_unit_convert", "type": "Enum", "values": {"range": ["c", "f"]}}}))
    stf = sb.tuya_state("tuya_thermostat", rf, dict(THERMO_DPS, **{"20": "f", "2": 716, "3": 680}))
    assert stf["setpoint"] == 22.0 and stf["temperature"] == 20.0
    # the panel: a heater with a set temperature, no floor or child lock
    rp = roles(PANEL_MAP)
    assert sb.tuya_kind({"category": "qn"}, rp) == "tuya_heater"
    assert sb.tuya_kind({"category": "wk"}, r) == "tuya_thermostat"
    assert sb.tuya_kind({}, r) == "tuya_thermostat" and sb.tuya_kind({"type": "heater"}, r) == "tuya_heater"
    sp = sb.tuya_state("tuya_heater", rp, {"1": False, "2": 20, "3": 18})
    assert sp == {"power": "off", "temperature": 18.0, "setpoint": 20.0, "setpointRange": [5.0, 40.0],
                  "setpointStep": 1.0, "hasSetpoint": True}
    plain = sb.tuya_state("tuya_heater", roles({"1": {"code": "switch", "type": "Boolean"}}), {"1": True})
    assert plain == {"power": "on", "hasSetpoint": False}


def test_command_encoding():
    r = roles(THERMO_MAP)
    dps = dict(THERMO_DPS)
    assert sb.tuya_command("setpoint", 22.3, r, dps) == {"2": 225}      # rounded to the 0.5 step, tenths
    assert sb.tuya_command("setpoint", 22.2, r, dps) == {"2": 220}
    assert sb.tuya_command("setpoint", 40, r, dps) == {"2": 300}        # clamped to the upper limit 30 °C
    assert sb.tuya_command("setpoint", 2, r, dps) == {"2": 100}         # and the lower limit 10 °C
    assert sb.tuya_command("on", None, r, dps) == {"1": True}
    assert sb.tuya_command("off", None, r, dps) == {"1": False}
    assert sb.tuya_command("mode", "off", r, dps) == {"1": False}
    assert sb.tuya_command("mode", "auto", r, dps) == {"4": "auto"}
    assert sb.tuya_command("mode", "away", r, dps) == {"4": "holiday"}
    assert sb.tuya_command("mode", "eco", r, dps) == {"5": True}
    # from eco back to heat: eco off, manual mode; from off: also switched on
    assert sb.tuya_command("mode", "heat", r, dict(dps, **{"5": True, "1": False})) == {"4": "manual", "5": False,
                                                                                       "1": True}
    assert sb.tuya_command("child_lock", True, r, dps) == {"6": True}
    assert sb.tuya_command("child_lock", "false", r, dps) == {"6": False}
    with pytest.raises(ValueError):
        sb.tuya_command("mode", "cool", r, dps)
    with pytest.raises(ValueError):
        sb.tuya_command("setpoint", "warm", r, dps)
    with pytest.raises(ValueError):
        sb.tuya_command("fan", 1, r, dps)
    rp = roles(PANEL_MAP)
    assert sb.tuya_command("setpoint", 21.6, rp, {}) == {"2": 22}       # whole degrees
    with pytest.raises(ValueError):
        sb.tuya_command("child_lock", True, rp, {})                     # the panel has no child lock


class FakeDevice:
    """tinytuya.Device stand-in: answers status from a shared dps dict, or errors like tinytuya does."""
    all: dict = {}

    def __init__(self, dev_id, address=None, local_key="", version=3.3, persist=False, **kw):
        self.id, self.address, self.key, self.version, self.persist = dev_id, address, local_key, version, persist
        self.closed = False
        FakeDevice.all[dev_id] = self

    def status(self):
        fake = FAKE[self.id]
        if fake.get("down"):
            return {"Error": "Network Error: Device Unreachable", "Err": "905", "Payload": None}
        return {"devId": self.id, "dps": dict(fake["dps"])}

    def set_value(self, dp, value):
        FAKE[self.id]["sent"].append({str(dp): value})
        FAKE[self.id]["dps"][str(dp)] = value
        return {"dps": {str(dp): value}}

    def set_multiple_values(self, data):
        FAKE[self.id]["sent"].append(dict(data))
        FAKE[self.id]["dps"].update(data)
        return {"dps": dict(data)}

    def close(self):
        self.closed = True


FAKE: dict = {}


@pytest.fixture
def fake_tuya(monkeypatch):
    mod = types.ModuleType("tinytuya")
    mod.Device = FakeDevice
    scans = []

    def devices(**kw):
        scans.append(kw)
        return {"bf01thermo0000000001": {"ip": "192.168.1.71", "version": "3.4", "gwId": "bf01thermo0000000001"},
                "bf02panel00000000002": {"ip": "192.168.1.72", "version": "3.3", "gwId": "bf02panel00000000002"}}
    mod.scanner = types.SimpleNamespace(devices=devices)

    class Cloud:
        def __init__(self, apiRegion=None, apiKey=None, apiSecret=None, **kw):
            self.error = None if apiSecret == SECRET else {"Error": "x", "Payload": "Cloud _gettoken() failed"}
            self.region = apiRegion

        def getdevices(self, verbose=False, include_map=False):
            assert include_map
            return [{"id": "bf01thermo0000000001", "name": "Bijgebouw Vloerverwarming", "key": KEY,
                     "category": "wk", "product_id": "p1", "mapping": THERMO_MAP, "last_ip": "1.2.3.4"},
                    {"id": "bf02panel00000000002", "name": "Office Desk Heating Panel", "key": KEY[::-1],
                     "category": "qn", "product_id": "p2", "mapping": PANEL_MAP},
                    {"id": "bf03sub", "name": "Zigbee thing", "key": "", "sub": True}]
    mod.Cloud = Cloud
    monkeypatch.setitem(sys.modules, "tinytuya", mod)
    FAKE.clear()
    FAKE["bf01thermo0000000001"] = {"dps": dict(THERMO_DPS), "sent": []}
    FAKE["bf02panel00000000002"] = {"dps": {"1": False, "2": 20, "3": 18}, "sent": []}
    sb.save_config({})
    for k in [k for k in sb.REG.devices if k.startswith("tuya-")]:
        sb.REG.devices.pop(k)
    return scans


def test_cloud_fetch_scan_poll_and_commands(fake_tuya, caplog):
    caplog.set_level(logging.DEBUG)
    sb.save_config({"tuya": {"api_key": "AccessId12345678", "api_secret": SECRET, "region": "eu",
                             "devices": [{"id": "bf02panel00000000002", "name": "Office Desk Heating Panel",
                                          "ip": "auto", "version": "auto"}]}})

    async def main():
        tl = sb.TuyaLocal(sb.load_config())
        assert await tl.fetch_cloud() == 3
        cfg = sb.load_config()["tuya"]
        assert len(cfg["cloud_devices"]) == 3 and "fetch" not in cfg and cfg["fetched"]
        tl.t = cfg
        tl.devices = sb.tuya_devices(cfg)
        assert [d["name"] for d in tl.devices] == ["Bijgebouw Vloerverwarming", "Office Desk Heating Panel"]
        await tl.discover(force=True)                     # UDP scan: ips and protocol versions, kept in config
        assert fake_tuya and fake_tuya[0]["wantids"] and fake_tuya[0]["poll"] is False
        assert sb.load_config()["tuya"]["ips"]["bf01thermo0000000001"] == {"ip": "192.168.1.71", "version": "3.4"}
        thermo, panel = tl.devices
        for d in tl.devices:
            tl.add(d)
            await tl.poll_once(d)
        dev = FakeDevice.all["bf01thermo0000000001"]
        assert dev.address == "192.168.1.71" and dev.version == 3.4 and dev.persist and dev.key == KEY
        t = sb.REG.devices["tuya-bf01thermo0000000001"]
        assert t["type"] == "tuya_thermostat" and t["online"] and t["state"]["setpoint"] == 21.5
        assert t["state"]["floorTemperature"] == 24.3
        p = sb.REG.devices["tuya-bf02panel00000000002"]
        assert p["type"] == "tuya_heater" and p["state"]["power"] == "off" and p["state"]["setpoint"] == 20.0
        assert "temp_set (DP 2)" in sb.REG.info["tuya-bf01thermo0000000001"]
        # commands from SmartThings: one DP or several in one message, the state follows right away
        h = sb.REG.handlers["tuya-bf01thermo0000000001"]
        await h("setpoint", 22.3)
        assert FAKE["bf01thermo0000000001"]["sent"][-1] == {"2": 225} and t["state"]["setpoint"] == 22.5
        await h("mode", "eco")
        await h("mode", "auto")
        assert FAKE["bf01thermo0000000001"]["sent"][-1] == {"4": "auto", "5": False}
        assert t["state"]["mode"] == "auto"
        await h("child_lock", True)
        assert t["state"]["childLock"] is True
        await sb.REG.handlers["tuya-bf02panel00000000002"]("on", None)
        assert p["state"]["power"] == "on"
        with pytest.raises(ValueError):
            await h("mode", "cool")
        # offline only after 3 failed reads in a row; the wait grows; back online at the next answer
        FAKE["bf01thermo0000000001"]["down"] = True
        waits = [await tl.poll_once(thermo) for _ in range(3)]
        assert t["online"] is False and waits[0] < waits[1] < waits[2] <= 300
        assert dev.closed                                 # the connection is made again after an error
        FAKE["bf01thermo0000000001"]["down"] = False
        assert 26 <= await tl.poll_once(thermo) <= 34 and t["online"] is True

    asyncio.run(main())
    text = caplog.text
    assert KEY not in text and KEY[::-1] not in text and SECRET not in text
    for line in sb.REG.info.values():
        assert KEY not in line


def test_st_key_keeps_the_smartthings_device(fake_tuya):
    """Paired again in the app: new Tuya id, same SmartThings device through "st_key"."""
    sb.save_config({"tuya": {"devices": [{"id": "bf03newid00000000003", "key": KEY, "name": "Panel",
                                          "ip": "192.168.1.72", "version": "3.3", "st_key": "bf02panel00000000002"}]}})

    async def main():
        tl = sb.TuyaLocal(sb.load_config())
        d = tl.devices[0]
        tl.add(d)
        assert "tuya-bf02panel00000000002" in sb.REG.devices and "tuya-bf03newid00000000003" not in sb.REG.devices
        assert tl.dev_id("bf03newid00000000003") == "tuya-bf02panel00000000002"
        assert tl.dev_id("other") == "tuya-other"

    asyncio.run(main())
    for k in [k for k in sb.REG.devices if k.startswith("tuya-")]:
        sb.REG.devices.pop(k)


def test_cloud_fetch_refused(fake_tuya, caplog):
    sb.save_config({"tuya": {"api_key": "AccessId12345678", "api_secret": "wrongwrongwrong1", "region": "eu",
                             "fetch": True}})

    async def main():
        tl = sb.TuyaLocal(sb.load_config())
        assert await tl.fetch_cloud() == 0
    asyncio.run(main())
    assert "refused" in sb.TUYA["error"] and "fetch" not in sb.load_config()["tuya"]
    assert "wrongwrongwrong1" not in caplog.text
    sb.TUYA["error"] = None


def test_setup_page_tuya(fake_tuya):
    started = []

    async def main():
        sb.save_config({})
        c = TestClient(TestServer(sb.make_app("t", started.append)))
        await c.start_server()
        port = c.server.port
        hdr = {"Origin": f"http://127.0.0.1:{port}"}
        r = await c.post("/setup", data={"section": "tuya_fetch"}, headers=hdr, allow_redirects=False)
        assert r.status == 409                                   # nothing to fetch with yet
        r = await c.post("/setup", data={"section": "tuya", "api_key": "abc", "api_secret": SECRET, "region": "eu"},
                         headers=hdr, allow_redirects=False)
        assert r.status == 400
        r = await c.post("/setup", data={"section": "tuya", "api_key": "AccessId12345678", "api_secret": SECRET,
                                         "region": "us"}, headers=hdr, allow_redirects=False)
        assert r.status == 302 and started[-1] == "tuya"
        t = sb.load_config()["tuya"]
        assert t["api_secret"] == SECRET and t["region"] == "us" and t["fetch"] is True
        r = await c.post("/setup", data={"section": "tuya", "api_key": "AccessId12345678", "api_secret": SECRET,
                                         "region": "eu"}, headers={"Origin": "http://evil.example"},
                         allow_redirects=False)
        assert r.status == 403                                   # same guards as the other secrets
        sb.REG.upsert("tuya-x", "tuya_thermostat", "Vloer", {"power": "on", "temperature": 19.8, "setpoint": 21.5,
                                                             "mode": "heat"})
        sb.REG.info["tuya-x"] = "192.168.1.71, protocol 3.4"
        r = await c.get("/setup")
        page = await r.text()
        assert "Access Secret" in page and 'type="password"' in page
        assert SECRET not in page and "AccessId12345678" not in page and "Ac...78" in page
        assert '<option value="us" selected>' in page
        assert "Vloer: online, on, 19.8 °C, set 21.5 °C, heat; 192.168.1.71, protocol 3.4" in page
        sb.REG.devices.pop("tuya-x")
        await c.close()
    asyncio.run(main())
