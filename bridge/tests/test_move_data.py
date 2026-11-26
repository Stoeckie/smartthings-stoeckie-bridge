"""move_data.py: data of the bridge moves encrypted; only the new machine can open it."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import move_data as md  # noqa: E402


def test_roundtrip_and_refusals(tmp_path, capsys):
    old = tmp_path / "old"
    (old / "homeconnect").mkdir(parents=True)
    (old / "config.json").write_text('{"secret": "x"}')
    (old / "claude-tokens.json").write_text("{}")
    (old / "homeconnect" / "dish.zip").write_bytes(b"zip")
    (old / "bridge.log").write_text("log")
    (old / "stoeckie_bridge.py").write_text("code")
    md.keygen(tmp_path / "k")
    md.export(tmp_path / "k" / "move-pub.pem", old, tmp_path / "m.enc")
    out = capsys.readouterr().out
    assert "secret" not in out and "bridge.log" not in out and "stoeckie_bridge.py" not in out
    assert b"secret" not in (tmp_path / "m.enc").read_bytes()
    new = tmp_path / "new"
    md.import_(tmp_path / "k" / "move-key.pem", tmp_path / "m.enc", new, force=False)
    assert (new / "config.json").read_text() == '{"secret": "x"}'
    assert (new / "homeconnect" / "dish.zip").read_bytes() == b"zip"
    assert not (new / "bridge.log").exists()
    with pytest.raises(SystemExit, match="--force"):
        md.import_(tmp_path / "k" / "move-key.pem", tmp_path / "m.enc", new, force=False)
    md.keygen(tmp_path / "other")
    with pytest.raises(ValueError):  # another key can not open it
        md.import_(tmp_path / "other" / "move-key.pem", tmp_path / "m.enc", tmp_path / "x", force=False)


def test_unsafe_names():
    assert not md._safe("../etc/passwd") and not md._safe("/abs") and not md._safe("C:/x")
    assert md._safe("homeconnect/a.zip")


def test_receive_once_from_allowed_net(tmp_path):
    import json
    import threading
    import time
    import urllib.request
    old = tmp_path / "old"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({"claude": {"public_url": "https://x.ts.net"}}))
    md.keygen(tmp_path / "k")
    md.export(tmp_path / "k" / "move-pub.pem", old, tmp_path / "m.enc")
    new = tmp_path / "new"
    t = threading.Thread(target=md.receive, args=(tmp_path / "k" / "move-key.pem", new, 18791, "127.0.0.0/8",
                                                    "https://home.example/"), daemon=True)
    t.start()
    time.sleep(0.5)
    bad = urllib.request.Request("http://127.0.0.1:18791/move", data=b"STBMOVE1garbage", method="POST")
    with pytest.raises(urllib.error.HTTPError):  # a wrong file is refused, the receiver keeps waiting
        urllib.request.urlopen(bad, timeout=5)
    ok = urllib.request.Request("http://127.0.0.1:18791/move", data=(tmp_path / "m.enc").read_bytes(), method="POST")
    assert urllib.request.urlopen(ok, timeout=10).read() == b"imported\n"
    t.join(5)
    assert not t.is_alive()
    assert json.loads((new / "config.json").read_text())["claude"]["public_url"] == "https://home.example"
