#!/usr/bin/env python3
"""Make the official KIS Trading MCP read-only by removing its order APIs.

The KIS Trading MCP exposes one tool per market (overseas_stock, domestic_stock,
...) and picks the API with an ``api_type`` argument, so tool-level permission
rules cannot separate orders from inquiries. The server rejects any api_type
missing from its ``configs/*.json``; deleting the order entries there makes
order, amend/cancel and reservation calls impossible.

Usage:
    python3 strip_order_apis.py "<repo>/MCP/Kis Trading MCP/configs"          # strip
    python3 strip_order_apis.py "<repo>/MCP/Kis Trading MCP/configs" --check  # verify
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# order*, *_order, daytime_order*, buy, sell, and amend/cancel (정정취소) APIs.
# A few inquiry APIs share these words (e.g. inquire_psbl_order); dropping them
# too is the safe side.
WRITE_API_PATTERN = re.compile(r"(^|_)(order|buy|sell)(_|$)|rvsecncl")


def is_write_api(api_type: str) -> bool:
    return bool(WRITE_API_PATTERN.search(api_type))


def strip_configs(config_dir, check_only: bool = False) -> list[str]:
    """Remove write APIs from every config; returns "file:api_type" entries found."""
    found = []
    for path in sorted(Path(config_dir).glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        apis = data.get("apis", {})
        write_apis = [api for api in apis if is_write_api(api)]
        found.extend(f"{path.name}:{api}" for api in write_apis)
        if write_apis and not check_only:
            for api in write_apis:
                del apis[api]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Remove order APIs from KIS Trading MCP configs")
    parser.add_argument("config_dir", help='Path to "MCP/Kis Trading MCP/configs"')
    parser.add_argument("--check", action="store_true", help="Only verify; exit 1 if any remain")
    args = parser.parse_args(argv)

    if not any(Path(args.config_dir).glob("*.json")):
        print(f"No config files in {args.config_dir}", file=sys.stderr)
        return 1
    found = strip_configs(args.config_dir, check_only=args.check)
    if args.check:
        if found:
            print("Order APIs still present: " + ", ".join(found))
            return 1
        print("Read-only check passed: no order APIs in the configs")
        return 0
    print(f"Removed {len(found)} order APIs" + (": " + ", ".join(found) if found else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
