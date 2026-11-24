"""Proxmox admin tools: input checks, the tool list and calls without a token."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("STBRIDGE_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stoeckie_bridge as sb  # noqa: E402
import pytest  # noqa: E402


def test_tools_listed():
    names = {t["name"] for t in sb.ADMIN_TOOLS}
    assert {"pve_vms", "pve_exec", "pve_power", "pve_snapshot"} <= names <= sb.ADMIN_NAMES


def test_vmid_checks():
    assert sb.pve_vmid({"vmid": "101"}) == 101
    for bad in (None, "x", 5, "101; rm"):
        with pytest.raises(ValueError):
            sb.pve_vmid({"vmid": bad})


def test_no_token_no_call(monkeypatch):
    monkeypatch.setattr(sb, "load_config", lambda: {"claude": {"admin": {"enabled": True}}})
    with pytest.raises(RuntimeError, match="not set up"):
        asyncio.run(sb.pve_call("GET", "/version"))


def test_power_and_snapshot_refuse_bad_actions(monkeypatch):
    monkeypatch.setattr(sb, "load_config", lambda: {"claude": {"admin": {"proxmox": {"node": "Stoeck"}}}})
    with pytest.raises(ValueError):
        asyncio.run(sb.admin_pve_power({"vmid": 101, "action": "destroy"}))
    with pytest.raises(ValueError):
        asyncio.run(sb.admin_pve_snapshot({"vmid": 101, "action": "rollback"}))
    with pytest.raises(ValueError):
        asyncio.run(sb.admin_pve_snapshot({"vmid": 101, "action": "create", "name": "1bad name"}))
    with pytest.raises(ValueError):
        asyncio.run(sb.admin_pve_exec({"vmid": 101, "command": "  "}))
