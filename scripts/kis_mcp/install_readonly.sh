#!/usr/bin/env bash
# Install the official KIS Trading MCP without its order APIs and register it
# with Claude Code as "kis-trading" (user scope). Safe to re-run; meant for the
# cloud environment's setup script.
#
#   bash ~/claude-trading-skills/scripts/kis_mcp/install_readonly.sh
#
# KIS_MCP_DIR overrides the checkout location; KIS_MCP_SKIP_REGISTER=1 skips
# the "claude mcp add" step.
set -euo pipefail

KIS_REPO=https://github.com/koreainvestment/open-trading-api
KIS_COMMIT=2077773a69a428daf63e148182511f068cc45de3 # reviewed 2026-10-15
DEST="${KIS_MCP_DIR:-$HOME/kis-open-trading-api}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$DEST/MCP/Kis Trading MCP"

if [ ! -d "$DEST/.git" ]; then
  git init -q "$DEST"
  git -C "$DEST" remote add origin "$KIS_REPO"
fi
if [ "$(git -C "$DEST" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$KIS_COMMIT" ]; then
  git -C "$DEST" fetch -q --depth 1 origin "$KIS_COMMIT"
  git -C "$DEST" checkout -q --force FETCH_HEAD
fi

python3 "$HERE/strip_order_apis.py" "$SERVER_DIR/configs"
python3 "$HERE/strip_order_apis.py" "$SERVER_DIR/configs" --check

(cd "$SERVER_DIR" && uv sync --quiet)

if [ "${KIS_MCP_SKIP_REGISTER:-0}" != "1" ] && ! claude mcp get kis-trading >/dev/null 2>&1; then
  claude mcp add --scope user -e KIS_MCP_DIR="$DEST" kis-trading -- "$HERE/run_readonly.sh"
fi
echo "kis-trading MCP ready (read-only): $SERVER_DIR"
