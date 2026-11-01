"""Buienradar rain forecast: parsing, summary, radius points, and the source against a fake Buienradar."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp import web  # noqa: E402


def test_parse_and_summary():
    rates = sb.parse_raintext("000|10:35\n077|10:40\n109|10:45\n141|10:50\n000|10:55\n")
    assert rates == [0.0, 0.1, 1.0, 10.0, 0.0]
    s = sb.rain_summary(rates)
    assert (s["raining"], s["minutesUntilRain"], s["minutesOfRain"], s["peakRate"]) == (False, 5, 15, 10.0)
    assert s["intensity"] == "possiblePrecipitation" and s["summary"].startswith("Regen over 5 min")
    s = sb.rain_summary([2.0, 3.0, 0.0])
    assert (s["raining"], s["minutesUntilRain"], s["minutesOfRain"], s["intensity"]) == (True, 0, 10, "light")
    assert s["summary"].startswith("Regent nu")
    s = sb.rain_summary([0.0] * 24)
    assert (s["raining"], s["minutesUntilRain"], s["minutesOfRain"]) == (False, 999, 0)
    assert s["summary"] == "Droog, geen regen verwacht (2 uur)"
    s = sb.rain_summary([0.0] * 20 + [1.0] * 4)
    assert s["minutesOfRain"] == 20 and "20+ min" in s["summary"]  # still raining at the end of the forecast


def test_points_and_settings():
    assert sb.rain_points(52.35, 5.62, 0) == [(52.35, 5.62)]
    pts = sb.rain_points(52.35, 5.62, 10000)
    assert len(pts) == 17 and pts[0] == (52.35, 5.62)
    assert all(abs(p[0] - 52.35) <= 0.1 and abs(p[1] - 5.62) <= 0.16 for p in pts)
    assert len(sb.rain_points(52.35, 5.62, 500)) <= 9  # small radius: one ring, rounded points merge
    assert sb.parse_location("52,35 5,62") == (52.35, 5.62)
    assert sb.parse_location("nowhere") is None
    assert sb.normalize_postcode(" 1012 js") == "1012JS" and sb.normalize_postcode("12345") is None
    assert sb.house_number("64a") == 64 and sb.house_number("") is None
    s = sb.buienradar_settings({"postcode": "1012 js", "housenumber": "1", "radius_m": "40000"}, {})
    assert (s["postcode"], s["housenumber"], s["radius_m"], s["lat"]) == ("1012JS", 1, 25000, None)
    s = sb.buienradar_settings({"location": "50.85, 4.35", "radius_m": "500"}, s)  # coordinates win
    assert (s["lat"], s["lon"], s["radius_m"]) == (50.85, 4.35, 500)
    s = sb.buienradar_settings({"location": ""}, dict(s, lat=None))
    assert s["location"] is None and s["lat"] is None and s["postcode"] == "1012JS"
    assert sb.buienradar_settings({}, {"radius_km": 2})["radius_m"] == 2000  # old config


def test_source(monkeypatch):
    seen = []

    async def main():
        app = web.Application()

        async def raintext(req):
            lat, lon = float(req.query["lat"]), float(req.query["lon"])
            seen.append((lat, lon))
            # only the point north of the center sees rain (in 10 minutes)
            wet = lat > 52.36 and abs(lon - 5.62) < 0.001
            rows = [("120" if wet and 2 <= i < 5 else "000") + f"|10:{i * 5:02d}" for i in range(24)]
            return web.Response(text="\r\n".join(rows))

        async def pdok(req):
            fq = req.query.getall("fq")
            if "postcode:1012JS" in fq and "huisnummer:1" in fq:
                return web.json_response({"response": {"docs": [
                    {"weergavenaam": "Dam 1, 1012JS Amsterdam", "centroide_ll": "POINT(5.62 52.35)"}]}})
            return web.json_response({"response": {"docs": []}})

        app.router.add_get("/data/raintext", raintext)
        app.router.add_get("/pdok", pdok)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(sb, "BUIENRADAR_URL", f"http://127.0.0.1:{port}/data/raintext")
        monkeypatch.setattr(sb, "PDOK_URL", f"http://127.0.0.1:{port}/pdok")

        async def wait_for(pred):
            for _ in range(200):
                d = sb.REG.devices.get("buienradar")
                if d and pred(d["state"]):
                    return d["state"]
                await asyncio.sleep(0.02)
            raise AssertionError(sb.REG.devices.get("buienradar"))

        # switched on without a location: a device that asks for it
        sb.save_config({"buienradar": {"enabled": True}})
        src = sb.Buienradar(sb.load_config())
        task = asyncio.create_task(src.run())
        st = await wait_for(lambda s: "postcode" in s)
        assert st["summary"].startswith("Geen locatie")
        # settings from SmartThings: postcode + house number + radius in meters
        h = sb.REG.handlers["buienradar"]
        await h("settings", {"postcode": "1012JS", "housenumber": "1", "location": "", "radius_m": 5000,
                             "threshold": 0.1})
        st = await wait_for(lambda s: "minutesUntilRain" in s)
        assert st["place"] == "Dam 1, 1012JS Amsterdam" and st["radiusM"] == 5000
        assert st["minutesUntilRain"] == 10 and st["minutesOfRain"] == 15 and st["raining"] is False
        assert st["points"] == 17
        assert sb.load_config()["buienradar"]["lat"] == 52.35  # looked up once, stored
        # unknown address
        await h("settings", {"postcode": "9999ZZ", "housenumber": "1"})
        st = await wait_for(lambda s: "niet gevonden" in s.get("summary", ""))
        task.cancel()

        # point only: the dry center
        sb.REG.devices.pop("buienradar")
        src = sb.Buienradar({"buienradar": {"lat": 52.35, "lon": 5.62, "radius_m": 0}})
        task = asyncio.create_task(src.run())
        st = await wait_for(lambda s: "minutesUntilRain" in s)
        assert st["minutesUntilRain"] == 999
        task.cancel()
        await runner.cleanup()

    asyncio.run(main())


def test_settings_endpoint():
    async def main():
        started = []
        sb.save_config({})
        app = sb.make_app("t1", on_setup=started.append)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}/stbridge/buienradar", json={"enabled": True}) as r:
                assert r.status == 200
            assert sb.load_config()["buienradar"]["enabled"] is True and started == ["buienradar"]
            async with s.post(f"http://127.0.0.1:{port}/stbridge/buienradar", json={"enabled": True}) as r:
                assert r.status == 200
            assert started == ["buienradar"]  # no change, no restart
            async with s.post(f"http://127.0.0.1:{port}/stbridge/buienradar", json={"enabled": False}) as r:
                assert r.status == 200
        assert sb.load_config()["buienradar"]["enabled"] is False and len(started) == 2
        await runner.cleanup()

    asyncio.run(main())
