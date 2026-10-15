#!/usr/bin/env python3
"""Install the KIS Trading MCP for Claude Desktop in read-only form (Windows / macOS).

Follows the "stdio 로컬 연동" route of KIS's guide (MCP/MCP AI 도구 연결 방법.md)
with three changes: the server comes from a reviewed, pinned commit; every
order, amend/cancel and reservation API is removed from its configs, so the
server rejects them; and the keys are typed into this terminal with hidden
input instead of being pasted anywhere else.

Run it with uv, which also supplies Python:
    uv run kis_setup.py            # install
    uv run kis_setup.py --check    # diagnose without changing anything (no keys shown)
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

KIS_COMMIT = "2077773a69a428daf63e148182511f068cc45de3"  # reviewed 2026-10-15
ARCHIVE_URL = f"https://codeload.github.com/koreainvestment/open-trading-api/zip/{KIS_COMMIT}"
SERVER_SUBDIR = PurePosixPath("MCP", "Kis Trading MCP")
KIS_REST = "https://openapi.koreainvestment.com:9443"
SERVER_NAME = "kis-trading"
MARKER = ".kis_readonly_install"

# Same rule as strip_order_apis.py (a test keeps the two identical).
WRITE_API_PATTERN = re.compile(r"(^|_)(order|buy|sell)(_|$)|rvsecncl")


def is_write_api(api_type: str) -> bool:
    return bool(WRITE_API_PATTERN.search(api_type))


def strip_configs(config_dir: Path) -> list[str]:
    removed = []
    for path in sorted(Path(config_dir).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        apis = data.get("apis", {})
        write_apis = [api for api in apis if is_write_api(api)]
        for api in write_apis:
            del apis[api]
            removed.append(f"{path.name}:{api}")
        if write_apis:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return removed


def write_apis_left(config_dir: Path) -> list[str]:
    left = []
    for path in sorted(Path(config_dir).glob("*.json")):
        apis = json.loads(path.read_text(encoding="utf-8")).get("apis", {})
        left.extend(f"{path.name}:{api}" for api in apis if is_write_api(api))
    return left


def install_server(archive: bytes, dest: Path) -> list[str]:
    """Extract only the Trading MCP folder into dest, then strip its order APIs."""
    dest = Path(dest)
    if dest.exists():
        if not (dest / MARKER).exists():
            raise RuntimeError(f"{dest} already exists and was not created by this installer")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for info in bundle.infolist():
            parts = PurePosixPath(info.filename).parts
            if len(parts) < 2 or info.is_dir():
                continue
            relative = PurePosixPath(*parts[1:])  # drop "open-trading-api-<sha>/"
            try:
                inner = relative.relative_to(SERVER_SUBDIR)
            except ValueError:
                continue
            target = (dest / Path(*inner.parts)).resolve()
            if root not in target.parents:
                raise RuntimeError(f"unsafe path in archive: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(info))
    (dest / MARKER).write_text(f"KIS Trading MCP {KIS_COMMIT}, order APIs removed\n")
    return strip_configs(dest / "configs")


def parse_account(raw: str, product: str = "01") -> tuple[str, str]:
    match = re.fullmatch(r"(\d{8})-?(\d{2})?", (raw or "").strip())
    if not match:
        raise ValueError("계좌번호는 12345678-01 형식으로 입력해 주십시오")
    return match.group(1), match.group(2) or product


def desktop_config_paths(platform: str, environ, home: Path) -> list[Path]:
    name = "claude_desktop_config.json"
    if platform == "darwin":
        return [home / "Library" / "Application Support" / "Claude" / name]
    if platform.startswith("win"):
        appdata = Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
        paths = [appdata / "Claude" / name]
        local = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        # The Microsoft Store build keeps its settings in a virtualized folder.
        for package in sorted((local / "Packages").glob("Claude_*")):
            store = package / "LocalCache" / "Roaming" / "Claude"
            if store.is_dir():
                paths.append(store / name)
        return paths
    return [home / ".config" / "Claude" / name]


def merge_desktop_config(path: Path, name: str, entry: dict) -> Path | None:
    """Add or replace one MCP server entry; returns the backup path, if any."""
    backup = None
    data = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if text.strip() else {}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"claude_desktop_config.backup-{stamp}.json")
        backup.write_text(text, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    data.setdefault("mcpServers", {})[name] = entry
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return backup


def _post_json(url: str, payload: dict):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def check_token(app_key: str, app_secret: str, post=_post_json) -> tuple[bool, str]:
    """Issue and revoke one access token to prove the keys and network work."""
    try:
        status, body = post(
            f"{KIS_REST}/oauth2/tokenP",
            {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        )
    except (urllib.error.URLError, OSError) as exc:
        return False, f"KIS 서버에 연결하지 못했습니다: {getattr(exc, 'reason', exc)}"
    token = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not token:
        detail = f"{body.get('error_code', '')} {body.get('error_description', '')}".strip()
        message = f"토큰 발급 실패 (HTTP {status}) {detail}".strip()
        for secret in (app_key, app_secret):
            message = message.replace(secret, "***")
        return False, message
    try:
        post(
            f"{KIS_REST}/oauth2/revokeP",
            {"appkey": app_key, "appsecret": app_secret, "token": token},
        )
    except Exception:  # noqa: BLE001 - the token expires in 24 hours anyway
        pass
    return True, "토큰 발급 성공: 키와 KIS 서버 연결이 정상입니다"


def find_uv(environ) -> str | None:
    candidates = [environ.get("UV"), shutil.which("uv")]
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        candidates += [str(folder / "uv"), str(folder / "uv.exe")]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def _ask(prompt: str, secret: bool = False) -> str:
    value = getpass.getpass(prompt) if secret else input(prompt)
    return value.strip()


def claude_log_dirs(platform: str, environ, home: Path) -> list[Path]:
    if platform == "darwin":
        return [home / "Library" / "Logs" / "Claude"]
    return [path.parent / "logs" for path in desktop_config_paths(platform, environ, home)]


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def diagnose(platform: str, environ, home: Path, dest: Path) -> list[str]:
    """Describe the install and Claude Desktop config; never shows key values."""
    lines = [f"운영체제: {platform}", f"uv: {find_uv(environ) or '찾지 못함'}"]
    dest = Path(dest)
    if not dest.exists():
        lines.append(f"설치 폴더 없음: {dest} (설치를 다시 실행해 주십시오)")
    else:
        venv = "있음" if (dest / ".venv").is_dir() else "없음 (uv sync가 끝나지 않았습니다)"
        server = "있음" if (dest / "server.py").is_file() else "없음"
        lines.append(f"설치 폴더: {dest} (server.py {server}, .venv {venv})")
        if (dest / "configs").is_dir():
            left = write_apis_left(dest / "configs")
            lines.append("주문 API 없음 (조회 전용)" if not left else f"주문 API 남아 있음: {left}")

    secrets: list[str] = []
    for path in desktop_config_paths(platform, environ, home):
        if not path.exists():
            lines.append(f"설정 파일 없음: {path}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            lines.append(f"JSON 형식 오류: {path} ({exc})")
            continue
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        entry = servers.get(SERVER_NAME)
        if not entry:
            lines.append(f"설정 파일에 kis-trading 없음: {path} (등록된 서버: {sorted(servers)})")
            continue
        env = entry.get("env", {})
        secrets += [value for key, value in env.items() if key.startswith("KIS_") and value]
        command = entry.get("command", "")
        lines.append(f"kis-trading 등록됨: {path}")
        lines.append(
            f"    실행 파일 {'있음' if Path(command).is_file() else '없음: ' + command}, "
            f"App Key {'입력됨' if env.get('KIS_APP_KEY') else '비어 있음'}"
        )

    for log_dir in claude_log_dirs(platform, environ, home):
        found = False
        for name in ("mcp-server-kis-trading.log", "mcp.log"):
            log = log_dir / name
            if not log.is_file():
                continue
            found = True
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()
            if name == "mcp.log":
                tail = [line for line in tail if "kis" in line.lower()]
            lines.append(f"로그 {log} (마지막 {min(len(tail), 15)}줄):")
            lines += ["    " + _scrub(line, secrets)[:300] for line in tail[-15:]]
        if not found:
            lines.append(f"kis-trading 로그 없음: {log_dir}")
    return [_scrub(line, secrets) for line in lines]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only KIS Trading MCP for Claude Desktop")
    parser.add_argument("--check", action="store_true", help="diagnose only, change nothing")
    args = parser.parse_args(argv)
    dest = Path.home() / "kis-trading-mcp"
    if args.check:
        print("\n".join(diagnose(sys.platform, os.environ, Path.home(), dest)))
        print(
            "\n설정을 바꾼 뒤에는 Claude 데스크톱을 트레이/메뉴 막대에서 '종료'로 완전히 끄고 다시 실행해야 합니다."
        )
        return 0

    print("한국투자증권 Trading MCP (조회 전용) 설치를 시작합니다.\n")
    uv = find_uv(os.environ)
    if not uv:
        print("uv가 없습니다. 먼저 설치한 뒤 새 터미널에서 다시 실행해 주십시오.")
        print("  Mac:     curl -LsSf https://astral.sh/uv/install.sh | sh")
        print('  Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"')
        return 2

    print(f"1/4 서버 내려받는 중 (커밋 {KIS_COMMIT[:7]}) ...")
    local_archive = os.environ.get("KIS_MCP_ARCHIVE")  # optional pre-downloaded zip
    if local_archive:
        archive = Path(local_archive).read_bytes()
    else:
        with urllib.request.urlopen(ARCHIVE_URL, timeout=120) as response:
            archive = response.read()
    removed = install_server(archive, dest)
    left = write_apis_left(dest / "configs")
    if left:
        print("주문 API 제거에 실패했습니다: " + ", ".join(left))
        return 1
    print(f"    주문 관련 API {len(removed)}개를 제거했습니다. 조회 API만 남았습니다.")

    print("2/4 파이썬 패키지 설치 중 (처음에는 몇 분 걸릴 수 있습니다) ...")
    result = subprocess.run([uv, "sync", "--quiet"], cwd=dest)
    if result.returncode != 0:
        print("uv sync에 실패했습니다. 위 메시지를 확인해 주십시오.")
        return 1

    print("3/4 키 입력 (입력하는 글자는 화면에 보이지 않습니다)")
    app_key = os.environ.get("KIS_APP_KEY") or _ask("    App Key: ", secret=True)
    app_secret = os.environ.get("KIS_APP_SECRET") or _ask("    App Secret: ", secret=True)
    while True:
        try:
            cano, prdt = parse_account(_ask("    계좌번호 (예: 12345678-01): "))
            break
        except ValueError as exc:
            print(f"    {exc}")
    ok, message = check_token(app_key, app_secret)
    print(f"    {message}")

    print("4/4 Claude 데스크톱 설정에 등록 중 ...")
    entry = {
        "command": uv,
        "args": ["--directory", str(dest), "run", "python", "server.py"],
        "env": {
            "ENV": "live",
            "MCP_TYPE": "stdio",
            "KIS_APP_KEY": app_key,
            "KIS_APP_SECRET": app_secret,
            "KIS_ACCT_STOCK": cano,
            "KIS_PROD_TYPE": prdt,
        },
    }
    for path in desktop_config_paths(sys.platform, os.environ, Path.home()):
        backup = merge_desktop_config(path, SERVER_NAME, entry)
        print(f"    {path}" + (f" (기존 설정 백업: {backup.name})" if backup else ""))

    print("\n완료했습니다.")
    print("- Claude 데스크톱을 완전히 종료(트레이/메뉴 막대에서 종료)한 뒤 다시 실행해 주십시오.")
    print("- 설정 → 개발자 또는 대화창의 도구 버튼에서 kis-trading이 보이면 연결된 것입니다.")
    print('- 예: "kis-trading으로 해외주식 잔고를 실전(env_dv real)으로 조회해줘"')
    print("- 설정 파일에 키가 저장되므로 그 파일은 다른 사람과 공유하지 마십시오.")
    if not ok:
        reason = (
            "인터넷 연결이나 백신·방화벽 설정을"
            if message.startswith("KIS 서버에 연결하지")
            else "App Key와 App Secret을"
        )
        print(f"\n주의: 토큰 발급이 실패했습니다. {reason} 확인한 뒤 다시 실행해 주십시오.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
