"""Execute imports from the source files actually copied into the parser image."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def test_parser_image_source_closure_supports_structured_tables(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for line in (root/'deployment/Dockerfile.parser').read_text().splitlines():
        if not line.startswith('COPY --chown=10001:10001 src/knowseam/'):
            continue
        _,_,source,target = shlex.split(line)
        destination = tmp_path/target.removeprefix('/app/')
        destination.parent.mkdir(parents=True,exist_ok=True)
        if (root/source).is_dir():
            shutil.copytree(root/source,destination,ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(root/source,destination)
    script = """from types import SimpleNamespace as N, ModuleType
import sys
from pathlib import Path
package = ModuleType('knowseam')
package.__path__ = [str(Path.cwd()/'src/knowseam')]
sys.modules['knowseam'] = package
from knowseam.document_parsing.extensions.tables import structured_table
from knowseam.document_parsing import normalizer
assert normalizer.NORMALIZER_VERSION
item=N(data=N(num_rows=0,num_cols=0,table_cells=[]))
assert structured_table(item,{},'table1').row_count==0
"""
    result = subprocess.run([sys.executable,'-c',script],cwd=tmp_path,
        env={**os.environ,'PYTHONPATH':str(tmp_path/'src')},capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
