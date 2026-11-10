"""Amazon Echo source with a fake alexapy (no network, no Amazon account)."""
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402

DEVICES = [
    {"serialNumber": "G0001", "accountName": "Kitchen Echo", "deviceFamily": "ECHO", "deviceType": "A3S5",
     "capabilities": ["AUDIO_PLAYER", "VOLUME_SETTING"], "online": True},
    {"serialNumber": "G0002", "accountName": "Living Show", "deviceFamily": "KNIGHT", "deviceType": "A4ZP",
     "capabilities": ["AUDIO_PLAYER"], "online": True},
    {"serialNumber": "APP1", "accountName": "David's Alexa app", "deviceFamily": "VOX",
     "capabilities": ["AUDIO_PLAYER"]},
    {"serialNumber": "PLUG1", "accountName": "Smart plug", "deviceFamily": "THIRD_PARTY", "capabilities": []},
]


def test_helpers():
    assert sb.echo_wanted(DEVICES[0]) and sb.echo_wanted(DEVICES[1])
    assert not sb.echo_wanted(DEVICES[2]) and not sb.echo_wanted(DEVICES[3])
    assert sb.echo_volume("55.4") == 55 and sb.echo_volume(130) == 100 and sb.echo_volume(-3) == 0
    assert sb.echo_text("  hello\n world ") == "hello world"
    assert len(sb.echo_text("x" * 400)) == 250
    try:
        sb.echo_text("   ")
        raise AssertionError("empty text accepted")
    except ValueError:
        pass
    assert not sb.echo_configured({"email": "a@b.c"})
    assert sb.echo_configured({"email": "a@b.c", "oauth": {"refresh_token": "x"}})


def test_commands_and_log(monkeypatch, caplog):
    calls = []

    class FakeAPI:
        def __init__(self, dev, login):
            self.serial = dev.device_serial_number

        @staticmethod
        async def get_devices(login):
            return DEVICES

        async def send_tts(self, text, queue_delay=0):
            calls.append(("tts", self.serial, text))

        async def send_announcement(self, text, method, title, targets, queue_delay=0):
            calls.append(("announce", self.serial, text, tuple(targets)))

        async def run_custom(self, text):
            calls.append(("custom", self.serial, text))

        async def set_volume(self, v, queue_delay=0):
            calls.append(("volume", self.serial, v))

        async def stop(self, queue_delay=0):
            calls.append(("stop", self.serial))

        async def run_routine(self, name, queue_delay=0):
            calls.append(("routine", self.serial, name))

    import alexapy
    monkeypatch.setattr(alexapy, "AlexaAPI", FakeAPI)

    async def main():
        src = sb.EchoCloud({"echo": {"email": "a@b.c", "oauth": {"refresh_token": "x"}}})
        src.login = object()
        await src._fetch()
        assert "echo-G0001" in sb.REG.devices and "echo-G0002" in sb.REG.devices
        assert "echo-APP1" not in sb.REG.devices and "echo-PLUG1" not in sb.REG.devices
        assert sb.REG.devices["echo-G0001"]["type"] == "echo"
        h = sb.REG.handlers["echo-G0001"]
        with caplog.at_level(logging.INFO):
            await h("speak", "The washing machine is done")
            await h("announce", "Dinner")
            await h("command", "play Radio 538")
            await h("volume", 40)
            await h("volumeUp", None)
            await h("stop", None)
        assert calls[0] == ("tts", "G0001", "The washing machine is done")
        assert calls[1] == ("announce", "G0001", "Dinner", ("G0001",))
        assert calls[2] == ("custom", "G0001", "play Radio 538")
        assert calls[3] == ("volume", "G0001", 0.4) and calls[4] == ("volume", "G0001", 0.5)
        assert sb.REG.devices["echo-G0001"]["state"]["volume"] == 50
        assert calls[5] == ("stop", "G0001")
        # the spoken texts never go into the log, only their length
        assert "washing machine" not in caplog.text and "Radio 538" not in caplog.text
        assert "speak (27 chars)" in caplog.text
    asyncio.run(main())


def test_setup_page_mentions_echo():
    assert "Amazon Echo (optional, unofficial)" in sb.SETUP_HTML and "{echo_link}" in sb.SETUP_HTML


def test_setup_login_flow():
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        c = TestClient(TestServer(sb.make_app("t", None)))
        await c.start_server()
        port = c.server.port
        r = await c.post("/setup", data={"section": "echo", "email": "a@b.nl", "password": "secret-pw",
                                         "url": "amazon.de"},
                         headers={"Origin": f"http://127.0.0.1:{port}"}, allow_redirects=False)
        assert r.status == 302
        cfg = sb.load_config()
        assert cfg["echo"]["email"] == "a@b.nl" and "password" not in cfg["echo"]  # the password is not kept
        assert sb.ECHO_LOGIN["proxy"] is not None and "/setup/echo-auth" in sb.ECHO_LOGIN["url"]
        page = await (await c.get("/setup")).text()
        assert "Log in at Amazon" in page and "secret-pw" not in page
        # the relayed Amazon page has no setup CSP (its scripts must run), other setup pages keep it
        r = await c.get("/setup")
        assert "Content-Security-Policy" in r.headers
        # from another site: refused
        r = await c.get("/setup/echo-auth/", headers={"Host": f"evil.example:{port}"}, allow_redirects=False)
        assert r.status == 403
        await c.close()
    asyncio.run(main())


