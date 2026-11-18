"""UniFi firewall switches: switch meaning, PUT body, back to normal after X minutes (fake console, no network)."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402

BLOCK = {"id": "p1", "enabled": True, "name": "Block Internet IoT", "action": {"type": "BLOCK"},
         "source": {"zoneId": "z1"}, "destination": {"zoneId": "z2"}, "ipProtocolScope": {"ipVersion": "IPV4"},
         "loggingEnabled": False, "index": 10000, "metadata": {"origin": "USER_DEFINED"}}
ALLOW = dict(BLOCK, id="p2", name="Allow thing", action={"type": "ALLOW"}, enabled=False)


def test_meaning_and_body():
    assert sb.unifi_blocking(BLOCK) and not sb.unifi_blocking(ALLOW)
    assert not sb.unifi_switch_on(BLOCK)                          # block policy active = switch off
    assert sb.unifi_switch_on(dict(BLOCK, enabled=False))         # paused = switch on
    assert not sb.unifi_switch_on(ALLOW) and sb.unifi_switch_on(dict(ALLOW, enabled=True))
    body = sb.unifi_put_body(BLOCK, False)
    assert body["enabled"] is False and "id" not in body and "metadata" not in body and "index" not in body
    assert body["source"] == BLOCK["source"] and body["ipProtocolScope"] == BLOCK["ipProtocolScope"]
    assert BLOCK["enabled"] is True                                # original untouched
    assert sb.unifi_minutes("30") == 30 and sb.unifi_minutes("x") == 60 and sb.unifi_minutes(5000) == 1440


class FakeConsole:
    def __init__(self):
        self.p = {"p1": dict(BLOCK), "p2": dict(ALLOW)}
        self.puts = []

    async def call(self, method, path, body=None):
        if path.startswith("/sites?"):
            return {"data": [{"id": "S1", "internalReference": "default", "name": "Default"}]}
        pid = path.rsplit("/", 1)[-1].split("?")[0]
        if method == "PUT":
            assert "id" not in body and "metadata" not in body
            self.puts.append((pid, body["enabled"]))
            self.p[pid] = dict(self.p[pid], enabled=body["enabled"])
            return self.p[pid]
        if pid == "policies":
            return {"data": list(self.p.values()), "totalCount": 2}
        return dict(self.p[pid])


def test_switch_on_and_back(monkeypatch):
    cfg = {"unifi": {"host": "192.0.2.1", "api_key": "k" * 32, "pin": "00" * 32,
                     "switches": [{"id": "p1", "minutes": 30, "name": ""}]}}
    sb.save_config(cfg)
    con = FakeConsole()
    fw = sb.UnifiFirewall(sb.load_config())
    monkeypatch.setattr(fw, "_call", con.call)
    now = [1_000_000.0]
    monkeypatch.setattr(sb.time, "time", lambda: now[0])

    async def go():
        await fw._round()
        dev = sb.REG.devices["unifi-p1"]
        assert dev["type"] == "net_switch" and dev["state"]["switch"] == "off" and dev["name"] == "UniFi Block Internet IoT"
        await sb.REG.handlers["unifi-p1"]("on", None)
        assert con.puts == [("p1", False)]
        assert sb.REG.devices["unifi-p1"]["state"]["switch"] == "on"
        assert sb.load_config()["unifi"]["until"]["p1"] == now[0] + 1800
        now[0] += 1700
        await fw._round()
        assert con.puts == [("p1", False)]                     # not yet
        now[0] += 200
        await fw._round()
        assert con.puts == [("p1", False), ("p1", True)]       # back to normal after 30 min
        assert sb.REG.devices["unifi-p1"]["state"]["switch"] == "off"
        assert "p1" not in sb.load_config()["unifi"]["until"]
        # switched back in UniFi itself before the time: the bridge forgets the time, sends nothing
        await sb.REG.handlers["unifi-p1"]("on", None)
        con.p["p1"]["enabled"] = True
        await fw._round()
        assert "p1" not in sb.load_config()["unifi"]["until"] and len(con.puts) == 3
        # off by hand
        await sb.REG.handlers["unifi-p1"]("on", None)
        await sb.REG.handlers["unifi-p1"]("off", None)
        assert con.puts[-1] == ("p1", True) and "p1" not in sb.load_config()["unifi"]["until"]

    asyncio.run(go())


def test_not_configured_without_pin():
    assert not sb.unifi_configured({"host": "h", "api_key": "k"})
    assert sb.unifi_configured({"host": "h", "api_key": "k", "pin": "ab"})


def test_setup_page_and_forms():
    from aiohttp.test_utils import TestClient, TestServer
    sb.save_config({"unifi": {"host": "192.0.2.1", "api_key": "k" * 32, "pin": "ab" * 32}})
    sb.UNIFI["policies"] = [{"id": "0e0fd705-bbc7-5307-8252-3c25e8b0c3b7", "name": "Block Internet IoT", "enabled": True, "blocking": True}]
    started = []

    async def main():
        c = TestClient(TestServer(sb.make_app("t", started.append)))
        await c.start_server()
        page = await (await c.get("/setup")).text()
        assert 'id="unifi"' in page and "Block Internet IoT (block)" in page and "{unifi" not in page
        assert "k" * 32 not in page                                   # the key never goes into the page
        r = await c.post("/setup", data={"section": "unifi_add", "id": "0e0fd705-bbc7-5307-8252-3c25e8b0c3b7", "minutes": "45", "name": "IOT internet access 60 minutes"},
                         allow_redirects=False)
        assert r.status == 302 and sb.load_config()["unifi"]["switches"] == [
            {"id": "0e0fd705-bbc7-5307-8252-3c25e8b0c3b7", "minutes": 45, "name": "IOT internet access 60 minutes"}]
        assert started[-1] == "unifi"
        page = await (await c.get("/setup")).text()
        assert "back after 45 min" in page and "Block Internet IoT (block)</option>" not in page
        r = await c.post("/setup", data={"section": "unifi_add", "id": "nope", "minutes": "5"}, allow_redirects=False)
        assert r.status == 400
        r = await c.post("/setup", data={"section": "unifi", "host": "x y", "api_key": "k" * 32}, allow_redirects=False)
        assert r.status == 400
        r = await c.post("/setup", data={"section": "unifi_remove", "id": "0e0fd705-bbc7-5307-8252-3c25e8b0c3b7"}, allow_redirects=False)
        assert r.status == 302 and sb.load_config()["unifi"]["switches"] == []
        await c.close()

    asyncio.run(main())


def test_policy_list_ids_as_text():
    lst = sb.unifi_policy_list([dict(BLOCK, id=12), dict(BLOCK, id=None, _id="ab-1"), dict(BLOCK, id=None),
                                dict(BLOCK, id="s", metadata={"origin": "SYSTEM_DEFINED"}), "junk"])
    assert [x["id"] for x in lst] == ["12", "ab-1"]
