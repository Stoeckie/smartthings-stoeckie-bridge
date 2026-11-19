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
    assert "hub_log" in {t["name"] for t in r["result"]["tools"]}  # always listed, so the list never changes
    r = asyncio.run(sb.claude_rpc({"id": 2, "method": "tools/call", "params": {"name": "bridge_log", "arguments": {}}}))
    assert r["result"]["isError"] and "off" in r["result"]["content"][0]["text"]
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


def test_fetch_reads_the_whole_archive(monkeypatch):
    """GitHub sends the archive in pieces: all of them must be read (19c read only the first ones)."""
    import gzip
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(30):
            data = os.urandom(20000)
            ti = tarfile.TarInfo(f"repo-abc/src/f{i}.bin")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    blob = buf.getvalue()
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def handler(req):
        resp = web.StreamResponse()
        await resp.prepare(req)
        for i in range(0, len(blob), 7000):
            await resp.write(blob[i:i + 7000])
            await asyncio.sleep(0.001)
        return resp

    async def main():
        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        srv = TestServer(app)
        await srv.start_server()
        real = sb.aiohttp.ClientSession

        class S(real):
            def get(self, url, **kw):
                return super().get(str(srv.make_url("/x")), **kw)
        monkeypatch.setattr(sb.aiohttp, "ClientSession", S)
        files = await sb.admin_fetch_tree("Stoeckie", "smartthings-x", "abcdef1")
        await srv.close()
        return files
    files = asyncio.run(main())
    assert len(files) == 30 and all(len(v) == 20000 for v in files.values())


def test_update_with_other_packages_waits_for_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "HERE", tmp_path)
    monkeypatch.setattr(sb, "STAGED", tmp_path / "staged")
    (tmp_path / "run.cmd").write_text("if exist stoeckie_bridge.next.py ...")
    (tmp_path / "requirements.txt").write_text("aiohttp==3.9.0 \\\n    --hash=sha256:aa\n")
    new_req = b"aiohttp==3.9.5 \\\n    --hash=sha256:bb\nnewpkg==1.0 \\\n    --hash=sha256:cc\n"

    async def fake_tree(owner, repo, ref):
        return {"bridge/stoeckie_bridge.py": b'VERSION = "2099-01-01a"\n', "bridge/requirements.txt": new_req}

    ages = {"aiohttp": 30.0, "newpkg": 10.0}

    async def fake_age(session, name, version):
        return ages[name]
    monkeypatch.setattr(sb, "admin_fetch_tree", fake_tree)
    monkeypatch.setattr(sb, "_pkg_age_days", fake_age)
    r = asyncio.run(sb.admin_bridge_update({"ref": "abcdef1"}))
    assert r["needs_approval"] and {c["package"] for c in r["package_changes"]} == {"aiohttp", "newpkg"}
    st = sb.staged_update()
    assert st["state"] == "waiting" and st["to"] == "2099-01-01a"
    html = sb.claude_setup_html({"claude": {}})
    assert "waiting for your approval" in html and "aiohttp: 3.9.0" in html and "newpkg: new" in html
    assert not (tmp_path / "stoeckie_bridge.next.py").exists()
    ages["newpkg"] = 2.0  # too young: refused
    with pytest.raises(RuntimeError, match="younger than 7 days"):
        asyncio.run(sb.admin_bridge_update({"ref": "abcdef1"}))


def test_approved_update_installs_tests_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "HERE", tmp_path)
    monkeypatch.setattr(sb, "STAGED", tmp_path / "staged")
    (tmp_path / "staged").mkdir()
    (tmp_path / "staged" / "stoeckie_bridge.py").write_bytes(b'VERSION = "2099-01-01a"\n')
    (tmp_path / "staged" / "requirements.txt").write_bytes(b"x==1 --hash=sha256:aa\n")
    (tmp_path / "requirements.txt").write_bytes(b"x==0 --hash=sha256:bb\n")
    import json as _j
    (tmp_path / "staged" / "meta.json").write_text(_j.dumps({"t": sb.time.time(), "ref": "abcdef1", "from": "old",
                                                             "to": "2099-01-01a", "changes": [], "state": "waiting"}))
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(args) or R())
    monkeypatch.setattr(sb, "_load_test", lambda path, want: (True, ""))
    restarted = []
    monkeypatch.setattr(sb, "_restart_into", lambda new, v, ref: restarted.append(v))
    asyncio.run(sb.apply_staged_update())
    assert calls and "--require-hashes" in calls[0] and restarted == ["2099-01-01a"]
    assert (tmp_path / "requirements.txt").read_bytes() == b"x==1 --hash=sha256:aa\n"
    assert (tmp_path / "requirements.prev.txt").read_bytes() == b"x==0 --hash=sha256:bb\n"
    assert sb.staged_update()["state"] == "done"


