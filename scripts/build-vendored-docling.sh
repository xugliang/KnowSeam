#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-"$root/.build-parser/wheelhouse"}
mkdir -p "$out"
python "$root/scripts/vendor_identity.py" "$root/third_party/docling" > /dev/null
python -m build --no-isolation --wheel --outdir "$out" "$root/third_party/docling"
mapfile -t wheels < <(find "$out" -maxdepth 1 -name 'docling_slim-*.whl')
[ "${#wheels[@]}" = 1 ] || { echo 'expected exactly one local implementation wheel' >&2; exit 1; }
python "$root/scripts/vendor_identity.py" "$root/third_party/docling" --wheel "${wheels[0]}" \
  --metadata "$root/third_party/docling.upstream.json" --lock "$root/deployment/requirements-parser.lock" > "$out/parser-build.json"
