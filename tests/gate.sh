#!/bin/bash
# The pre-commit gate: state + templates + liveness + fast smoke.
# Full smoke (real model): run tests/smoke.py without SMOKE_FAST.
set -e
cd "$(dirname "$0")/.."
PY=.venv/bin/python
set -o pipefail
$PY tests/test_state.py
$PY tests/test_liveness.py
$PY tests/test_templates.py
$PY tests/test_first_contact.py
SMOKE_FAST=1 $PY tests/smoke.py
echo
echo "GATE: ALL GREEN"
