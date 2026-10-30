"""Škoda public API source against a fake API (no network, no real key)."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp import web  # noqa: E402

VIN = "TMBJC9NY0RF000001"
VEHICLE = {"vehicle": {
    "vin": VIN, "name": "Kodiaq", "licensePlate": "X-123-YZ",
    "status": {"overall": {"doorsLocked": "YES", "locked": "YES", "doors": "CLOSED", "windows": "CLOSED",
                           "lights": "OFF"}, "detail": {}},
    "fuelStatus": {"carType": "HYBRID", "totalRangeInKm": 640,
                   "primaryEngineRange": {"engineType": "GASOLINE", "currentFuelLevelInPercent": 55,
                                          "remainingRangeInKm": 600},
                   "secondaryEngineRange": {"engineType": "ELECTRIC", "currentSoCInPercent": 80,
                                            "remainingRangeInKm": 40}},
    "odometer": {"mileageInKm": 41234},
    "airConditioning": {"state": "OFF", "targetTemperature": {"value": 21.0, "unit": "CELSIUS"}},
    "charging": {"isVehicleInSavedLocation": True,
                 "status": {"state": "CHARGING", "chargePowerInKw": 3.6, "remainingTimeToFullyChargedInMinutes": 50,
                            "plugConnectionState": "CONNECTED",
                            "battery": {"stateOfChargeInPercent": 81, "remainingCruisingRangeInMeters": 41000}},
                 "settings": {"targetStateOfChargeInPercent": 100}}},
    "errors": []}


def test_car_state():
    s = sb.car_state(VEHICLE["vehicle"])
    assert (s["battery"], s["electricRange"], s["fuelLevel"], s["totalRange"]) == (81, 41, 55, 640)
    assert (s["charging"], s["plug"], s["atHome"], s["climate"], s["doors"]) == \
        ("CHARGING", "CONNECTED", True, "OFF", "CLOSED")
    assert sb.car_state({})["battery"] is None  # missing parts stay None


def test_source_and_commands(monkeypatch):
    calls = []

    async def main():
        app = web.Application()

        async def get_vehicle(req):
            calls.append(("GET", req.path, req.headers.get("X-API-Key")))
            return web.json_response(VEHICLE, headers={"X-API-Key-Expires-At": "2027-01-01T00:00:00Z"})

        async def post(req):
            body = await req.text()
            calls.append((req.method, req.path, body))
            return web.Response(status=202)

        app.router.add_get(f"/api/v1/vehicles/{VIN}", get_vehicle)
        app.router.add_post(f"/api/v1/vehicles/{VIN}/{{rest:.*}}", post)
        app.router.add_put(f"/api/v1/vehicles/{VIN}/{{rest:.*}}", post)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(sb, "SKODA_API", f"http://127.0.0.1:{port}/api/v1")

        src = sb.Skoda({"skoda": {"api_key": "k1", "vin": VIN.lower(), "climate_temp": 20}})
        task = asyncio.create_task(src.run())
        for _ in range(100):
            if "skoda-" + VIN in sb.REG.devices:
                break
            await asyncio.sleep(0.02)
        d = sb.REG.devices["skoda-" + VIN]
        assert d["type"] == "car" and d["name"] == "Kodiaq" and d["state"]["battery"] == 81
        assert d["state"]["keyExpires"] == "2027-01-01T00:00:00Z"
        assert calls[0][2] == "k1"
        h = sb.REG.handlers["skoda-" + VIN]
        await h("off", None)
        await h("climate_on", None)
        await h("limit", 80)
        await h("settings", {"poll_minutes": 15, "climate_temp": 22})
        assert src.POLL == 900 and src.k["climate_temp"] == 22.0
        assert sb.load_config()["skoda"]["poll_minutes"] == 15
        posted = [(m, p, b) for m, p, b in calls if m != "GET"]
        assert posted[0][1].endswith("/charging/stop")
        assert posted[1][1].endswith("/air-conditioning/start") and json.loads(posted[1][2])["targetTemperature"]["value"] == 20
        assert posted[2][0] == "PUT" and json.loads(posted[2][2]) == {"targetStateOfChargeInPercent": 80}
        task.cancel()
        await runner.cleanup()
    asyncio.run(main())


def test_settings_validation():
    assert sb.skoda_settings("3", "35", {}) == {"poll_minutes": 5, "climate_temp": 29.0}
    assert sb.skoda_settings("15", "20,6", {}) == {"poll_minutes": 15, "climate_temp": 20.5}
    assert sb.skoda_settings("", "x", {"poll_minutes": 12, "climate_temp": 22.0}) == {"poll_minutes": 12, "climate_temp": 22.0}
