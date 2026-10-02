#!/bin/sh
# One-shot acceptance run: unit tests, then HTTP smoke against the app service.
# Exit code is the acceptance result (0 = pass).
set -eu
cd "$(dirname "$0")"

echo "== unit tests =="
python3 -m unittest tests.test_walrec -v

echo "== HTTP smoke against ${APP_URL:-http://app:8080} =="
python3 tests/smoke.py

echo "VERIFY OK"
