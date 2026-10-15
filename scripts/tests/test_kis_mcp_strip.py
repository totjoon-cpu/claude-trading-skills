"""Tests for scripts/kis_mcp/strip_order_apis.py (read-only KIS Trading MCP)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "kis_mcp" / "strip_order_apis.py"


@pytest.fixture(scope="module")
def strip():
    spec = importlib.util.spec_from_file_location("strip_order_apis", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load strip_order_apis.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WRITE_APIS = [
    "order",
    "order_cash",
    "order_credit",
    "order_rvsecncl",
    "order_resv",
    "order_resv_rvsecncl",
    "daytime_order",
    "daytime_order_rvsecncl",
    "buy",
    "sell",
    "inquire_psbl_rvsecncl",
]
READ_APIS = [
    "inquire_balance",
    "inquire_present_balance",
    "inquire_ccnl",
    "inquire_ccld",
    "inquire_period_profit",
    "inquire_psamount",
    "price",
    "algo_ordno",
]


@pytest.mark.parametrize("api_type", WRITE_APIS)
def test_write_apis_are_detected(strip, api_type):
    assert strip.is_write_api(api_type)


@pytest.mark.parametrize("api_type", READ_APIS)
def test_read_apis_are_kept(strip, api_type):
    assert not strip.is_write_api(api_type)


def _write_config(directory: Path, name: str, api_types: list[str]) -> Path:
    path = directory / f"{name}.json"
    apis = {api: {"name": api, "github_url": f"https://github.com/x/{api}"} for api in api_types}
    path.write_text(
        json.dumps({"tool_info": {"name": name}, "apis": apis}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_strip_configs_removes_only_write_apis(strip, tmp_path):
    path = _write_config(tmp_path, "overseas_stock", ["inquire_balance", "order", "price", "sell"])

    removed = strip.strip_configs(tmp_path)

    assert sorted(removed) == ["overseas_stock.json:order", "overseas_stock.json:sell"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(data["apis"]) == ["inquire_balance", "price"]
    assert data["tool_info"] == {"name": "overseas_stock"}


def test_check_mode_reports_without_changing(strip, tmp_path):
    path = _write_config(tmp_path, "domestic_stock", ["order_cash", "inquire_balance"])
    before = path.read_text(encoding="utf-8")

    found = strip.strip_configs(tmp_path, check_only=True)

    assert found == ["domestic_stock.json:order_cash"]
    assert path.read_text(encoding="utf-8") == before


def test_cli_check_fails_until_stripped(strip, tmp_path, capsys):
    _write_config(tmp_path, "overseas_stock", ["order", "inquire_balance"])

    assert strip.main([str(tmp_path), "--check"]) == 1
    assert strip.main([str(tmp_path)]) == 0
    assert strip.main([str(tmp_path), "--check"]) == 0
    assert "order" in capsys.readouterr().out


def test_cli_rejects_directory_without_configs(strip, tmp_path):
    assert strip.main([str(tmp_path)]) == 1
