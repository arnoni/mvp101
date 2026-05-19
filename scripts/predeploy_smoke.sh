#!/usr/bin/env bash
set -euo pipefail

python -m compileall app
python -c "from app.main import app; print('FastAPI import ok')"
