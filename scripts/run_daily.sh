#!/usr/bin/env bash
# Daily run for cron/systemd. Logs to data/run.log; the report is rewritten at reports/index.html.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
{
  echo "=== $(date -Is) ==="
  .venv/bin/python -m raiseradar run
} >> data/run.log 2>&1
