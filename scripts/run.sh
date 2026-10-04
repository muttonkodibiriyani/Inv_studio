#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${INV_STUDIO_PORT:-8765}" --no-access-log
