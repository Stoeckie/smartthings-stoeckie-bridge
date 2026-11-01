"""Setup page only for this pc, addressed by IP/localhost, not posted from another site."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def _client():
    client = TestClient(TestServer(sb.make_app("t", None)))
    await client.start_server()
    return client


def test_setup_guards():
    async def main():
        c = await _client()
        port = c.server.port
        r = await c.get("/setup")
        assert r.status == 200 and r.headers["X-Frame-Options"] == "DENY"
        r = await c.get("/setup", headers={"Host": f"evil.example:{port}"})
        assert r.status == 403  # DNS rebinding
        r = await c.post("/setup", data={"section": "fglair", "username": "a", "password": "b"},
                         headers={"Origin": "http://evil.example"}, allow_redirects=False)
        assert r.status == 403  # cross-site form post
        r = await c.post("/setup", data={"section": "fglair", "username": "a@b.nl", "password": "b"},
                         headers={"Origin": f"http://127.0.0.1:{port}"}, allow_redirects=False)
        assert r.status == 302
        r = await c.get("/stbridge/info")
        assert r.status == 200
        await c.close()
    run(main())


def test_poll_settings_from_setup_page():
    async def main():
        sb.save_config({})
        c = await _client()
        port = c.server.port
        for section, value, want in (("fglair_settings", "5", 30), ("velux_settings", "120", 120)):
            r = await c.post("/setup", data={"section": section, "poll_seconds": value},
                             headers={"Origin": f"http://127.0.0.1:{port}"}, allow_redirects=False)
            assert r.status == 302
            assert sb.load_config()[section.split("_")[0]]["poll_seconds"] == want
        await c.close()
    run(main())


def test_https_certificate():
    d = Path(tempfile.mkdtemp())
    ctx, fp = sb.setup_tls(d)
    assert ctx is not None and len(fp) == 95  # SHA-256 fingerprint AA:BB:...
    assert (d / "setup-cert.pem").exists() and (d / "setup-key.pem").exists()
    ctx2, fp2 = sb.setup_tls(d)  # reused, not regenerated
    assert fp2 == fp
