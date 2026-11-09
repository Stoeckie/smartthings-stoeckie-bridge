"""Samsung local: client certificate."""
from cryptography import x509

import stoeckie_bridge as sb


def test_cert(tmp_path):
    c, k = sb.samsung_cert(tmp_path)
    cert = x509.load_pem_x509_certificate(c.read_bytes())
    subj = cert.subject.rfc4514_string()
    assert f"uuid:{sb.SS_CLIENT_UUID}" in subj and f"urn:uuid:{sb.SS_CLIENT_UUID}" in subj
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sb.SS_CLIENT_UUID in san.get_values_for_type(x509.DNSName)
    assert b"PRIVATE KEY" in k.read_bytes()
    assert sb.samsung_cert(tmp_path) == (c, k)       # made once, reused
    assert c.read_bytes() == sb.samsung_cert(tmp_path)[0].read_bytes()


def test_washer_state():
    res = {
        "/operational/state/vs/0": {"x.com.samsung.da.state": "Run", "x.com.samsung.da.progress": "Rinse",
                                    "x.com.samsung.da.remainingTime": "00:42:00",
                                    "x.com.samsung.da.progressPercentage": "60"},
        "/power/vs/0": {"x.com.samsung.da.power": "On"},
        "/remotectrl/vs/0": {"x.com.samsung.da.remoteControlEnabled": "true"},
        "/course/vs/0": {"x.com.samsung.da.options": ["Course_1C", "DrumCleanProposal_40"]},
        "/st/washercourse/vs/0": {"x.com.samsung.da.st.courseTable": "Table_02"},
    }
    st = sb.ss_washer_state(res)
    assert st["power"] == "on" and st["operation"] == "run" and st["job"] == "rinse"
    assert st["remainingTime"] == 2520 and st["progress"] == 60 and st["completionTime"]
    assert st["remoteStartAllowed"] is True and st["course"] == "Eco 40-60"
    res["/operational/state/vs/0"].update({"x.com.samsung.da.progress": "Finish"})
    st = sb.ss_washer_state(res)
    assert st["operation"] == "finished" and st["job"] == "finish" and st["remainingTime"] is None
    off = sb.ss_washer_state({})
    assert off["power"] == "off" and off["operation"] == "inactive" and off["course"] is None
    assert sb.ss_seconds("12:30") == 750 and sb.ss_seconds("x") is None
    assert sb.ss_course_label("ZZ", "Table_99") == "Course ZZ"


def test_washer_commands(monkeypatch):
    import asyncio
    import pytest
    ss = sb.Samsung({"samsung": {"appliances": [{"host": "192.168.1.9"}]}})
    writes = []
    monkeypatch.setattr(sb.Samsung, "_write", staticmethod(lambda sess, path, body: writes.append((path, body))))
    ss.locks["192.168.1.9"] = asyncio.Lock()
    h = ss._command("192.168.1.9")
    with pytest.raises(ValueError):
        asyncio.run(h("pause", None))             # not connected
    ss.sessions["192.168.1.9"] = object()
    asyncio.run(h("start", None))
    asyncio.run(h("stop", None))
    asyncio.run(h("off", None))
    assert writes[0] == (("operational", "state", "vs", "0"), {"x.com.samsung.da.state": "Run"})
    assert writes[1][1] == {"x.com.samsung.da.state": "Ready"}
    assert writes[2] == (("power", "vs", "0"), {"x.com.samsung.da.power": "Off"})


def test_courses():
    # 1 nibble header 1 -> records of 3 bytes; first byte is the course code
    course = {"x.com.samsung.da.supportedOptions": "1D0AAAABDBBBBDCCCCCD8DDDDBA0000",
              "x.com.samsung.da.options": ["Course_D0"]}
    assert sb.ss_course_codes(course) == ["D0", "BD", "DC", "D8", "BA"]
    assert sb.ss_course_codes({}, {"x.com.samsung.da.editCourseList": "EditCourseList_1C1D21"}) == ["1C", "1D", "21"]
    res = {"/course/vs/0": course, "/st/washercourse/vs/0": {"x.com.samsung.da.st.courseTable": "Table_00"}}
    progs = sb.ss_programs(res)
    assert progs == {"cotton": "D0", "eco4060": "BD", "quick15": "DC", "wool": "D8", "drainSpin": "BA"}
    st = sb.ss_washer_state(res)
    assert st["program"] == "cotton" and st["programs"][:2] == ["cotton", "eco4060"]


def test_course_command(monkeypatch):
    import asyncio
    import pytest
    ss = sb.Samsung({})
    writes = []
    monkeypatch.setattr(sb.Samsung, "_write", staticmethod(lambda sess, path, body: writes.append((path, body))))
    host = "192.168.1.9"
    ss.locks[host], ss.sessions[host] = asyncio.Lock(), object()
    ss.programs[host] = {"cotton": "D0", "quick15": "DC"}
    h = ss._command(host)
    asyncio.run(h("program", "quick15"))
    assert writes == [(("course", "vs", "0"), {"x.com.samsung.da.options": ["Course_DC"]})]
    asyncio.run(h("start", "cotton"))
    assert writes[1][1] == {"x.com.samsung.da.options": ["Course_D0"]}
    assert writes[2] == (("operational", "state", "vs", "0"), {"x.com.samsung.da.state": "Run"})
    with pytest.raises(ValueError):
        asyncio.run(h("program", "wool"))


