import hashlib
from pathlib import Path
import subprocess
import sys


def test_local_built_dependency_hash_is_added_without_losing_next_entry(tmp_path):
    root = Path(__file__).resolve().parents[2]
    lock = tmp_path/'requirements.lock'
    lock.write_text('pylatexenc==2.10 \\\n    --hash=sha256:'+'a'*64+'\nother==1.0 \\\n    --hash=sha256:'+'b'*64+'\n')
    wheels = tmp_path/'wheelhouse'
    wheels.mkdir()
    content = b'synthetic built wheel bytes'
    (wheels/'pylatexenc-2.10-py3-none-any.whl').write_bytes(content)
    subprocess.run([sys.executable, str(root/'scripts/record_wheel_hashes.py'), str(lock), str(wheels)], check=True)
    output = lock.read_text()
    assert '--hash=sha256:'+hashlib.sha256(content).hexdigest() in output
    assert '\nother==1.0' in output
    first, second = output.split('other==1.0')
    assert 'b'*64 not in first and 'a'*64 not in second
    subprocess.run([sys.executable, str(root/'scripts/record_wheel_hashes.py'), str(lock), str(wheels)], check=True)
    assert lock.read_text() == output
