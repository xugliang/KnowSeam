#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
# Dedicated disposable builder, never the runtime parser. No host Docker socket
# or business configuration is mounted inside the container.
docker run --rm --name knowseam-parser-wheel-builder \
  --label knowseam.component=parser-build --cpus=2 --memory=4g \
  --mount "type=bind,src=$root/deployment/requirements-parser.lock,dst=/requirements-parser.lock,readonly" \
  --mount "type=bind,src=$root/.build-parser/wheelhouse,dst=/wheelhouse" \
  python:3.12-slim python -m pip wheel --no-cache-dir --require-hashes \
    --find-links /wheelhouse \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --wheel-dir /wheelhouse -r /requirements-parser.lock
python "$root/scripts/record_wheel_hashes.py" "$root/deployment/requirements-parser.lock" "$root/.build-parser/wheelhouse"
