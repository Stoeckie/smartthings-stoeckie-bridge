"""VELUX ACTIVE departure mode: fake Netatmo/VELUX cloud and a fake gateway pairing port (no network, no account)."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
from aiohttp import web  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import x25519  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402

GW_KEY = bytes(range(32))


def test_scenario_hash():
    key = base64.b64encode(b"k" * 16).decode()
    want = base64.b64encode(hmac.new(b"k" * 16, b"scenariohome123450gw1", hashlib.sha512).digest()).decode()
    assert sb.velux_scenario_hash(key, "home", 12345, 0, "gw1") == want.replace("+", "-").replace("/", "_")
    assert sb.velux_scenario_hash(key, "home", 12345, 1, "gw1") != sb.velux_scenario_hash(key, "home", 12345, 0, "gw1")


def fake_gateway(srv: socket.socket, seen: dict) -> None:
    """Gateway side of the pairing protocol (port 25050 after the button press)."""
    c, _ = srv.accept()
    c.settimeout(5)
    buf = bytearray()

    def recv():
        while True:
            if len(buf) >= 4:
                t, n = struct.unpack_from("<HH", buf)
                if len(buf) >= 4 + n:
                    out = (t, bytes(buf[4:4 + n]))
                    del buf[:4 + n]
                    return out
            buf.extend(c.recv(4096))

    def frame(t, p=b""):
        return struct.pack("<HH", t, len(p)) + p

    def bump(n):
        for i in range(11, 3, -1):
            if n[i] < 0xFF:
                n[i] += 1
                return
            n[i] = 0

    assert recv()[0] == 0x0000
    c.sendall(frame(0x0001))
    t, p = recv()
    assert t == 0x0201 and p[0] == 1
    priv = x25519.X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    c.sendall(frame(0x0202, b"\x01" + pub))
    aead = ChaCha20Poly1305(hashlib.sha512(priv.exchange(x25519.X25519PublicKey.from_public_bytes(p[1:]))).digest()[:32])
    tx, rx = bytearray(12), bytearray(12)

    def srecv():
        t, p = recv()
        assert t == 0x0200
        plain = aead.decrypt(bytes(rx), p, None)
        bump(rx)
        return struct.unpack_from("<H", plain)[0], plain[4:]

    def ssend(t, p=b""):
        c.sendall(frame(0x0200, aead.encrypt(bytes(tx), frame(t, p), None)))
        bump(tx)

    t, p = srecv()
    assert t == 0x020E and p[0] == 0
    key_id = p[1:]
    seen["key_id"] = key_id
    ssend(0x020F, b"\x00" + GW_KEY)
    t, p = srecv()
    assert t == 0x020A
    ssend(0x020B, b"N" * 16)
    t, p = srecv()
    assert t == 0x020C and p == key_id + hashlib.sha512(key_id + GW_KEY + b"N" * 16).digest()
    ssend(0x020D, b"\x00")
    t, _ = srecv()
    assert t == 0x0008
    seen["ok"] = True
    c.close()


def test_retrieve_key():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    seen = {}

    def serve():
        # the bridge first probes whether the port is open, then connects for real
        c, _ = srv.accept()
        c.close()
        fake_gateway(srv, seen)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    key_id, key = sb.velux_retrieve_key("127.0.0.1", timeout=10, port=port)
    th.join(5)
    assert seen.get("ok")
    assert base64.urlsafe_b64decode(key_id) == seen["key_id"]
    assert base64.urlsafe_b64decode(key) == GW_KEY


def test_source_lock_unlock(monkeypatch):
    calls = []
    status = {"locked": True}

    async def main():
        app = web.Application()

        async def token(req):
            form = await req.post()
            calls.append(("token", form.get("grant_type"), form.get("client_id")))
            return web.json_response({"access_token": "at1", "refresh_token": "rt1", "expires_in": 10800})

        async def homesdata(req):
            calls.append(("homesdata", req.headers.get("Authorization")))
            return web.json_response({"body": {"homes": [{"id": "home1", "name": "Thuis", "timezone": "Europe/Amsterdam",
                                                          "modules": [{"id": "70:ee:50:00:00:01", "type": "NXG",
                                                                       "name": "Gateway"},
                                                                      {"id": "w1", "type": "NXO"}]}]}})

        async def homestatus(req):
            form = await req.post()
            calls.append(("homestatus", form.get("home_id")))
            return web.json_response({"body": {"home": {"id": "home1", "modules": [
                {"id": "70:ee:50:00:00:01", "type": "NXG", "locked": status["locked"], "is_raining": False}]}}})

        async def setstate(req):
            body = await req.json()
            calls.append(("setstate", body))
            m = body["home"]["modules"][0]
            if m.get("scenario") == "away":
                status["locked"] = True
            elif m.get("scenario") == "home":
                status["locked"] = False
            return web.json_response({"status": "ok", "body": {}})

        app.router.add_post("/oauth2/token", token)
        app.router.add_post("/api/homesdata", homesdata)
        app.router.add_post("/api/homestatus", homestatus)
        app.router.add_post("/syncapi/v1/setstate", setstate)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(sb, "VELUX_NETATMO", f"http://127.0.0.1:{port}/")
        monkeypatch.setattr(sb, "VELUX_SYNC", f"http://127.0.0.1:{port}/syncapi/v1/")

        sb.save_config({"velux": {"username": "a@b.c", "password": "pw"}})
        src = sb.VeluxCloud(sb.load_config())
        task = asyncio.create_task(src.run())
        dev_id = "velux-70:ee:50:00:00:01"
        for _ in range(100):
            if dev_id in sb.REG.devices:
                break
            await asyncio.sleep(0.02)
        d = sb.REG.devices[dev_id]
        assert d["type"] == "velux_lock" and d["state"]["locked"] is True and d["state"]["canUnlock"] is False
        assert calls[0] == ("token", "password", sb.VELUX_CLIENT["client_id"])
        assert ("homesdata", "Bearer at1") in calls
        assert sb.load_config()["velux"]["tokens"]["refresh_token"] == "rt1"  # kept for the next start
        assert sb.REG.devices["velux-rain-70:ee:50:00:00:01"]["state"] == {"raining": False}

        h = sb.REG.handlers[dev_id]
        try:
            await h("unlock", None)
            raise AssertionError("unlock without key must fail")
        except PermissionError:
            pass
        cfg = sb.load_config()
        key = base64.b64encode(b"z" * 32).decode()
        cfg["velux"].update(sign_key_id="kid", hash_sign_key=key, sign_key_gateway="70:ee:50:00:00:01")
        sb.save_config(cfg)
        await h("unlock", None)
        body = [c for c in calls if c[0] == "setstate"][-1][1]
        m = body["home"]["modules"][0]
        assert body["app_type"] == "app_velux" and body["home"]["timezone"] == "Europe/Amsterdam"
        assert m["scenario"] == "home" and m["sign_key_id"] == "kid" and m["nonce"] == 0
        assert m["hash_scenario"] == sb.velux_scenario_hash(key, "home", m["timestamp"], 0, "70:ee:50:00:00:01")
        for _ in range(100):
            if sb.REG.devices[dev_id]["state"]["locked"] is False:
                break
            await asyncio.sleep(0.05)
        assert sb.REG.devices[dev_id]["state"]["locked"] is False
        await h("lock", None)
        m = [c for c in calls if c[0] == "setstate"][-1][1]["home"]["modules"][0]
        assert m == {"id": "70:ee:50:00:00:01", "scenario": "away"}
        task.cancel()
        await runner.cleanup()

    asyncio.run(main())