RUN_HEAD = (b'@echo off\r\ncd /d "%~dp0"\r\nset STBRIDGE_SETUP_ALLOW=1.2.3.4\r\n:loop\r\n'
            b'if exist stoeckie_bridge.next.py (copy /y a b >nul)\r\n'
            b'"%~dp0venv\\Scripts\\python.exe" "%~dp0stoeckie_bridge.py"\r\n')
RUN_TAIL_OLD = b"set rc=%errorlevel%\r\nif %rc%==75 goto loop\r\nexit /b %rc%\r\n"


def test_run_cmd_tail_waits_for_approval_and_keeps_head(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "HERE", tmp_path)
    monkeypatch.setattr(sb, "STAGED", tmp_path / "staged")
    (tmp_path / "run.cmd").write_bytes(RUN_HEAD + RUN_TAIL_OLD)
    tail_repo = (Path(__file__).parent.parent / "run-tail.cmd").read_bytes()

    async def fake_tree(owner, repo, ref):
        return {"bridge/stoeckie_bridge.py": b'VERSION = "2099-01-01a"\n', "bridge/run-tail.cmd": tail_repo}
    monkeypatch.setattr(sb, "admin_fetch_tree", fake_tree)
    r = asyncio.run(sb.admin_bridge_update({"ref": "abcdef1"}))
    assert r["needs_approval"] and r["run_cmd_change"] and r["package_changes"] == []
    html = sb.claude_setup_html({"claude": {}})
    assert "run.cmd" in html and "start again in 30 s" in html
    monkeypatch.setattr(sb, "_load_test", lambda path, want: (True, ""))
    restarted = []
    monkeypatch.setattr(sb, "_restart_into", lambda new, v, ref: restarted.append(v))
    asyncio.run(sb.apply_staged_update())
    now = (tmp_path / "run.cmd").read_bytes()
    assert now.startswith(RUN_HEAD) and now[len(RUN_HEAD):] == tail_repo.replace(b"\r", b"").replace(b"\n", b"\r\n")
    assert (tmp_path / "run.prev.cmd").read_bytes() == RUN_HEAD + RUN_TAIL_OLD
    assert restarted == ["2099-01-01a"] and sb.staged_update()["state"] == "done"
    # same tail again: no approval needed
    (tmp_path / "stoeckie_bridge.next.py").unlink(missing_ok=True)
    r = asyncio.run(sb.admin_bridge_update({"ref": "abcdef1"}))
    assert "needs_approval" not in r


def test_run_tail_rules():
    with pytest.raises(RuntimeError):
        sb.run_tail_check(b'"%~dp0venv\\Scripts\\python.exe" "%~dp0stoeckie_bridge.py"\r\ngoto loop\r\n')
    with pytest.raises(RuntimeError):
        sb.run_tail_check(b"exit /b 0\r\n")
    assert sb.run_tail_check(b"goto loop\n\n") == b"goto loop\r\n"
    assert sb.run_split(b"no python line") is None


def test_installer_tail_matches_repo_tail():
    here = Path(__file__).parent.parent
    ps1 = (here / "install-remote.ps1").read_text(encoding="utf-8")
    i = ps1.index('$run += "`"%~dp0venv\\Scripts\\python.exe`"')
    lines = []
    for ln in ps1[i:].splitlines()[1:]:
        ln = ln.strip()
        if not ln.startswith('$run += "'):
            break
        lines.append(ln[len('$run += "'):-1].replace("`\"", '"'))
    assert lines == (here / "run-tail.cmd").read_text().replace("\r", "").strip().split("\n")
