"""Deterministic, structure-preserving chunks and embedding-input fingerprints."""
from __future__ import annotations

import hashlib
import json
import re
from .domain import Chunk, ParsedDocument

CHUNKER_VERSION = "structure-v1:1400:120"


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chunk_document(parsed: ParsedDocument, max_chars: int = 1400, overlap: int = 120) -> tuple[Chunk, ...]:
    if max_chars < 200 or overlap < 0 or overlap >= max_chars:
        raise ValueError("invalid chunk bounds")
    chunks: list[Chunk] = []
    for section_no, section in enumerate(parsed.sections):
        text = section.text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        start = 0
        while start < len(text):
            stop = min(start + max_chars, len(text))
            if stop < len(text):
                candidates = list(re.finditer(r"[\n。！？；]", text[start:stop]))
                if candidates and candidates[-1].end() > max_chars // 2:
                    stop = start + candidates[-1].end()
            excerpt = text[start:stop]
            # Header context is part of the embedding input. Title/URL are metadata.
            embedding_text = (section.heading + "\n" + excerpt).strip()
            content_hash = digest(embedding_text)
            locator = {**section.locator, "section": section_no, "char_start": start, "char_end": stop}
            ordinal = len(chunks)
            chunks.append(Chunk(f"c-{ordinal}-{content_hash[:16]}", excerpt, embedding_text,
                                content_hash, locator, ordinal, section.heading))
            if stop == len(text):
                break
            start = max(start + 1, stop - overlap)
    return tuple(chunks)


def parsed_hash(parsed: ParsedDocument) -> str:
    # Include structure/locators: equal visible text in a different table/page is not the same evidence.
    return digest(canonical({"sections": [{"text": s.text, "heading": s.heading, "locator": s.locator}
                                          for s in parsed.sections],
                             "warnings": parsed.warnings, "parser_version": parsed.parser_version}))
