"""Admin tools for Claude: input checks, secret masking, the driver package and the tool gate."""
import asyncio
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
import pytest  # noqa: E402


def test_ref_and_repo_checks():
    assert sb.admin_ref("E1A6B0A") == "e1a6b0a"
    for bad in ("main", "e1a6", "e1a6b0a; rm", "../x", ""):
        with pytest.raises(ValueError):
            sb.admin_ref(bad)
    assert sb.admin_repo(None, "smartthings-lifx-local") == ("Stoeckie", "smartthings-lifx-local")
    for owner, repo in (("someone", "smartthings-x"), ("Stoeckie", "other"), ("Stoeckie", "smartthings-../x")):
        with pytest.raises(ValueError):
            sb.admin_repo(owner, repo)


def test_mask_secrets():
    t = sb.admin_mask('Authorization: Bearer 0123456789abcdef0123 access_token="abcd1234efgh" code 123-45-678 ok')
    assert "0123456789abcdef" not in t and "abcd1234efgh" not in t and "123-45-678" not in t and t.endswith("ok")
    assert sb.admin_mask("[t6] session closed: closed") == "[t6] session closed: closed"


def test_driver_zip_like_the_cli():
    files = {"config.yaml": b'name: "LIFX (local)"\npackageKey: "lifx-local"\n',
             "profiles/a.yaml": b"name: a\n", "profiles/readme.md": b"x",
             "src/init.lua": b"-- init", "src/lib/x.lua": b"--", "src/test/t.lua": b"--", "tests/test_x.py": b"",
             "README.md": b"r"}
    z, info = sb.admin_driver_zip(files, "")
    names = sorted(zipfile.ZipFile(io.BytesIO(z)).namelist())
    assert names == ["config.yml", "profiles/a.yml", "src/init.lua", "src/lib/x.lua"]
    assert info["packageKey"] == "lifx-local" and info["name"] == "LIFX (local)"
    sub = {"driver/" + k: v for k, v in files.items()}
    z2, info2 = sb.admin_driver_zip(sub, "driver")
    assert info2["packageKey"] == "lifx-local"
    with pytest.raises(RuntimeError):
        sb.admin_driver_zip({"src/init.lua": b""}, "")
    with pytest.raises(ValueError):
        sb.admin_driver_zip(files, "../etc")


def test_tools_only_when_switched_on():
    sb.save_config({"claude": {"enabled": True}})
    r = asyncio.run(sb.claude_rpc({"id": 1, "method": "tools/list"}))
    assert "hub_log" not in {t["name"] for t in r["result"]["tools"]}
    r = asyncio.run(sb.claude_rpc({"id": 2, "method": "tools/call", "params": {"name": "bridge_log", "arguments": {}}}))
    assert "error" in r
    sb.save_config({"claude": {"enabled": True, "admin": {"enabled": True}}})
    r = asyncio.run(sb.claude_rpc({"id": 3, "method": "tools/list"}))
    assert {"hub_log", "driver_deploy", "bridge_log", "bridge_update", "admin_hubs"} <= {t["name"] for t in r["result"]["tools"]}
    sb.LOG_FILE.write_text("a line\nBearer abcdefabcdef1234 secret\nlast\n", encoding="utf-8")
    r = asyncio.run(sb.claude_rpc({"id": 4, "method": "tools/call", "params": {"name": "bridge_log",
                                                                                 "arguments": {"lines": 10}}}))
    text = r["result"]["content"][0]["text"]
    assert "last" in text and "abcdefabcdef1234" not in text


def test_bridge_update_needs_the_new_run_cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "HERE", tmp_path)
    (tmp_path / "run.cmd").write_text("python stoeckie_bridge.py")
    with pytest.raises(RuntimeError, match="install-remote"):
        asyncio.run(sb.admin_bridge_update({"ref": "e1a6b0a"}))
