#!/usr/bin/env bash
set -euo pipefail
[ $# = 1 ] || { echo 'usage: prepare-parser-models.sh NEW_MODEL_DIRECTORY' >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
python "$root/scripts/prepare_parser_models.py" "$1"
