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