def test_courses_real_dump():
    # supportedOptions of a WW..T washer (DA_WM_A51_20_COMMON, Table_00); same 14 courses as the cloud lists
    so = ("3D0847E923FA7FFBD84109308A7FFDC811E933FA33FDB841E923FA67FDA8102923FA53FD8841E920FA30FD6841E933FA30F"
          "E48560943FA67FD585209204A520BA80009000A6FFD48000913FA6FFD3831E920FA30FD2843E923FA67FD18430923FA7FF")
    course = {"x.com.samsung.da.supportedOptions": [so], "x.com.samsung.da.options": ["Course_D0"]}
    assert sb.ss_course_codes(course) == ["D0", "BD", "DC", "DB", "DA", "D8", "D6", "E4", "D5", "BA", "D4", "D3",
                                          "D2", "D1"]
    progs = sb.ss_programs({"/course/vs/0": course,
                            "/st/washercourse/vs/0": {"x.com.samsung.da.st.courseTable": "Table_00"}})
    assert list(progs) == ["cotton", "eco4060", "quick15", "superSpeed", "superEco", "wool", "bedding",
                           "antiAllergen", "drumClean", "drainSpin", "rinseSpin", "delicates", "synthetics", "eCotton"]


def test_loop_keeps_session_on_timeouts(monkeypatch):
    """A few unanswered reads do not close the session; state still gets published."""
    import asyncio
    import cbor2
    sb.REG.devices.clear()
    calls = {"n": 0, "closed": 0}

    class Sess:
        def get(self, path, timeout=10):
            calls["n"] += 1
            if 3 <= calls["n"] <= 6:
                raise TimeoutError("session operation timed out")
            data = {"operational/state/vs/0": {"x.com.samsung.da.state": "Run", "x.com.samsung.da.progress": "Wash",
                                               "x.com.samsung.da.remainingTime": "00:30:00"},
                    "power/vs/0": {"x.com.samsung.da.power": "On"}}.get("/".join(path), {})
            return 0x45, cbor2.dumps(data)

        def ping(self):
            pass

        def close(self):
            calls["closed"] += 1

    ss = sb.Samsung({"samsung": {"appliances": [{"host": "192.168.1.9", "name": "W"}]}})
    monkeypatch.setattr(ss, "_open", lambda host: (Sess(), 49155))
    monkeypatch.setattr(sb.Samsung, "HOT_EVERY", 0.01)

    async def main():
        t = asyncio.create_task(ss._run_one({"host": "192.168.1.9", "name": "W"}))
        await asyncio.sleep(0.6)
        t.cancel()
    asyncio.run(main())
    st = sb.REG.devices["samsung-192-168-1-9"]["state"]
    assert st["operation"] == "run" and st["power"] == "on" and calls["closed"] == 0


def test_tv_helpers():
    h, body = sb.tv_soap("SetVolume", "<DesiredVolume>12</DesiredVolume>")
    assert "RenderingControl:1#SetVolume" in h["SOAPACTION"] and "<DesiredVolume>12</DesiredVolume>" in body
    assert sb.tv_xml_value("<a><CurrentVolume>17</CurrentVolume></a>", "CurrentVolume") == "17"
    assert sb.wol_packet("AA:BB:CC:DD:EE:FF") == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16
    tv = sb.SamsungTV({"samsungtv": {"tvs": [{"host": "192.168.1.30", "type": "tv"}]}})
    assert [t["host"] for t in tv.tvs] == ["192.168.1.30"] and tv.dev_id("192.168.1.30") == "samsungtv-192-168-1-30"
    ss = sb.Samsung({"samsung": {"appliances": [{"host": "192.168.1.30", "type": "tv"}, {"host": "192.168.1.9"}]}})
    assert [a["host"] for a in ss.appliances] == ["192.168.1.9"]


