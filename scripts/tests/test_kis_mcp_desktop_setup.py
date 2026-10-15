"""Tests for scripts/kis_mcp/setup_desktop.py (read-only KIS MCP for Claude Desktop)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

KIS_MCP_DIR = Path(__file__).resolve().parents[1] / "kis_mcp"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, KIS_MCP_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def setup():
    return _load("setup_desktop")


def test_write_api_rule_matches_strip_script(setup):
    strip = _load("strip_order_apis")
    assert setup.WRITE_API_PATTERN.pattern == strip.WRITE_API_PATTERN.pattern


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12345678-01", ("12345678", "01")),
        ("1234567822", ("12345678", "22")),
        ("12345678", ("12345678", "01")),
    ],
)
def test_parse_account(setup, raw, expected):
    assert setup.parse_account(raw) == expected


def test_parse_account_rejects_garbage(setup):
    with pytest.raises(ValueError):
        setup.parse_account("12-34")


def test_config_candidates_per_platform(setup, tmp_path):
    home = tmp_path / "home"
    mac = setup.desktop_config_paths("darwin", {}, home)
    assert mac == [
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    ]

    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    store = local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    win = setup.desktop_config_paths(
        "win32", {"APPDATA": str(appdata), "LOCALAPPDATA": str(local)}, home
    )
    assert win[0] == appdata / "Claude" / "claude_desktop_config.json"
    assert store / "claude_desktop_config.json" in win


def test_merge_config_keeps_other_servers_and_backs_up(setup, tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    entry = {"command": "uv", "args": ["run"], "env": {"ENV": "live"}}

    backup = setup.merge_desktop_config(path, "kis-trading", entry)

    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["kis-trading"] == entry
    assert data["theme"] == "dark"
    assert backup is not None and json.loads(backup.read_text())["mcpServers"] == {
        "other": {"command": "x"}
    }


def test_merge_config_creates_missing_file(setup, tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"

    backup = setup.merge_desktop_config(path, "kis-trading", {"command": "uv"})

    assert backup is None
    assert json.loads(path.read_text()) == {"mcpServers": {"kis-trading": {"command": "uv"}}}


def test_extract_server_strips_order_apis(setup, tmp_path):
    buffer = io.BytesIO()
    root = "open-trading-api-abc/MCP/Kis Trading MCP"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{root}/server.py", "print('hi')\n")
        archive.writestr(
            f"{root}/configs/overseas_stock.json",
            json.dumps({"apis": {"inquire_balance": {}, "order": {}, "daytime_order": {}}}),
        )
        archive.writestr("open-trading-api-abc/README.md", "other files are skipped")
    dest = tmp_path / "kis-trading-mcp"

    removed = setup.install_server(buffer.getvalue(), dest)

    assert (dest / "server.py").exists()
    assert not (dest / "README.md").exists()
    config = json.loads((dest / "configs" / "overseas_stock.json").read_text())
    assert list(config["apis"]) == ["inquire_balance"]
    assert sorted(removed) == ["overseas_stock.json:daytime_order", "overseas_stock.json:order"]
    assert setup.write_apis_left(dest / "configs") == []


def test_check_token_reports_success_without_secrets(setup):
    calls = []

    def fake_post(url, payload):
        calls.append((url, payload))
        if url.endswith("/oauth2/tokenP"):
            return 200, {"access_token": "tok", "expires_in": 86400}
        return 200, {"code": 200}

    ok, message = setup.check_token("KEY123", "SECRET456", post=fake_post)

    assert ok
    assert "KEY123" not in message and "SECRET456" not in message and "tok" not in message
    assert calls[-1][0].endswith("/oauth2/revokeP")


def test_check_token_reports_failure(setup):
    ok, message = setup.check_token(
        "KEY123", "SECRET456", post=lambda url, payload: (403, {"error_code": "EGW00103"})
    )
    assert not ok and "EGW00103" in message