def test_password_optional():
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        c = TestClient(TestServer(sb.make_app("t", None)))
        await c.start_server()
        port = c.server.port
        r = await c.post("/setup", data={"section": "echo", "email": "a@b.nl", "password": "", "url": "amazon.com"},
                         headers={"Origin": f"http://127.0.0.1:{port}"}, allow_redirects=False)
        assert r.status == 302 and sb.ECHO_LOGIN["proxy"] is not None
        await c.close()
    asyncio.run(main())


def test_echo_volumes_from():
    import stoeckie_bridge as b
    js = {"volumes": [{"dsn": "G1", "speakerVolume": 40}, {"dsn": "G2", "speakerVolume": None},
                      {"dsn": "G3", "speakerVolume": 140}, {"bad": 1}]}
    assert b.echo_volumes_from(js) == {"G1": 40, "G3": 100}
    assert b.echo_volumes_from(None) == {} and b.echo_volumes_from([]) == {}


def test_echo_helpers_extra():
    import json
    import stoeckie_bridge as b
    assert b.echo_music("spotify: Coldplay") == ("SPOTIFY", "Coldplay")
    assert b.echo_music("Radio 538") == (None, "Radio 538")
    assert b.echo_music("radio: 538") == ("TUNEIN", "538")
    assert b.echo_dnd_from({"doNotDisturbDeviceStatusList": [{"deviceSerialNumber": "A", "enabled": True},
                                                             {"deviceSerialNumber": "B"}]}) == {"A": True, "B": False}
    bt = b.echo_bluetooth_from({"bluetoothStates": [{"deviceSerialNumber": "A", "pairedDeviceList": [
        {"friendlyName": "Speaker", "address": "11:22", "connected": True}, {"friendlyName": "Phone", "address": "33"}]}]})
    assert bt["A"]["connected"] == "Speaker" and bt["A"]["paired"] == {"Speaker": "11:22", "Phone": "33"}
    now = 1_800_000_000.0
    al = b.echo_alarms_from([
        {"deviceSerialNumber": "A", "type": "Alarm", "status": "ON", "alarmTime": (now + 3600) * 1000},
        {"deviceSerialNumber": "A", "type": "Alarm", "status": "ON", "alarmTime": (now + 7200) * 1000},
        {"deviceSerialNumber": "A", "type": "Alarm", "status": "OFF", "alarmTime": (now + 60) * 1000},
        {"deviceSerialNumber": "A", "type": "Timer", "status": "ON", "remainingTime": 600000},
        {"deviceSerialNumber": "B", "type": "Reminder", "status": "ON", "alarmTime": (now + 60) * 1000}], now)
    assert set(al) == {"A"} and set(al["A"]) == {"nextAlarm", "timer"}
    assert b.echo_player_from({"playerInfo": {"state": "PLAYING", "infoText": {"title": "Song", "subText1": "Band"}}}) \
        == {"playback": "playing", "nowPlaying": "Song - Band"}
    assert b.echo_player_from(None) == {"playback": "stopped", "nowPlaying": ""}
    inner = {"command": "PUSH_VOLUME_CHANGE",
             "payload": json.dumps({"dopplerId": {"deviceSerialNumber": "A"}, "volumeSetting": 44})}
    msg = {"directive": {"payload": {"renderingUpdates": [{"resourceMetadata": json.dumps(inner)}]}}}
    (cmd, pl), = b.echo_push_parse(msg)
    assert cmd == "PUSH_VOLUME_CHANGE" and b.echo_push_serial(pl) == "A" and pl["volumeSetting"] == 44
    assert b.echo_push_serial({"key": {"entryId": "1#TYPE#SER"}}) == "SER"
    assert b.echo_push_parse({"junk": 1}) == []
    assert b.echo_iso(1_800_000_000_000) == "2027-01-15T08:00:00Z"


def test_echo_new_commands(monkeypatch):
    import asyncio
    import sys
    import types
    import stoeckie_bridge as b
    calls = []

    class FakeAPI:
        def __init__(self, dev, login):
            pass

        def __getattr__(self, name):
            async def f(*a, **k):
                calls.append((name, a))
            return f
    monkeypatch.setitem(sys.modules, "alexapy", types.SimpleNamespace(AlexaAPI=FakeAPI))
    cloud = b.EchoCloud({"echo": {}})
    cloud.login = object()
    cloud.devs["S1"] = {"serialNumber": "S1", "accountName": "Kitchen", "online": True}
    cloud.bt["S1"] = {"connected": "", "paired": {"JBL": "AA:BB"}}
    h = cloud._command("S1")

    async def go():
        await h("dnd", "on")
        await h("music", "spotify: Coldplay")
        await h("music", "Radio 538")
        await h("sound", "doorbell")
        await h("push", "Hello")
        await h("bluetooth", "jbl")
        await h("bluetooth", "off")
        await h("pause", None)
    asyncio.run(go())
    names = [c[0] for c in calls]
    assert names == ["set_dnd_state", "play_music", "run_custom", "play_sound", "send_mobilepush", "set_bluetooth",
                     "disconnect_bluetooth", "pause"]
    assert calls[1][1] == ("SPOTIFY", "Coldplay") and calls[2][1] == ("play Radio 538",)
    assert calls[5][1] == ("AA:BB",)
    assert b.REG.devices["echo-S1"]["state"]["dnd"] == "on"
