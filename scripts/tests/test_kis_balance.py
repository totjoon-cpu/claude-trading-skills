"""Tests for scripts/kis_balance.py (read-only KIS overseas account snapshot)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "kis_balance.py"

APP_KEY = "PSfakeappkey1234567890"
APP_SECRET = "fakesecretABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
TOKEN = "eyJfake.token.value"


@pytest.fixture(scope="module")
def kis():
    spec = importlib.util.spec_from_file_location("kis_balance", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load kis_balance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    """Records requests and answers them from a routing function."""

    def __init__(self, route):
        self.route = route
        self.calls = []

    def __call__(self, method, url, headers, body):
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        payload = json.loads(body) if body else None
        call = {
            "method": method,
            "path": parsed.path,
            "port": parsed.port,
            "headers": headers,
            "params": params,
            "body": payload,
        }
        self.calls.append(call)
        return self.route(call)


def _ok(output1=None, output2=None, output3=None, tr_cont="", ctx=("", "")):
    body = {"rt_cd": "0", "msg_cd": "OK", "msg1": "정상처리 되었습니다."}
    if output1 is not None:
        body["output1"] = output1
    if output2 is not None:
        body["output2"] = output2
    if output3 is not None:
        body["output3"] = output3
    body["ctx_area_fk200"], body["ctx_area_nk200"] = ctx
    return 200, {"tr_cont": tr_cont}, body


def _holding(symbol, exchange, qty, avg, last):
    return {
        "ovrs_pdno": symbol,
        "ovrs_item_name": f"{symbol} INC",
        "ovrs_excg_cd": exchange,
        "ovrs_cblc_qty": str(qty),
        "pchs_avg_pric": str(avg),
        "now_pric2": str(last),
        "ovrs_stck_evlu_amt": f"{qty * last:.2f}",
        "frcr_evlu_pfls_amt": f"{qty * (last - avg):.2f}",
        "evlu_pfls_rt": f"{(last / avg - 1) * 100:.2f}",
        "frcr_pchs_amt1": f"{qty * avg:.2f}",
    }


def _account_route(holdings_by_exchange, cash_rows, paged_exchange=None):
    """Route token, balance (per exchange, optional 2 pages) and cash calls."""

    def route(call):
        if call["path"] == "/oauth2/tokenP":
            return 200, {}, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 86400}
        if call["path"] == "/oauth2/revokeP":
            return 200, {}, {"code": 200, "message": "ok"}
        if call["path"].endswith("/inquire-balance"):
            exchange = call["params"]["OVRS_EXCG_CD"]
            rows = holdings_by_exchange.get(exchange, [])
            if exchange == paged_exchange:
                if call["headers"].get("tr_cont") == "N":
                    return _ok(output1=rows[1:], output2={})
                return _ok(output1=rows[:1], output2={}, tr_cont="M", ctx=("FK1", "NK1"))
            return _ok(output1=rows, output2={})
        if call["path"].endswith("/inquire-present-balance"):
            return _ok(output1=[], output2=cash_rows, output3={"tot_asst_amt": "7500000"})
        raise AssertionError(f"unexpected call {call['path']}")

    return route


# --- account parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12345678-01", ("12345678", "01")),
        ("1234567801", ("12345678", "01")),
        ("12345678", ("12345678", "01")),
        (" 12345678-22 ", ("12345678", "22")),
    ],
)
def test_parse_account_accepts_common_formats(kis, raw, expected):
    assert kis.parse_account(raw) == expected


def test_parse_account_uses_product_code_for_eight_digits(kis):
    assert kis.parse_account("12345678", product_cd="22") == ("12345678", "22")


@pytest.mark.parametrize("raw", ["", "1234", "abcdefgh-01", "12345678-1"])
def test_parse_account_rejects_bad_values(kis, raw):
    with pytest.raises(ValueError):
        kis.parse_account(raw)


def test_mask_account_keeps_only_last_four_digits(kis):
    assert kis.mask_account("12345678", "01") == "****5678-01"


# --- read-only guarantees --------------------------------------------------


def test_client_refuses_tr_outside_allowlist(kis):
    client = kis.KisClient(APP_KEY, APP_SECRET, transport=FakeTransport(lambda c: None))
    with pytest.raises(kis.KisError):
        client.get("order", {})


def test_module_has_no_order_endpoints(kis):
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/trading/order" not in source
    for tr_id in kis.READ_ONLY_TRS.values():
        assert tr_id["real"].endswith("R") and tr_id["mock"].endswith("R")


def test_real_and_mock_hosts(kis):
    transport = FakeTransport(_account_route({}, []))
    kis.KisClient(APP_KEY, APP_SECRET, env="real", transport=transport).issue_token()
    kis.KisClient(APP_KEY, APP_SECRET, env="mock", transport=transport).issue_token()
    assert [c["port"] for c in transport.calls] == [9443, 29443]


# --- holdings and cash -----------------------------------------------------


def test_fetch_holdings_dedupes_exchanges_and_follows_pages(kis):
    sndk = _holding("SNDK", "NASD", 2, 1514.98, 1300.0)
    csco = _holding("CSCO", "NASD", 8, 115.6725, 120.0)
    mrk = _holding("MRK", "NYSE", 3, 150.0, 155.0)
    sold_out = _holding("AMD", "NASD", 0, 100.0, 110.0)
    transport = FakeTransport(
        _account_route(
            # NASD returns all US rows on a real account; NYSE repeats MRK.
            {"NASD": [sndk, csco, mrk, sold_out], "NYSE": [mrk]},
            [],
            paged_exchange="NASD",
        )
    )
    client = kis.KisClient(APP_KEY, APP_SECRET, transport=transport)
    client.issue_token()

    holdings = kis.fetch_holdings(client, "12345678", "01")

    assert [h["symbol"] for h in holdings] == ["SNDK", "CSCO", "MRK"]
    sndk_row = holdings[0]
    assert sndk_row["qty"] == 2
    assert sndk_row["avg_price"] == pytest.approx(1514.98)
    assert sndk_row["last_price"] == pytest.approx(1300.0)
    assert sndk_row["market_value"] == pytest.approx(2600.0)
    balance_calls = [c for c in transport.calls if c["path"].endswith("/inquire-balance")]
    second_page = [c for c in balance_calls if c["headers"].get("tr_cont") == "N"]
    assert second_page and second_page[0]["params"]["CTX_AREA_NK200"] == "NK1"
    assert all(c["headers"]["tr_id"] == "TTTS3012R" for c in balance_calls)
    assert all(c["method"] == "GET" for c in balance_calls)


def test_fetch_cash_reads_usd_row(kis):
    cash_rows = [
        {"crcy_cd": "USD", "frcr_dncl_amt_2": "1932.30", "frcr_drwg_psbl_amt_1": "1800.00"},
        {"crcy_cd": "JPY", "frcr_dncl_amt_2": "0", "frcr_drwg_psbl_amt_1": "0"},
    ]
    transport = FakeTransport(_account_route({}, cash_rows))
    client = kis.KisClient(APP_KEY, APP_SECRET, transport=transport)
    client.issue_token()

    cash = kis.fetch_cash(client, "12345678", "01")

    assert cash["usd_deposit"] == pytest.approx(1932.30)
    assert cash["usd_withdrawable"] == pytest.approx(1800.0)
    assert "frcr_dncl_amt_2" in cash["fields_seen"]["output2"]
    call = next(c for c in transport.calls if c["path"].endswith("/inquire-present-balance"))
    assert call["headers"]["tr_id"] == "CTRP6504R"


def test_api_error_message_is_raised_without_secrets(kis):
    def route(call):
        if call["path"] == "/oauth2/tokenP":
            return 200, {}, {"access_token": TOKEN}
        return 200, {}, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": f"bad key {APP_KEY}"}

    client = kis.KisClient(APP_KEY, APP_SECRET, transport=FakeTransport(route))
    client.issue_token()
    with pytest.raises(kis.KisError) as excinfo:
        kis.fetch_cash(client, "12345678", "01")
    assert "EGW00123" in str(excinfo.value)
    assert APP_KEY not in str(excinfo.value)


# --- CLI -------------------------------------------------------------------


def _env(**extra):
    env = {
        "KIS_APP_KEY": APP_KEY,
        "KIS_APP_SECRET": APP_SECRET,
        "KIS_ACCOUNT_NO": "12345678-01",
    }
    env.update(extra)
    return env


def test_main_accepts_kis_trading_mcp_variable_names(kis, tmp_path):
    transport = FakeTransport(_account_route({}, []))
    environ = {
        "KIS_API_KEY": APP_KEY,
        "KIS_API_SECRET": APP_SECRET,
        "KIS_ACCT_STOCK": "12345678",
        "KIS_PROD_TYPE": "22",
    }

    code = kis.main(["--output", str(tmp_path / "s.json")], environ=environ, transport=transport)

    assert code == 0
    token_call = transport.calls[0]
    assert token_call["body"]["appkey"] == APP_KEY
    balance_call = next(c for c in transport.calls if c["path"].endswith("/inquire-balance"))
    assert balance_call["params"]["ACNT_PRDT_CD"] == "22"


def test_main_reports_missing_variables(kis, capsys):
    code = kis.main(["--output", "-"], environ={}, transport=FakeTransport(lambda c: None))
    err = capsys.readouterr().err
    assert code == 1
    assert "KIS_APP_KEY" in err and "KIS_ACCOUNT_NO" in err


def test_main_writes_snapshot_without_secrets(kis, tmp_path, capsys):
    transport = FakeTransport(
        _account_route(
            {"NASD": [_holding("CSCO", "NASD", 8, 115.6725, 120.0)]},
            [{"crcy_cd": "USD", "frcr_dncl_amt_2": "1932.30", "frcr_drwg_psbl_amt_1": "1800"}],
        )
    )
    out = tmp_path / "snapshot.json"

    code = kis.main(["--output", str(out)], environ=_env(), transport=transport)

    assert code == 0
    text = out.read_text(encoding="utf-8")
    snapshot = json.loads(text)
    assert snapshot["account"] == "****5678-01"
    assert snapshot["holdings"][0]["symbol"] == "CSCO"
    assert snapshot["cash"]["usd_deposit"] == pytest.approx(1932.30)
    assert snapshot["totals"]["stock_value_usd"] == pytest.approx(960.0)
    assert snapshot["totals"]["account_total_usd"] == pytest.approx(2892.30)
    printed = capsys.readouterr()
    for secret in (APP_KEY, APP_SECRET, TOKEN, "12345678"):
        assert secret not in text
        assert secret not in printed.out
        assert secret not in printed.err
    # The token is revoked at the end of the run.
    assert transport.calls[-1]["path"] == "/oauth2/revokeP"
