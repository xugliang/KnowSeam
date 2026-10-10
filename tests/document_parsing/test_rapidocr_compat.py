import ast
from functools import lru_cache
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


def test_new_rapidocr_public_language_api_without_removed_private_constant(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root/'third_party/docling/docling/models/stages/ocr/rapid_ocr_model.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_installed_ppocrv6_codes')
    resolver = ModuleType('rapidocr.utils.model_resolver')
    calls = []
    resolver.list_supported_langs = lambda task, version, size: calls.append((task,version,size)) or ['ch','en']
    typings = ModuleType('rapidocr.utils.typings')
    typings.TaskType = SimpleNamespace(REC='recognition')
    typings.OCRVersion = SimpleNamespace(PPOCRV6='v6')
    typings.ModelType = SimpleNamespace(SMALL='small')
    monkeypatch.setitem(sys.modules, resolver.__name__, resolver)
    monkeypatch.setitem(sys.modules, typings.__name__, typings)
    namespace = {'lru_cache':lru_cache, 'safe_version':lambda _: '3.10.0'}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<vendored-function>', 'exec'), namespace)
    assert namespace['_installed_ppocrv6_codes']() == frozenset({'ch','en'})
    assert calls == [('recognition','v6','small')]
