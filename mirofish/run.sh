#!/usr/bin/env bash
# Launch the MiroFish A2 service on loopback, with keys loaded from arbiter/.env.
# Usage:  bash mirofish/run.sh            # real Claude judge (needs ANTHROPIC_API_KEY)
#         MIROFISH_FAKE_LLM=1 bash mirofish/run.sh   # canned judge, no API cost
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Load shared keys (ANTHROPIC_API_KEY, ALPACA_*, EDGAR_USER_AGENT) from arbiter/.env.
if [[ -f arbiter/.env ]]; then
  set -a; source arbiter/.env; set +a
fi

export MIROFISH_HOST="${MIROFISH_HOST:-127.0.0.1}"
export MIROFISH_PORT="${MIROFISH_PORT:-8900}"

exec arbiter/.venv/bin/python -m mirofish
