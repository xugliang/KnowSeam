#!/usr/bin/env bash
set -euo pipefail
[ $# = 1 ] || { echo 'usage: parser-smoke.sh SOCKET' >&2; exit 2; }
python - "$1" <<'PY'
from pathlib import Path
import sys
from knowseam.document_parsing.client import ParserClient
client = ParserClient(Path(sys.argv[1]))
try:
    print(client.status().model_dump_json())
finally:
    client.close()
PY
