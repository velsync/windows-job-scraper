#!/usr/bin/env bash
# Recreate the development environment from the committed lock files.
# Usage: scripts/bootstrap_dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install --upgrade -q pip
.venv/bin/pip install -q -r requirements/production.lock.txt -r requirements/dev.lock.txt
.venv/bin/pip install -q -e .
.venv/bin/python -m pip check
echo "Dev environment ready: .venv"