def _off_test(monkeypatch, state):
    """The washer answers, then stops answering (switched off): offline, and off when it was not washing."""
    import asyncio
    import cbor2
    sb.REG.devices.clear()
    calls = {"n": 0}

    class Sess:
        def get(self, path, timeout=10):
            calls["n"] += 1
            if calls["n"] > 6:
                raise TimeoutError("session operation timed out")
            data = {"operational/state/vs/0": {"x.com.samsung.da.state": state, "x.com.samsung.da.progress": "Wash",
                                               "x.com.samsung.da.remainingTime": "00:30:00"},
                    "power/vs/0": {"x.com.samsung.da.power": "On"},
                    "remotectrl/vs/0": {"x.com.samsung.da.remoteControlEnabled": "true"}}.get("/".join(path), {})
            return 0x45, cbor2.dumps(data)

        def ping(self):
            pass

        def close(self):
            pass

    opened = {"n": 0}

    def fake_open(host):
        opened["n"] += 1
        if opened["n"] > 1:
            raise ConnectionError("no answer (switched off?)")
        return Sess(), 49155

    ss = sb.Samsung({"samsung": {"appliances": [{"host": "192.168.1.9", "name": "W"}]}})
    monkeypatch.setattr(ss, "_open", fake_open)
    monkeypatch.setattr(sb.Samsung, "HOT_EVERY", 0.01)
    monkeypatch.setattr(sb.Samsung, "LIVENESS", 0.1)
    monkeypatch.setattr(sb.Samsung, "RETRY", 0.01)
    seen = []
    first = []
    orig = sb.REG.upsert

    def rec(dev_id, kind, name, state, online=True, handler=None):
        first.append((online, dict(state)))
        orig(dev_id, kind, name, state, online=online, handler=handler)
    monkeypatch.setattr(sb.REG, "upsert", rec)

    async def main():
        t = asyncio.create_task(ss._run_one({"host": "192.168.1.9", "name": "W"}))
        for _ in range(100):
            await asyncio.sleep(0.01)
            d = sb.REG.devices.get("samsung-192-168-1-9")
            if d:
                seen.append((d["online"], dict(d["state"])))
        t.cancel()
    asyncio.run(main())
    seen.insert(0, first[0])
    return seen, sb.REG.devices["samsung-192-168-1-9"]


def test_washer_off_when_idle(monkeypatch):
    seen, d = _off_test(monkeypatch, "Ready")
    assert seen[0] == (False, {})  # not shown as off before the first answer
    assert any(o and s.get("power") == "on" and s.get("remoteStartAllowed") for o, s in seen)
    assert d["online"] is False and d["state"]["power"] == "off" and d["state"]["remoteStartAllowed"] is False


def test_washer_keeps_state_when_lost_during_wash(monkeypatch):
    seen, d = _off_test(monkeypatch, "Run")
    assert d["online"] is False and d["state"]["operation"] == "run" and d["state"]["power"] == "on"


def test_washer_off_after_restart_without_answer(monkeypatch):
    """Bridge starts while the washer is switched off: offline and off, not the last state from before."""
    import asyncio
    sb.REG.devices.clear()
    ss = sb.Samsung({"samsung": {"appliances": [{"host": "192.168.1.9", "name": "W"}]}})

    def no_answer(host):
        raise ConnectionError("no answer (switched off?)")
    monkeypatch.setattr(ss, "_open", no_answer)
    monkeypatch.setattr(sb.Samsung, "RETRY", 0.01)

    async def main():
        t = asyncio.create_task(ss._run_one({"host": "192.168.1.9", "name": "W"}))
        await asyncio.sleep(0.2)
        t.cancel()
    asyncio.run(main())
    d = sb.REG.devices["samsung-192-168-1-9"]
    assert d["online"] is False and d["state"]["power"] == "off" and d["state"]["remoteStartAllowed"] is False


def test_tv_certificate_pinned(tmp_path):
    """The TV token only goes to the TV whose certificate was seen first; another certificate gets no token."""
    import asyncio
    import json as _json

    import aiohttp
    from aiohttp import web

    seen = []

    async def ws(req):
        seen.append(req.query.get("token"))
        w = web.WebSocketResponse()
        await w.prepare(req)
        await w.send_str(_json.dumps({"event": "ms.channel.connect", "data": {"token": "tok1"}}))
        await w.close()
        return w

    async def serve(certdir):
        certdir.mkdir()
        ctx, _ = sb.setup_tls(certdir)
        assert ctx is not None
        app = web.Application()
        app.router.add_get("/api/v2/channels/samsung.remote.control", ws)
        r = web.AppRunner(app)
        await r.setup()
        site = web.TCPSite(r, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        return r, site._server.sockets[0].getsockname()[1]

    async def main():
        sb.save_config({})
        tv = sb.SamsungTV({})
        tv.session = aiohttp.ClientSession()
        r, port = await serve(tmp_path / "a")
        tv.WS_PORT = port
        await tv._keys("127.0.0.1", [], 5)               # first time: no token, certificate kept
        assert seen == [None] and tv.tokens["127.0.0.1"] == "tok1" and len(tv.certs["127.0.0.1"]) == 64
        await tv._keys("127.0.0.1", [], 5)               # same TV: token sent
        assert seen[-1] == "tok1"
        await r.cleanup()
        r, port = await serve(tmp_path / "b")            # another device at the address (other certificate)
        tv.WS_PORT = port
        try:
            await tv._keys("127.0.0.1", [], 5)
            raise AssertionError("expected a refusal")
        except ValueError as ex:
            assert "certificate changed" in str(ex)
        assert len(seen) == 2                            # it never got a request, so never the token
        assert "127.0.0.1" not in tv.tokens and "127.0.0.1" not in tv.certs
        assert "127.0.0.1" not in (sb.load_config()["samsungtv"].get("tokens") or {})
        await r.cleanup()
        await tv.session.close()

    asyncio.run(main())
