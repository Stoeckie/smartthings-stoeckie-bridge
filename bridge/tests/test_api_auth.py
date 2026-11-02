"""LAN API: no browser/DNS-rebinding/form posts, pairing window, signed requests and answers, no replays."""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def reset():
    cfg = sb.load_config()
    for k in ("api_token", "api_paired"):
        cfg.pop(k, None)
    sb.save_config(cfg)
    sb.PAIRING["until"] = 0
    sb._SEEN_SIGS.clear()


def signed(token, method, path, body=b""):
    ts = str(int(time.time()))
    sig = sb.sign(token, sb.request_message(method, path, ts, body))
    return {"X-Stbridge-Time": ts, "X-Stbridge-Sig": sig, "Content-Type": "application/json"}, sig


def test_browser_requests_refused():
    reset()

    async def main():
        c = TestClient(TestServer(sb.make_app("t", None)))
        await c.start_server()
        port = c.server.port
        r = await c.get("/stbridge/devices", headers={"Host": f"evil.example:{port}"})
        assert r.status == 403                      # DNS rebinding
        r = await c.get("/stbridge/devices", headers={"Origin": "http://evil.example"})
        assert r.status == 403                      # fetch from a web page
        r = await c.post("/stbridge/devices/x/command", data='{"cmd":"open"}', headers={"Content-Type": "text/plain"})
        assert r.status == 415                      # "simple" cross-site post
        r = await c.get("/stbridge/devices")
        assert r.status == 200                      # not paired yet: open, as before
        await c.close()
    run(main())


def test_pairing_and_signatures():
    reset()

    async def main():
        c = TestClient(TestServer(sb.make_app("t", None)))
        await c.start_server()
        r = await c.post("/stbridge/pair", json={})
        assert r.status == 403                      # pairing not opened
        sb.PAIRING["until"] = time.time() + 60
        r = await c.post("/stbridge/pair", json={})
        token = (await r.json())["token"]
        assert r.status == 200 and len(token) == 64
        r = await c.post("/stbridge/pair", json={})
        assert r.status == 403                      # closes after one hub
        r = await c.get("/stbridge/devices")
        assert r.status == 401                      # paired: unsigned refused
        r = await c.get("/stbridge/info")
        assert r.status == 200 and (await r.json())["paired"] is True
        h, sig = signed(token, "GET", "/stbridge/devices")
        r = await c.get("/stbridge/devices", headers=h)
        body = await r.read()
        assert r.status == 200
        assert r.headers["X-Stbridge-Sig"] == sb.sign(token, sig.encode() + b"\n" + body)
        r = await c.get("/stbridge/devices", headers=h)
        assert r.status == 401                      # replay
        h, _ = signed("0" * 64, "GET", "/stbridge/devices")
        r = await c.get("/stbridge/devices", headers=h)
        assert r.status == 401                      # wrong token
        b = json.dumps({"enabled": False}).encode()
        h, _ = signed(token, "POST", "/stbridge/buienradar", b"{}")
        r = await c.post("/stbridge/buienradar", data=b, headers=h)
        assert r.status == 401                      # body changed after signing
        await c.close()
    run(main())
    reset()
