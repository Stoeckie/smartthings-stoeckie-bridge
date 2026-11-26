"""Docker variant (bridge 2026-11-26a): self update under run.sh, X-Forwarded-For from a trusted reverse proxy."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402

BRIDGE = Path(__file__).resolve().parents[1]


def req(remote, xff=None):
    return SimpleNamespace(remote=remote, headers={"X-Forwarded-For": xff} if xff else {})


def test_forwarded_address_only_from_loopback_or_trusted_proxy(monkeypatch):
    monkeypatch.delenv("STBRIDGE_TRUSTED_PROXIES", raising=False)
    assert sb.claude_client_ip(req("127.0.0.1", "160.79.105.10")) == "160.79.105.10"
    # a LAN device can not pretend to be Anthropic
    assert sb.claude_client_ip(req("192.168.20.10", "160.79.105.10")) == "192.168.20.10"
    monkeypatch.setenv("STBRIDGE_TRUSTED_PROXIES", "192.168.20.10, not-an-ip")
    assert sb.claude_client_ip(req("192.168.20.10", "160.79.105.10, 10.0.0.1")) == "160.79.105.10"
    assert sb.claude_client_ip(req("192.168.20.11", "160.79.105.10")) == "192.168.20.11"


def test_docker_update_without_run_cmd_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "HERE", tmp_path)
    monkeypatch.setenv("STBRIDGE_RUNNER", "sh")
    (tmp_path / "requirements.txt").write_bytes(b"aiohttp==3.9.0 \\\n    --hash=sha256:aa\n")
    new = b'VERSION = "2099-01-01a"\n'

    async def fake_tree(owner, repo, ref):
        return {"bridge/stoeckie_bridge.py": new, "bridge/requirements.txt": b"aiohttp==3.9.0 \\\n    --hash=sha256:aa\n",
                "bridge/run-tail.cmd": b"something else\r\ngoto loop\r\n"}
    restarted = {}
    monkeypatch.setattr(sb, "admin_fetch_tree", fake_tree)
    monkeypatch.setattr(sb, "_load_test", lambda path, want: (True, ""))
    monkeypatch.setattr(sb, "_restart_into", lambda n, v, r: restarted.update(v=v))
    r = asyncio.run(sb.admin_bridge_update({"ref": "abcdef1"}))
    assert restarted == {"v": "2099-01-01a"} and "run.sh" in r["note"] and "needs_approval" not in r


def test_docker_update_with_other_packages_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "HERE", tmp_path)
    monkeypatch.setenv("STBRIDGE_RUNNER", "sh")
    (tmp_path / "requirements.txt").write_bytes(b"aiohttp==3.9.0 \\\n    --hash=sha256:aa\n")

    async def fake_tree(owner, repo, ref):
        return {"bridge/stoeckie_bridge.py": b'VERSION = "2099-01-01a"\n',
                "bridge/requirements.txt": b"aiohttp==3.9.5 \\\n    --hash=sha256:bb\n"}
    monkeypatch.setattr(sb, "admin_fetch_tree", fake_tree)
    with pytest.raises(RuntimeError, match="docker compose"):
        asyncio.run(sb.admin_bridge_update({"ref": "abcdef1"}))


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
def test_run_sh_applies_update_and_rolls_back(tmp_path):
    """run.sh with a fake python: first start copies the image code, exit 75 applies next.py, a failing update goes back."""
    app, data, binp = tmp_path / "app", tmp_path / "data", tmp_path / "bin"
    for d in (app, data, binp):
        d.mkdir()
    (app / "stoeckie_bridge.py").write_text('VERSION = "2026-11-26a"\n')
    (app / "requirements.txt").write_text("x\n")
    (app / "config.example.json").write_text("{}\n")
    script = (BRIDGE / "run.sh").read_text().replace("/app/", str(app) + "/")
    (tmp_path / "run.sh").write_text(script)
    # fake python: run 1 leaves an update and asks for a restart, run 2 (the update) crashes, run 3 stops cleanly
    (binp / "python").write_text(
        "#!/bin/sh\nn=$(cat count 2>/dev/null || echo 0); n=$((n+1)); echo $n > count\n"
        "grep VERSION stoeckie_bridge.py >> seen\n"
        "if [ $n -eq 1 ]; then echo 'VERSION = \"2099-01-01a\"' > stoeckie_bridge.next.py; exit 75; fi\n"
        "if [ $n -eq 2 ]; then exit 1; fi\nexit 0\n")
    (binp / "python").chmod(0o755)
    env = {**os.environ, "PATH": f"{binp}:{os.environ['PATH']}", "STBRIDGE_DATA": str(data)}
    subprocess.run(["sh", str(tmp_path / "run.sh")], env=env, timeout=30, check=True)
    seen = (data / "code" / "seen").read_text().splitlines()
    assert seen == ['VERSION = "2026-11-26a"', 'VERSION = "2099-01-01a"', 'VERSION = "2026-11-26a"']
    assert "previous version back" in (data / "code" / "run-restarts.log").read_text()
