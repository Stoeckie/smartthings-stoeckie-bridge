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


def _fake_net(tmp, iface, gw_hex, ifindex, iflink):
    route = Path(tmp, "route")
    route.write_text("Iface\tDestination\tGateway \tFlags\n"
                     f"{iface}\t00000000\t{gw_hex}\t0003\n"
                     f"{iface}\t000011AC\t00000000\t0001\n")
    d = Path(tmp, "net", iface)
    d.mkdir(parents=True)
    (d / "ifindex").write_text(f"{ifindex}\n")
    (d / "iflink").write_text(f"{iflink}\n")
    return str(route), str(Path(tmp, "net"))


def test_container_gateway_bridge_network():
    """Docker bridge network: default route over a veth, gateway 172.17.0.1 is allowed for the setup page."""
    with tempfile.TemporaryDirectory() as tmp:
        route, net = _fake_net(tmp, "eth0", "010011AC", 12, 13)
        gw = sb.container_gateway(route, net)
        assert str(gw) == "172.17.0.1"


def test_container_gateway_host_network():
    """Host networking: the default route is a real interface, so the LAN router is NOT allowed."""
    with tempfile.TemporaryDirectory() as tmp:
        route, net = _fake_net(tmp, "enp3s0", "0101A8C0", 2, 2)
        assert sb.container_gateway(route, net) is None
        assert sb.container_gateway(str(Path(tmp, "missing")), net) is None


def test_setup_source_allowed_gateway():
    import ipaddress
    old = sb.SETUP_GATEWAY
    try:
        sb.SETUP_GATEWAY = ipaddress.ip_address("172.17.0.1")
        assert sb.setup_source_allowed(ipaddress.ip_address("172.17.0.1"))
        assert sb.setup_source_allowed(ipaddress.ip_address("127.0.0.1"))
        assert not sb.setup_source_allowed(ipaddress.ip_address("192.168.1.50"))
        sb.SETUP_GATEWAY = None
        assert not sb.setup_source_allowed(ipaddress.ip_address("172.17.0.1"))
    finally:
        sb.SETUP_GATEWAY = old


def test_setup_page_is_english():
    async def main():
        c = await _client()
        r = await c.get("/setup")
        text = await r.text()
        assert "Pair (open for 5 minutes)" in text and "Koppelen" not in text and 'lang="en"' in text
        await c.close()
    run(main())


def test_gateway_needs_setup_key():
    """A browser from the Docker gateway (can be the whole LAN on Docker Desktop) must enter the setup key."""
    import ipaddress
    old = sb.SETUP_GATEWAY
    sb.SETUP_GATEWAY = ipaddress.ip_address("127.0.0.1")   # the test client comes from 127.0.0.1

    async def main():
        c = await _client()
        port = c.server.port
        r = await c.get("/setup")
        assert r.status == 200 and "Setup key" in await r.text() and "AEG" not in await r.text()
        r = await c.post("/setup", data={"section": "fglair", "username": "x@y.nl", "password": "p"},
                         headers={"Origin": f"http://127.0.0.1:{port}"}, allow_redirects=False)
        assert "Setup key" in await r.text()                 # nothing saved without the key
        r = await c.post("/setup", data={"section": "setup_key", "key": "wrong"},
                         headers={"Origin": f"http://127.0.0.1:{port}"}, allow_redirects=False)
        assert "Wrong key" in await r.text()
        r = await c.post("/setup", data={"section": "setup_key", "key": sb.setup_key()},
                         headers={"Origin": f"http://127.0.0.1:{port}"}, allow_redirects=False)
        assert r.status == 302 and "stbridge_setup" in r.headers.get("Set-Cookie", "")
        r = await c.get("/setup", headers={"Cookie": "stbridge_setup=" + sb.setup_cookie_value()})
        assert r.status == 200 and "AEG" in await r.text()
        await c.close()
    try:
        run(main())
    finally:
        sb.SETUP_GATEWAY = old


def test_no_plain_setup_from_lan_without_https():
    """When the certificate could not be made, the setup page does not fall back to plain http for the LAN."""
    import ipaddress
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        lan = s.getsockname()[0]
    except OSError:
        return
    finally:
        s.close()
    if ipaddress.ip_address(lan).is_loopback:
        return
    old = list(sb.SETUP_ALLOW)
    sb.SETUP_ALLOW[:] = [ipaddress.ip_network(lan + "/32")]

    async def main():
        srv = TestServer(sb.make_app("t", None, None), host=lan)
        c = TestClient(srv)
        await c.start_server()
        r = await c.get("/setup", allow_redirects=False)
        assert r.status == 403 and "https is not available" in await r.text()
        await c.close()
        srv = TestServer(sb.make_app("t", None, 8443), host=lan)
        c = TestClient(srv)
        await c.start_server()
        r = await c.get("/setup", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"].startswith("https://")
        await c.close()
    try:
        run(main())
    finally:
        sb.SETUP_ALLOW[:] = old
