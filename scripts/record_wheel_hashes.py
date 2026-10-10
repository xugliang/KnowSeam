"""Mechanical lock enrichment for locally built pure-Python dependency wheels."""
import hashlib
from pathlib import Path
import re
import sys

lock, wheelhouse = Path(sys.argv[1]), Path(sys.argv[2])
text = lock.read_text()
for wheel in sorted(wheelhouse.glob('*.whl')):
    name, version = wheel.name.split('-')[:2]
    if name == 'docling_slim':
        continue
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    pattern = re.compile(r'(?m)^' + re.escape(name.replace('_', '-')) + r'==' + re.escape(version) + r'(?:[^\n]*\n(?:[ \t]+[^\n]*\n)*)')
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError(f'wheel not bound to exactly one locked package: {name}')
    entry = matches[0].group()
    if digest not in entry:
        updated = entry.rstrip() + ' \\\n    --hash=sha256:' + digest + '\n'
        text = text[:matches[0].start()] + updated + text[matches[0].end():]
lock.write_text(text)
