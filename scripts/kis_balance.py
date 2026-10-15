#!/usr/bin/env python3
"""Read-only snapshot of a Korea Investment & Securities (KIS) US stock account.

Fetches US holdings and foreign-currency cash through the KIS Open API inquiry
endpoints and writes them as JSON. Only the allowlisted read-only TR IDs in
READ_ONLY_TRS can be sent; there is no order code path. Credentials come from
environment variables and never appear in the output.

Environment variables (the KIS Trading MCP names in brackets also work):
    KIS_APP_KEY, KIS_APP_SECRET  App key and secret from KIS Developers
                                 [KIS_API_KEY, KIS_API_SECRET]
    KIS_ACCOUNT_NO               Account number: "12345678-01", 10 digits, or 8 digits
                                 [KIS_ACCT_STOCK]
    KIS_ACCOUNT_PRODUCT_CD       Product code for an 8-digit account number (default "01")
                                 [KIS_PROD_TYPE]
    KIS_ENV                      "real" (default) or "mock"

Usage:
    python3 scripts/kis_balance.py                      # writes reports/kis_balance_<date>.json
    python3 scripts/kis_balance.py --output -           # prints the JSON instead
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

HOSTS = {
    "real": "https://openapi.koreainvestment.com:9443",
    "mock": "https://openapivts.koreainvestment.com:29443",
}

# Inquiry-only TR IDs (the trailing "R" marks a read request at KIS).
READ_ONLY_TRS = {
    "balance": {
        "path": "/uapi/overseas-stock/v1/trading/inquire-balance",
        "real": "TTTS3012R",
        "mock": "VTTS3012R",
    },
    "present_balance": {
        "path": "/uapi/overseas-stock/v1/trading/inquire-present-balance",
        "real": "CTRP6504R",
        "mock": "VTRP6504R",
    },
}

# On a real account "NASD" already covers every US exchange; the others are
# queried too so mock accounts work, and duplicates are dropped.
US_EXCHANGES = ("NASD", "NYSE", "AMEX")
MAX_PAGES = 10
RATE_LIMIT_CODE = "EGW00201"
# Setting -> accepted variable names, first one is the documented name.
ENV_NAMES = {
    "app_key": ("KIS_APP_KEY", "KIS_API_KEY"),
    "app_secret": ("KIS_APP_SECRET", "KIS_API_SECRET"),
    "account": ("KIS_ACCOUNT_NO", "KIS_ACCT_STOCK"),
    "product": ("KIS_ACCOUNT_PRODUCT_CD", "KIS_PROD_TYPE"),
}
REQUIRED_SETTINGS = ("app_key", "app_secret", "account")


class KisError(Exception):
    """KIS API call failed; the message never contains credentials."""


def parse_account(raw: str, product_cd: str = "01") -> tuple[str, str]:
    """Split an account number into (CANO, ACNT_PRDT_CD)."""
    value = (raw or "").strip()
    match = re.fullmatch(r"(\d{8})-?(\d{2})?", value)
    if not match:
        raise ValueError(
            "KIS_ACCOUNT_NO must look like 12345678-01 (8 digits, optional 2-digit code)"
        )
    cano, prdt = match.group(1), match.group(2) or product_cd
    if not re.fullmatch(r"\d{2}", prdt or ""):
        raise ValueError("Account product code must be 2 digits")
    return cano, prdt


def mask_account(cano: str, prdt: str) -> str:
    return f"****{cano[-4:]}-{prdt}"


def urllib_transport(method, url, headers, body):
    """Send one HTTPS request (honors HTTPS_PROXY) and decode the JSON reply."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, response_headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, response_headers, raw = exc.code, exc.headers, exc.read()
    lowered = {k.lower(): v for k, v in (response_headers or {}).items()}
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        payload = {"msg1": raw[:200].decode("utf-8", "replace")}
    return status, lowered, payload


