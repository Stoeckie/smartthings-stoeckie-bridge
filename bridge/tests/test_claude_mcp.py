"""Claude MCP endpoint: address filter, OAuth with password and PKCE, tools via a fake SmartThings."""
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

ANTH = {"X-Forwarded-For": "160.79.105.10"}
EVIL = {"X-Forwarded-For": "8.8.8.8"}


def run(coro):
    return asyncio.run(coro)


def setup_cfg(**extra):
    cfg = sb.load_config()
    cfg["claude"] = {"enabled": True, "password_hash": sb.claude_hash_password("correct horse battery"),
                     "public_url": "https://tvpc.example.ts.net", **extra}
    sb.save_config(cfg)
    if sb.CLAUDE_TOKENS.exists():
        sb.CLAUDE_TOKENS.unlink()
    sb.CLAUDE_STATE["fails"].clear()


async def connect(c):
    r = await c.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                        "client_name": "Claude"}, headers=ANTH)
    assert r.status == 201
    cid = (await r.json())["client_id"]
    ver = "v" * 50
    chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
    q = {"response_type": "code", "client_id": cid, "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
         "code_challenge": chal, "code_challenge_method": "S256", "state": "xyz"}
    r = await c.get("/authorize", params=q)
    assert r.status == 200 and "password" in await r.text()
    r = await c.post("/authorize", data={**q, "password": "wrong one here"}, allow_redirects=False)
    assert r.status == 200 and "Wrong password" in await r.text()
    r = await c.post("/authorize", data={**q, "password": "correct horse battery"}, allow_redirects=False)
    assert r.status == 302
    loc = urlsplit(r.headers["Location"])
    assert loc.netloc == "claude.ai"
    code = parse_qs(loc.query)["code"][0]
    assert parse_qs(loc.query)["state"][0] == "xyz"
    r = await c.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": cid,
                                     "redirect_uri": q["redirect_uri"], "code_verifier": "x" * 50}, headers=ANTH)
    assert r.status == 400  # wrong verifier; the code is used up
    r = await c.post("/authorize", data={**q, "password": "correct horse battery"}, allow_redirects=False)
    code = parse_qs(urlsplit(r.headers["Location"]).query)["code"][0]
    r = await c.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": cid,
                                     "redirect_uri": q["redirect_uri"], "code_verifier": ver}, headers=ANTH)
    assert r.status == 200
    return await r.json()


def test_oauth_and_filter():
    setup_cfg()

    async def main():
        c = TestClient(TestServer(sb.make_claude_app()))
        await c.start_server()
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=ANTH)
        assert r.status == 401 and "resource_metadata" in r.headers["WWW-Authenticate"]
        r = await c.get("/.well-known/oauth-protected-resource", headers=EVIL)
        assert r.status == 403
        r = await c.get("/.well-known/oauth-authorization-server", headers=ANTH)
        assert (await r.json())["token_endpoint"] == "https://tvpc.example.ts.net/token"
        r = await c.post("/register", json={"redirect_uris": ["https://evil.example/cb"]}, headers=ANTH)
        assert r.status == 400
        tok = await connect(c)
        h = {**ANTH, "Authorization": "Bearer " + tok["access_token"]}
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h)
        assert r.status == 200 and len((await r.json())["result"]["tools"]) == 16
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={**EVIL, "Authorization": h["Authorization"]})
        assert r.status == 403
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        assert r.status == 202
        # refresh rotates
        r = await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, headers=ANTH)
        assert r.status == 200
        r = await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}, headers=ANTH)
        assert r.status == 400
        await c.close()
    run(main())


def test_password_lockout():
    setup_cfg()

    async def main():
        c = TestClient(TestServer(sb.make_claude_app()))
        await c.start_server()
        r = await c.post("/register", json={"redirect_uris": ["https://claude.ai/cb"]}, headers=ANTH)
        cid = (await r.json())["client_id"]
        q = {"response_type": "code", "client_id": cid, "redirect_uri": "https://claude.ai/cb",
             "code_challenge": "abc", "code_challenge_method": "S256"}
        sb.CLAUDE_STATE["fails"].extend([sb.time.time()] * 5)
        r = await c.post("/authorize", data={**q, "password": "correct horse battery"}, allow_redirects=False)
        assert r.status == 200 and "Too many" in await r.text()
        await c.close()
    run(main())


def test_tools_with_fake_smartthings():
    setup_cfg(locks="lock")
    devs = [{"deviceId": "d1", "label": "Lock Front Door", "roomId": "r1",
             "components": [{"id": "main", "capabilities": [{"id": "lock"}]}]},
            {"deviceId": "d2", "label": "Light Kitchen Ceiling", "roomId": "r2",
             "components": [{"id": "main", "capabilities": [{"id": "switch"}]}]},
            {"deviceId": "d3", "label": "Light Kitchen Spots", "roomId": "r2",
             "components": [{"id": "main", "capabilities": [{"id": "switch"}]}]}]
    sent = []

    async def fake_call(method, path, body=None, retry=True):
        if path == "/locations":
            return {"items": [{"locationId": "L", "name": "Home"}]}
        if path.endswith("/rooms"):
            return {"items": [{"roomId": "r1", "name": "Hall"}, {"roomId": "r2", "name": "Kitchen"}]}
        if path.startswith("/devices?"):
            return {"items": devs}
        if path.endswith("/commands"):
            sent.append((path, body))
            return {"results": [{"status": "ACCEPTED"}]}
        if path.endswith("/components/main/status"):
            return {"switch": {"switch": {"value": "on"}}}
        return {}
    sb.ST_CLOUD.call = fake_call
    sb.ST_CLOUD.cache = {"t": 0}

    async def main():
        r = await sb.st_tool("get_many", {"room": "kitchen"})
        assert [x["switch"] for x in r] == ["on", "on"]
        try:
            await sb.st_tool("send_command", {"device": "kitchen", "capability": "switch", "command": "on"})
            raise AssertionError("ambiguous name accepted")
        except ValueError as ex:
            assert "2 devices" in str(ex)
        await sb.st_tool("send_command", {"device": "spots", "capability": "switch", "command": "off"})
        await sb.st_tool("send_command", {"device": "front door", "capability": "lock", "command": "lock"})
        try:
            await sb.st_tool("send_command", {"device": "front door", "capability": "lock", "command": "unlock"})
            raise AssertionError("unlock allowed with locks=lock")
        except ValueError:
            pass
        assert [p for p, _ in sent] == ["/devices/d3/commands", "/devices/d1/commands"]
    run(main())
