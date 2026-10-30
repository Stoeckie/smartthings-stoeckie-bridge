"""iRobot favorites: cloud answer -> local start commands, served on the LAN API (fake account, no network)."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

BLID = "ABCDEF0123456789"
RAW = [
    {"name": "Hele keuken", "favorite_id": "f2", "display_order": 2,
     "commanddefs": [{"command": "start", "robot_id": BLID, "pmap_id": "P1", "ordered": 1,
                      "regions": [{"region_id": "7", "type": "rid"}], "user_pmapv_id": "V1"}]},
    {"name": "Kamer", "favorite_id": "f1", "display_order": 1,
     "commanddefs": [{"command": "start", "robot_id": BLID, "pmap_id": "P1", "ordered": 1,
                      "regions": [{"region_id": "5", "type": "rid"}], "user_pmapv_id": "V1"}]},
    {"name": "Other robot", "favorite_id": "f3", "commanddefs": [{"command": "start", "robot_id": "XYZ"}]},
    {"name": "Gone", "favorite_id": "f4", "deleted": True, "commanddefs": [{"command": "start", "robot_id": BLID}]},
]


def test_favorites_for():
    favs = sb.favorites_for(RAW, BLID)
    assert [f["name"] for f in favs] == ["Kamer", "Hele keuken"]
    assert favs[0]["params"] == {"pmap_id": "P1", "ordered": 1, "regions": [{"region_id": "5", "type": "rid"}],
                                 "user_pmapv_id": "V1"}


def test_cloud_source_and_api(monkeypatch):
    class FakeRest:
        async def get_favorites(self):
            return RAW

    class FakeAccount:
        robots = {BLID: SimpleNamespace(name="Nounou")}

        def classic_rest(self):
            return FakeRest()

    async def fake_login(*a, **kw):
        return FakeAccount()

    import roombapy_prime
    monkeypatch.setattr(roombapy_prime.CloudAccount, "login", staticmethod(fake_login))

    async def main():
        src = sb.IRobotCloud({"irobot": {"username": "a@b.nl", "password": "x", "country": "NL"}})
        task = asyncio.create_task(src.run())
        for _ in range(50):
            if sb.IROBOT["updated"]:
                break
            await asyncio.sleep(0.02)
        c = TestClient(TestServer(sb.make_app("t", None)))
        await c.start_server()
        r = await c.get(f"/stbridge/irobot/{BLID}")
        data = await r.json()
        assert r.status == 200 and data["name"] == "Nounou" and len(data["favorites"]) == 2
        r = await c.get("/stbridge/irobot/UNKNOWN")
        assert r.status == 404
        await c.close()
        task.cancel()
    asyncio.run(main())