class KisClient:
    def __init__(self, app_key, app_secret, env="real", transport=None, sleep=time.sleep):
        if env not in HOSTS:
            raise ValueError(f"KIS_ENV must be one of {sorted(HOSTS)}")
        self.app_key = app_key
        self.app_secret = app_secret
        self.env = env
        self.base_url = HOSTS[env]
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.token = None

    def scrub(self, text: str) -> str:
        for secret in (self.app_key, self.app_secret, self.token):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _post_json(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json; charset=utf-8"}
        return self.transport("POST", self.base_url + path, headers, body)

    def issue_token(self) -> None:
        status, _, payload = self._post_json(
            "/oauth2/tokenP",
            {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
        )
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if status != 200 or not token:
            code = payload.get("error_code", "") if isinstance(payload, dict) else ""
            detail = payload.get("error_description", "") if isinstance(payload, dict) else ""
            raise KisError(
                self.scrub(f"token request failed (HTTP {status}) {code} {detail}".strip())
            )
        self.token = token

    def revoke_token(self) -> None:
        """Best effort: tokens otherwise stay valid for 24 hours."""
        if not self.token:
            return
        try:
            self._post_json(
                "/oauth2/revokeP",
                {"appkey": self.app_key, "appsecret": self.app_secret, "token": self.token},
            )
        except Exception:  # noqa: BLE001 - revocation failure must not hide the result
            pass
        self.token = None

    def get(self, name, params, tr_cont=""):
        """Run one allowlisted inquiry; returns (payload, response headers)."""
        spec = READ_ONLY_TRS.get(name)
        if spec is None:
            raise KisError(f"'{name}' is not an allowed read-only inquiry")
        if not self.token:
            raise KisError("issue_token() must run first")
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": spec[self.env],
            "tr_cont": tr_cont,
            "custtype": "P",
        }
        url = f"{self.base_url}{spec['path']}?{urlencode(params)}"
        for attempt in range(2):
            status, response_headers, payload = self.transport("GET", url, headers, None)
            if payload.get("msg_cd") == RATE_LIMIT_CODE and attempt == 0:
                self.sleep(1.0)
                continue
            break
        if status != 200 or payload.get("rt_cd") != "0":
            message = f"{payload.get('msg_cd', '')} {payload.get('msg1', '')} (HTTP {status})"
            raise KisError(self.scrub(message.strip()))
        return payload, response_headers


def _num(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _holding_from_row(row: dict) -> dict:
    return {
        "symbol": (row.get("ovrs_pdno") or "").strip(),
        "name": (row.get("ovrs_item_name") or "").strip(),
        "exchange": (row.get("ovrs_excg_cd") or "").strip(),
        "qty": _num(row.get("ovrs_cblc_qty")) or 0.0,
        "avg_price": _num(row.get("pchs_avg_pric")),
        "last_price": _num(row.get("now_pric2")),
        "market_value": _num(row.get("ovrs_stck_evlu_amt")),
        "cost": _num(row.get("frcr_pchs_amt1")),
        "unrealized_pl": _num(row.get("frcr_evlu_pfls_amt")),
        "unrealized_pl_pct": _num(row.get("evlu_pfls_rt")),
    }


def fetch_holdings(client: KisClient, cano: str, prdt: str, exchanges=US_EXCHANGES) -> list[dict]:
    holdings: dict[str, dict] = {}
    for exchange in exchanges:
        fk = nk = tr_cont = ""
        for _ in range(MAX_PAGES):
            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": prdt,
                "OVRS_EXCG_CD": exchange,
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": fk,
                "CTX_AREA_NK200": nk,
            }
            payload, headers = client.get("balance", params, tr_cont=tr_cont)
            for row in payload.get("output1") or []:
                holding = _holding_from_row(row)
                if holding["symbol"] and holding["qty"] > 0:
                    holdings.setdefault(holding["symbol"], holding)
            if headers.get("tr_cont") not in ("M", "F"):
                break
            fk = payload.get("ctx_area_fk200", "")
            nk = payload.get("ctx_area_nk200", "")
            tr_cont = "N"
            client.sleep(0.1)
    return list(holdings.values())


def _field_names(output) -> list[str]:
    rows = output if isinstance(output, list) else [output] if isinstance(output, dict) else []
    return sorted({key for row in rows if isinstance(row, dict) for key in row})


def fetch_cash(client: KisClient, cano: str, prdt: str) -> dict:
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "WCRC_FRCR_DVSN_CD": "02",
        "NATN_CD": "840",
        "TR_MKET_CD": "00",
        "INQR_DVSN_CD": "00",
    }
    payload, _ = client.get("present_balance", params)
    rows = payload.get("output2") or []
    if isinstance(rows, dict):
        rows = [rows]
    by_currency = {
        row.get("crcy_cd", "?"): {
            "deposit": _num(row.get("frcr_dncl_amt_2")),
            "withdrawable": _num(row.get("frcr_drwg_psbl_amt_1")),
        }
        for row in rows
        if isinstance(row, dict)
    }
    usd = by_currency.get("USD", {})
    return {
        "usd_deposit": usd.get("deposit"),
        "usd_withdrawable": usd.get("withdrawable"),
        "by_currency": by_currency,
        # Response field names only (no values), to check the schema on first use.
        "fields_seen": {
            key: _field_names(payload.get(key)) for key in ("output1", "output2", "output3")
        },
    }


