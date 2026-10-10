"""Expand selected local extras without resolving a PyPI implementation."""
import sys
from pathlib import Path
import tomllib

project = tomllib.loads(Path(sys.argv[1]).read_text())['project']
selected = ['convert-core', 'format-pdf', 'format-office', 'models-local', 'feat-ocr-rapidocr-onnx', 'cli']
requirements = set(project['dependencies'])
visited = set()


def expand(extra):
    if extra in visited:
        return
    visited.add(extra)
    for value in project['optional-dependencies'][extra]:
        if value.startswith('docling-slim['):
            for nested in value.split('[', 1)[1].split(']', 1)[0].split(','):
                expand(nested)
        else:
            requirements.add(value)


for extra in selected:
    expand(extra)
print('\n'.join(sorted(requirements)))
