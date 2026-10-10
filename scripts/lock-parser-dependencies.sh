#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-"$root/.build-parser/wheelhouse"}
mkdir -p "$out"
# uv is a build-time maintainer tool, not a query-service dependency.
# Resolve against the local implementation wheel; never install PyPI facade.
python -m build --wheel --outdir "$out" "$root/third_party/docling"
mapfile -t wheels < <(find "$out" -maxdepth 1 -name 'docling_slim-*.whl')
[ "${#wheels[@]}" = 1 ] || exit 1
mkdir -p "$root/.build-parser"
python "$root/scripts/selected_dependencies.py" "$root/third_party/docling/pyproject.toml" > "$root/.build-parser/selected-dependencies.in"
uv pip compile "$root/deployment/requirements-parser.in" \
  "$root/.build-parser/selected-dependencies.in" \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match --generate-hashes --no-annotate --no-header \
  --output-file "$root/deployment/requirements-parser.lock"
echo 'Lock generated. Build wheelhouse for CPython 3.12 Linux and verify all hashes before image build.'