def build_snapshot(holdings, cash, cano, prdt, env, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    stock_value = sum(
        h["market_value"] if h["market_value"] is not None else h["qty"] * (h["last_price"] or 0)
        for h in holdings
    )
    cash_usd = cash.get("usd_deposit")
    total = stock_value + cash_usd if cash_usd is not None else None
    return {
        "source": "KIS Open API (read-only inquiry)",
        "env": env,
        "as_of": now.isoformat(timespec="seconds"),
        "account": mask_account(cano, prdt),
        "holdings": holdings,
        "cash": cash,
        "totals": {
            "stock_value_usd": round(stock_value, 2),
            "cash_usd": cash_usd,
            "account_total_usd": round(total, 2) if total is not None else None,
            "exposure_pct": round(stock_value / total * 100, 2) if total else None,
        },
    }


def _read_setting(environ, key):
    return next((environ[name] for name in ENV_NAMES[key] if environ.get(name)), "")


def main(argv=None, environ=None, transport=None) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Read-only KIS US stock account snapshot")
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path, or '-' for stdout (default: reports/kis_balance_<date>.json)",
    )
    parser.add_argument("--env", choices=sorted(HOSTS), default=None, help="real (default) or mock")
    args = parser.parse_args(argv)

    settings = {key: _read_setting(environ, key) for key in ENV_NAMES}
    missing = [ENV_NAMES[key][0] for key in REQUIRED_SETTINGS if not settings[key]]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1
    try:
        cano, prdt = parse_account(settings["account"], settings["product"] or "01")
        env = args.env or environ.get("KIS_ENV") or "real"
        client = KisClient(settings["app_key"], settings["app_secret"], env, transport)
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1

    try:
        client.issue_token()
        holdings = fetch_holdings(client, cano, prdt)
        cash = fetch_cash(client, cano, prdt)
    except KisError as exc:
        print(f"KIS API error: {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        reason = client.scrub(str(getattr(exc, "reason", exc)))
        print(f"Cannot reach {HOSTS[env]}: {reason}", file=sys.stderr)
        return 1
    finally:
        client.revoke_token()

    snapshot = build_snapshot(holdings, cash, cano, prdt, env)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(text)
        return 0
    path = Path(args.output or f"reports/kis_balance_{datetime.now().strftime('%Y-%m-%d')}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    totals = snapshot["totals"]
    print(
        f"Saved {path}: {len(holdings)} holdings, stock ${totals['stock_value_usd']:,.2f}, "
        f"USD cash {totals['cash_usd']}, total {totals['account_total_usd']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
