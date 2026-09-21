"""Lexical tests: real Jieba cases skip explicitly if its dependency is absent."""

import pytest
from types import SimpleNamespace

from knowseam import tokenization
from knowseam.tokenization import JiebaTokenizer, TokenizationError


def test_missing_jieba_is_not_silently_replaced(monkeypatch):
    def missing(name):
        raise ImportError("contract missing jieba")

    monkeypatch.setattr(tokenization.importlib, "import_module", missing)
    with pytest.raises(TokenizationError, match="requires jieba"):
        JiebaTokenizer().fts_text("公司知识库")


def test_input_bounds_and_bad_dictionary(tmp_path):
    with pytest.raises(TokenizationError):
        JiebaTokenizer(user_dictionary=tmp_path / "missing.txt")
    with pytest.raises(TokenizationError):
        JiebaTokenizer(terms="not-a-list")
    with pytest.raises(TokenizationError):
        JiebaTokenizer(terms=[""])
    tokenizer = JiebaTokenizer(max_chars=2)
    with pytest.raises(TokenizationError, match="character limit"):
        tokenizer.tokenize("三四五")
    with pytest.raises(TokenizationError):
        tokenizer.tokenize(None)


def test_version_normalizes_terms_and_ignores_order():
    first = JiebaTokenizer(terms=["ＫＩＮＯ", "  示例知识中台  ", "Alpha", "alpha"])
    second = JiebaTokenizer(terms=["示例知识中台", "alpha", "kino"])
    assert first.version == second.version
    assert first.terms == second.terms
    assert JiebaTokenizer(terms=["新增公司词汇"]).version != first.version


def test_default_vocabulary_is_product_independent():
    assert tokenization.DEFAULT_TERMS == ()
    assert JiebaTokenizer().terms == ()
    assert JiebaTokenizer(terms=["示例产品专名"]).version != JiebaTokenizer().version


def test_version_changes_with_dictionary_bytes_not_path(tmp_path, monkeypatch):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    content = "银河采购机器人 10000000 nz\n"
    first.write_text(content, encoding="utf-8")
    second.write_text(content, encoding="utf-8")
    old_version = JiebaTokenizer(first).version
    assert old_version == JiebaTokenizer(second).version
    first.write_text(content.replace("10000000", "20000000"), encoding="utf-8")
    assert old_version != JiebaTokenizer(first).version
    monkeypatch.setattr(tokenization, "TOKENIZER_ALGORITHM_VERSION", "contract-next-algorithm")
    assert old_version != JiebaTokenizer(second).version


def test_dictionary_snapshot_matches_version_at_lazy_initialization(tmp_path, monkeypatch):
    """Contract double checks lifecycle only, not real Chinese segmentation quality."""
    loaded = []

    class ContractEngine:
        def initialize(self):
            pass

        def load_userdict(self, stream):
            loaded.append(stream.read())

        def add_word(self, term, **kwargs):
            pass

        def cut_for_search(self, text, **kwargs):
            return [text]

    monkeypatch.setattr(tokenization.importlib, "import_module",
                        lambda name: SimpleNamespace(Tokenizer=ContractEngine))
    path = tmp_path / "dictionary.txt"
    original = "银河采购机器人 10000000 nz\n"
    changed = "新公司术语 10000000 nz\n"
    path.write_bytes(original.encode("utf-8"))
    tokenizer = JiebaTokenizer(path)
    version = tokenizer.version
    path.write_bytes(changed.encode("utf-8"))
    newer = JiebaTokenizer(path)
    assert newer.version != version
    tokenizer.tokenize("任意输入")
    newer.tokenize("任意输入")
    assert loaded == [original, changed]
    assert tokenizer.version == version


def test_dictionary_snapshot_rejects_bad_encoding_and_bounds(tmp_path, monkeypatch):
    path = tmp_path / "dictionary.txt"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(TokenizationError, match="UTF-8"):
        JiebaTokenizer(path)
    monkeypatch.setattr(tokenization, "MAX_DICTIONARY_BYTES", 8)
    path.write_bytes(b"x" * 9)
    with pytest.raises(TokenizationError, match="size limit"):
        JiebaTokenizer(path)


def test_real_jieba_preserves_company_terms_and_whole_identifiers():
    pytest.importorskip("jieba", reason="Jieba runtime not installed; not a fake tokenizer success")
    tokenizer = JiebaTokenizer(terms=["示例知识中台", "示例产品"])
    tokens = tokenizer.tokenize("示例知识中台：示例产品支持K1-fast，考核TEST-00000001，版本v1.2.3")
    for term in ["示例知识中台", "示例产品", "k1-fast", "test-00000001", "v1.2.3"]:
        assert term in tokens
    assert "zkid" + "test-00000001".encode().hex() in tokens
    assert "test-00000001" in tokenizer.fts_text("ＴＥＳＴ－０００００００１")
    assert tokenizer.tokenize("示例产品") == tokenizer.tokenize("示例产品")


def test_real_jieba_user_dictionary(tmp_path):
    pytest.importorskip("jieba", reason="Jieba runtime not installed")
    dictionary = tmp_path / "contract-userdict.txt"
    dictionary.write_text("银河采购机器人 10000000 nz\n", encoding="utf-8")
    assert "银河采购机器人" in JiebaTokenizer(dictionary).tokenize("请查询银河采购机器人的功能")


def test_real_jieba_token_count_limit_and_empty():
    pytest.importorskip("jieba", reason="Jieba runtime not installed")
    assert JiebaTokenizer().fts_text("  ") == ""
    with pytest.raises(TokenizationError, match="token limit"):
        JiebaTokenizer(max_tokens=1).tokenize("公司 产品 用户")
