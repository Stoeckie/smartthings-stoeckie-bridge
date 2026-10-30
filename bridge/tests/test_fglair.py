"""FGLair source against a fake Ayla cloud (no real account needed).

    pip install -r requirements.txt pytest
    python -m pytest tests
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from aiohttp import web

os.environ["STBRIDGE_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from ayla_iot_unofficial import device as ayla_device  # noqa: E402

CAPS = 2047  # all modes, all fan speeds, vertical swing


def prop(name, value, base="integer", ro=False):
    return {"property": {"name": name, "value": value, "base_type": base, "read_only": ro}}


class FakeAyla:
    def __init__(self):
        self.props = {
            "device_name": ("Kantoor", "string", True),
            "device_capabilities": (CAPS, "integer", True),
            "operation_mode": (6, "integer", False),      # heat
            "adjust_temperature": (160, "integer", False),  # 16.0
            "fan_speed": (0, "integer", False),           # quiet
            "af_vertical_swing": (0, "integer", False),
            "af_horizontal_swing": (0, "integer", False),
            "display_temperature": (7000, "integer", True),  # 20.0 C
            "outdoor_temperature": (5800, "integer", True),  # 8.0 C
            "refresh": (0, "integer", False),
        }
        self.sets = []
        self.logins = 0

    def app(self):
        app = web.Application()

        async def sign_in(req):
            body = await req.json()
            if body["user"]["password"] != "goed":
                return web.json_response({"error": {"message": "Invalid email or password"}}, status=401)
            self.logins += 1
            return web.json_response({"access_token": "a", "refresh_token": "r", "expires_in": 86400})

        async def devices(_req):
            return web.json_response([{"device": {
                "dsn": "AC000W000000001", "key": 1, "oem_model": "AP-WA2E", "model": "AY001MHP1",
                "mac": "x", "lan_ip": "192.168.2.50", "product_name": "AC-KANTOOR",
                "connection_status": "Online"}}])

        async def properties(_req):
            return web.json_response([prop(k, v, b, ro) for k, (v, b, ro) in self.props.items()])

        async def datapoint(req):
            name = req.match_info["name"]
            value = (await req.json())["datapoint"]["value"]
            self.sets.append((name, value))
            _, b, ro = self.props[name]
            self.props[name] = (value, b, ro)
            return web.json_response({"value": value})

        app.router.add_post("/users/sign_in.json", sign_in)
        app.router.add_get("/apiv1/devices.json", devices)
        app.router.add_get("/apiv1/dsns/{dsn}/properties.json", properties)
        app.router.add_post("/apiv1/dsns/{dsn}/properties/{name}/datapoints.json", datapoint)
        return app


@pytest.fixture
def fake(monkeypatch, unused_tcp_port_factory=None):
    return FakeAyla()


async def start(fake):
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def patch_urls(monkeypatch, base):
    orig_api = sb.FGLair._new_api

    def new_api(self, session):
        api = orig_api(self, session)
        api.eu_user_field_url = api.eu_ads_url = base
        return api
    monkeypatch.setattr(sb.FGLair, "_new_api", new_api)
    orig_init = ayla_device.Device.__init__

    def dev_init(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.eu_ads_url = base
    monkeypatch.setattr(ayla_device.Device, "__init__", dev_init)


def test_state_and_commands(fake, monkeypatch):
    async def main():
        runner, base = await start(fake)
        patch_urls(monkeypatch, base)
        monkeypatch.setattr(sb.asyncio, "sleep", _fast_sleep)
        sb.REG.devices.clear()
        fg = sb.FGLair({"fglair": {"username": "a@b.nl", "password": "goed", "region": "eu"}})
        import aiohttp
        async with aiohttp.ClientSession() as session:
            fg.api = fg._new_api(session)
            await fg.api.async_sign_in()
            await fg._sync()
            d = sb.REG.devices["fglair-AC000W000000001"]
            s = d["state"]
            assert d["type"] == "aircon" and d["name"] == "Kantoor" and d["online"]
            assert s["power"] == "on" and s["mode"] == "heat" and s["setpoint"] == 16.0
            assert s["fan"] == "quiet" and s["swing"] == "off"
            assert s["temperature"] == 20.0 and s["outdoorTemperature"] == 8.0
            assert set(s["supportedModes"]) == {"auto", "cool", "dry", "fan", "heat"}
            assert "quiet" in s["supportedFans"] and "vertical" in s["supportedSwings"]
            assert s["setpointRange"] == [16.0, 30.0]

            h = sb.REG.handlers["fglair-AC000W000000001"]
            await h("setpoint", 21.5)
            await h("mode", "cool")
            await h("fan", "auto")
            await h("swing", "vertical")
            s = sb.REG.devices["fglair-AC000W000000001"]["state"]
            assert s["setpoint"] == 21.5 and s["mode"] == "cool" and s["fan"] == "auto" and s["swing"] == "vertical"
            await h("off", None)
            assert sb.REG.devices["fglair-AC000W000000001"]["state"]["power"] == "off"
            await h("on", None)
            assert ("operation_mode", 1) in fake.sets
            assert ("adjust_temperature", 215) in fake.sets
            with pytest.raises(Exception):
                await h("mode", "turbo")
        await runner.cleanup()
    asyncio.run(main())


def test_wrong_password(fake, monkeypatch):
    async def main():
        runner, base = await start(fake)
        patch_urls(monkeypatch, base)
        fg = sb.FGLair({"fglair": {"username": "a@b.nl", "password": "fout", "region": "eu"}})
        import aiohttp
        async with aiohttp.ClientSession() as session:
            fg.api = fg._new_api(session)
            with pytest.raises(Exception) as ei:
                await fg.api.async_sign_in()
            assert "Invalid" in str(ei.value)
        await runner.cleanup()
    asyncio.run(main())


def test_configured():
    assert not sb.fglair_configured({})
    assert not sb.fglair_configured({"username": "a", "password": "PLAK_HIER_JE_FGLAIR_WACHTWOORD"})
    assert sb.fglair_configured({"username": "a", "password": "x"})


_real_sleep = asyncio.sleep


async def _fast_sleep(_s):
    await _real_sleep(0)
