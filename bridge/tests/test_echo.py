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
