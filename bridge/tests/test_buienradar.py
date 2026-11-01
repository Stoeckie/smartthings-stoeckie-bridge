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
    pts = sb.rain_points(52.35, 5.62, 10)
    assert len(pts) == 17 and pts[0] == (52.35, 5.62)
    assert all(abs(p[0] - 52.35) <= 0.1 and abs(p[1] - 5.62) <= 0.16 for p in pts)
    assert sb.parse_location("52,35 5,62") == (52.35, 5.62)
    assert sb.parse_location("nowhere") is None
    assert sb.buienradar_settings("52.35, 5.62", "40", {}) == {"lat": 52.35, "lon": 5.62, "radius_km": 25}
    assert sb.buienradar_settings("", "3", {"lat": 1, "lon": 2}) == {"lat": 1, "lon": 2, "radius_km": 3}


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

        app.router.add_get("/data/raintext", raintext)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(sb, "BUIENRADAR_URL", f"http://127.0.0.1:{port}/data/raintext")

        src = sb.Buienradar({"buienradar": {"lat": 52.35, "lon": 5.62, "radius_km": 5}})
        task = asyncio.create_task(src.run())
        for _ in range(100):
            if "buienradar" in sb.REG.devices:
                break
            await asyncio.sleep(0.02)
        st = sb.REG.devices["buienradar"]["state"]
        assert st["minutesUntilRain"] == 10 and st["minutesOfRain"] == 15 and st["raining"] is False
        assert st["points"] == 17 and len(set(seen)) == 17
        task.cancel()

        # point only: the dry center
        sb.REG.devices.pop("buienradar")
        src = sb.Buienradar({"buienradar": {"lat": 52.35, "lon": 5.62, "radius_km": 0}})
        task = asyncio.create_task(src.run())
        for _ in range(100):
            if "buienradar" in sb.REG.devices:
                break
            await asyncio.sleep(0.02)
        assert sb.REG.devices["buienradar"]["state"]["minutesUntilRain"] == 999
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
            async with s.post(f"http://127.0.0.1:{port}/stbridge/buienradar", json={"location": "x"}) as r:
                assert r.status == 400
            async with s.post(f"http://127.0.0.1:{port}/stbridge/buienradar",
                              json={"location": "52.35, 5.62", "radius": 5}) as r:
                assert r.status == 200
        assert sb.load_config()["buienradar"] == {"lat": 52.35, "lon": 5.62, "radius_km": 5}
        assert started == ["buienradar"]
        await runner.cleanup()

    asyncio.run(main())
