import asyncio

import stoeckie_bridge as sb


def test_parse_numbers_and_names():
    raw = '[{"Name":"Stoeck-TV","InterfaceAlias":"Wi-Fi","NetworkCategory":0,"IPv4Connectivity":4},' \
          '{"Name":"vEthernet","InterfaceAlias":"vEth","NetworkCategory":1,"IPv4Connectivity":0}]'
    p = sb.netprofile_parse(raw)
    assert p[0] == {"name": "Stoeck-TV", "category": "Public", "ipv4": "Internet"}
    assert p[1]["category"] == "Private" and p[1]["ipv4"] == "Disconnected"
    assert sb.netprofile_parse('{"Name":"x","NetworkCategory":"Private","IPv4Connectivity":"Internet"}')[0]["category"] == "Private"


def test_once_warns_and_pushes_once(monkeypatch):
    out = {"raw": b'{"Name":"Stoeck-TV","NetworkCategory":0,"IPv4Connectivity":4}'}

    runs = []

    class P:
        returncode = 0

        def __init__(self, args):
            self.args = args
            if args[0] == "schtasks":
                self.returncode = 1  # task not made yet

        async def communicate(self):
            if self.args[0] == "schtasks":
                runs.append(self.args)
                return b"", b"ERROR: The system cannot find the file specified."
            return out["raw"], b""

    async def fake_run(args, timeout=30):
        p = P(tuple(args))
        o, e = await p.communicate()
        return p.returncode, o.decode(), e.decode()
    monkeypatch.setattr(sb, "_run_cmd", fake_run)
    pushes = []

    class E:
        async def push_to_phone(self, title, msg):
            pushes.append(msg)
            return True
    monkeypatch.setattr(sb, "ECHO_NOW", E())
    sb.NETPROFILE.update(public=None, names=[], pushed=0.0, error="", fixed=0.0)
    asyncio.run(sb.netprofile_once())
    asyncio.run(sb.netprofile_once())
    assert sb.NETPROFILE["public"] is True and len(pushes) == 1 and "Stoeck-TV" in pushes[0]
    assert runs and runs[0][:3] == ("schtasks", "/Run", "/TN") and len(runs) == 1  # at most once per 4 min
    assert sb.netprofile_summary()[0].startswith("NOT private")
    out["raw"] = b'{"Name":"Stoeck-TV","NetworkCategory":1,"IPv4Connectivity":4}'
    asyncio.run(sb.netprofile_once())
    assert sb.NETPROFILE["public"] is False and sb.netprofile_summary()[0].startswith("Private")
