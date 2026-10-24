#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m smartmoney doctor
python3 -m unittest discover -s tests -v
python3 -m smartmoney status
python3 -m smartmoney analyze --market KR --symbol 005930 --no-save
printf '\nOffline harness completed. No remote APIs contacted.\n'
