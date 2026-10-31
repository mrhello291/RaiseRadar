#!/usr/bin/env bash
# Jobs-only refresh for cron (no LLM, no news requests): re-fetch jobs of tracked companies, rebuild the report.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
{
  echo "=== $(date -Is) jobs refresh ==="
  .venv/bin/python -m raiseradar jobs
  .venv/bin/python -m raiseradar report
} >> data/run.log 2>&1
