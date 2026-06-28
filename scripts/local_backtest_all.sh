#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

for horizon in 20 60 120 240 480; do
  .venv/bin/python scripts/run_prediction_backtest.py --config configs/poc.yaml --horizon "$horizon"
done

.venv/bin/python scripts/run_model_gatekeeper.py --config configs/poc.yaml
.venv/bin/python scripts/build_dashboard_snapshot.py --config configs/poc.yaml --horizon 3M

echo "Backtest, gatekeeper, and dashboard snapshot completed."
