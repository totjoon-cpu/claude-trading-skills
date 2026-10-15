#!/usr/bin/env bash
# Start the read-only KIS Trading MCP over stdio. Refuses to start while any
# order API is present in its configs. Stdout is the MCP channel, so all of
# this script's own output goes to stderr.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="${KIS_MCP_DIR:-$HOME/kis-open-trading-api}/MCP/Kis Trading MCP"

python3 "$HERE/strip_order_apis.py" "$SERVER_DIR/configs" --check >&2

# Accept the variable names used by scripts/kis_balance.py as well.
export KIS_APP_KEY="${KIS_APP_KEY:-${KIS_API_KEY:-}}"
export KIS_APP_SECRET="${KIS_APP_SECRET:-${KIS_API_SECRET:-}}"
if [ -z "${KIS_ACCT_STOCK:-}" ] && [ -n "${KIS_ACCOUNT_NO:-}" ]; then
  digits="${KIS_ACCOUNT_NO//[^0-9]/}"
  export KIS_ACCT_STOCK="${digits:0:8}"
  if [ "${#digits}" -ge 10 ] && [ -z "${KIS_PROD_TYPE:-}" ]; then
    export KIS_PROD_TYPE="${digits:8:2}"
  fi
fi

export ENV=live MCP_TYPE=stdio
cd "$SERVER_DIR"
exec uv run --quiet python server.py
