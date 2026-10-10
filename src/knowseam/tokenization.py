"""Deterministic Chinese lexical analysis for document and query PG FTS inputs."""

from __future__ import annotations

import importlib
import hashlib
import io
import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Iterable

from .domain import KnowledgeError

DEFAULT_TERMS: tuple[str, ...] = ()
TOKENIZER_ALGORITHM_VERSION = "jieba-search-company-v2"
MAX_DICTIONARY_BYTES = 10 * 1024 * 1024
# Keep product models, version strings and assessment/document IDs intact.
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*(?![A-Za-z0-9_])")
HAS_WORD = re.compile(r"[\w]", re.UNICODE)


class TokenizationError(KnowledgeError):
    pass


class JiebaTokenizer:
    """Own a Jieba instance; never change a process-global dictionary.

    Both indexing and querying must call the same configuration. Missing Jieba
    is a deployment error, not a reason to silently switch to character splitting.
    """

    def __init__(
        self, user_dictionary: str | Path | None = None, terms: Iterable[str] = (),
        *, glossary: Iterable[tuple[str, Iterable[str]]] = (),
        max_chars: int = 1_000_000, max_tokens: int = 200_000,
    ) -> None:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 2_000_000:
            raise TokenizationError("max_chars must be between 1 and 2000000")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 500_000:
            raise TokenizationError("max_tokens must be between 1 and 500000")
        if isinstance(terms, str):
            raise TokenizationError("terms must be a collection of words, not one string")
        if isinstance(glossary, (str, bytes)):
            raise TokenizationError("glossary must be a collection")
        alias_targets: dict[str, str] = {}
        glossary_rows: list[tuple[str, tuple[str, ...]]] = []
        for canonical, aliases in glossary:
            if not isinstance(canonical, str) or isinstance(aliases, (str, bytes)):
                raise TokenizationError("invalid enterprise terminology")
            canonical_name = unicodedata.normalize("NFKC", canonical).strip().lower()
            alias_names = tuple(unicodedata.normalize("NFKC", alias).strip().lower()
                                for alias in aliases)
            if (not canonical_name or len(canonical_name) > 256
                    or any(not alias or len(alias) > 256 for alias in alias_names)):
                raise TokenizationError("invalid enterprise terminology")
            for name in (canonical_name, *alias_names):
                if name in alias_targets and alias_targets[name] != canonical_name:
                    raise TokenizationError("ambiguous enterprise terminology")
                alias_targets[name] = canonical_name
            glossary_rows.append((canonical_name, alias_names))
        self._alias_targets = alias_targets
        self._glossary = tuple(sorted(glossary_rows))
        configured_terms = tuple(DEFAULT_TERMS) + tuple(terms) + tuple(alias_targets)
        if any(not isinstance(term, str) or not term.strip() or len(term) > 256 for term in configured_terms):
            raise TokenizationError("dictionary terms must be nonempty strings of at most 256 characters")
        if len(configured_terms) > 100_000:
            raise TokenizationError("too many dictionary terms")
        self._terms = tuple(sorted({unicodedata.normalize("NFKC", term).strip().lower()
                                    for term in configured_terms}))
        self.user_dictionary = Path(user_dictionary).resolve() if user_dictionary else None
        self._dictionary_text: str | None = None
        dictionary_hash = None
        if self.user_dictionary is not None:
            try:
                if not self.user_dictionary.is_file():
                    raise OSError("not a regular file")
                # Bound the actual read, not only stat(): the file may change
                # between construction and lazy Jieba initialization.
                with self.user_dictionary.open("rb") as stream:
                    dictionary_bytes = stream.read(MAX_DICTIONARY_BYTES + 1)
                if len(dictionary_bytes) > MAX_DICTIONARY_BYTES:
                    raise TokenizationError("user dictionary exceeds the 10 MiB size limit")
                self._dictionary_text = dictionary_bytes.decode("utf-8-sig")
            except (OSError, UnicodeError):
                raise TokenizationError("user dictionary must be a readable UTF-8 file of at most 10 MiB") from None
            dictionary_hash = hashlib.sha256(dictionary_bytes).hexdigest()
        fingerprint_data = {"algorithm": TOKENIZER_ALGORITHM_VERSION, "terms": self._terms,
                            "dictionary_sha256": dictionary_hash}
        if self._glossary:
            fingerprint_data["glossary"] = self._glossary
        fingerprint = json.dumps(fingerprint_data,
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._version = TOKENIZER_ALGORITHM_VERSION + ":" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        self.max_chars = max_chars
        self.max_tokens = max_tokens
        self._engine = None
        self._lock = threading.Lock()

    @property
    def terms(self) -> tuple[str, ...]:
        return self._terms

    @property
    def version(self) -> str:
        """Stable indexing fingerprint; file edits require a new instance."""
        return self._version

    def _initialize(self) -> None:
        if self._engine is not None:
            return
        try:
            jieba = importlib.import_module("jieba")
        except ImportError:
            raise TokenizationError("Chinese lexical retrieval requires jieba; install project dependencies") from None
        try:
            engine = jieba.Tokenizer()
            engine.initialize()
            if self._dictionary_text is not None:
                # Load precisely the bytes fingerprinted at construction,
                # even if the original file is replaced before the first query.
                engine.load_userdict(io.StringIO(self._dictionary_text))
            for term in self.terms:
                # Lowercase inputs and dictionary consistently for Latin product names.
                engine.add_word(term, freq=10_000_000)
        except Exception:
            raise TokenizationError("failed to initialize Jieba or load its configured dictionary") from None
        self._engine = engine

    def tokenize(self, text: str) -> list[str]:
        if not isinstance(text, str):
            raise TokenizationError("text must be a string")
        if len(text) > self.max_chars:
            raise TokenizationError("tokenizer input exceeds character limit")
        normalized = unicodedata.normalize("NFKC", text).lower()
        with self._lock:
            self._initialize()
            tokens = []
            for token in self._engine.cut_for_search(normalized, HMM=False):
                token = token.strip()
                if token and HAS_WORD.search(token):
                    tokens.append(token)
                if len(tokens) > self.max_tokens:
                    raise TokenizationError("tokenizer output exceeds token limit")
            seen = set(tokens)
            for match in IDENTIFIER.finditer(normalized):
                identifier = match.group()
                if identifier not in seen:
                    tokens.append(identifier)
                    seen.add(identifier)
                # PostgreSQL's parser can split punctuation even after Jieba.
                # Add an alphanumeric lexeme so exact full IDs survive PG FTS.
                if any(character in identifier for character in "-_./"):
                    encoded = "zkid" + identifier.encode("utf-8").hex()
                    if encoded not in seen:
                        tokens.append(encoded)
                        seen.add(encoded)
                if len(tokens) > self.max_tokens:
                    raise TokenizationError("tokenizer output exceeds token limit")
            for token in tuple(tokens):
                canonical = self._alias_targets.get(token)
                if canonical is not None and canonical not in seen:
                    tokens.append(canonical)
                    seen.add(canonical)
                    if len(tokens) > self.max_tokens:
                        raise TokenizationError("tokenizer output exceeds token limit")
            return tokens

    def fts_text(self, text: str) -> str:
        return " ".join(self.tokenize(text))
