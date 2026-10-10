"""Deterministic, structure-preserving chunks and embedding-input fingerprints."""
from __future__ import annotations

import hashlib
import json
import re
from .domain import Chunk, ParsedDocument

CHUNKER_VERSION = "structure-v4-table-field-aware:1400:120"


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
        table_context = ""
        if section.locator.get("table") and len(text) > max_chars:
            # Keep the bounded first-column identity before wide header text.
            row_context = str(section.locator.get("row_context", ""))[:90]
            budget = max(max_chars // 3, len(row_context) + 12)
            headers = "表头：" + " | ".join(section.locator.get("headers", []))
            table_context = row_context + "；" + headers[:budget-len(row_context)-2] + "\n"
        pieces = [(text, 0, '', table_context)]
        fields = section.locator.get('fields', [])
        if table_context and fields and all(
            isinstance(f, dict) and isinstance(f.get('label'), str)
            and isinstance(f.get('start'), int) and isinstance(f.get('end'), int)
            and 0 <= f['start'] <= f['end'] <= len(text) for f in fields
        ):
            row_context = str(section.locator.get('row_context', ''))[:max_chars // 4]
            headers = ' | '.join(section.locator.get('headers', []))[:max_chars // 5]
            pieces = [(text[f['start']:f['end']], f['start'], f['label'],
                       row_context + '；表头：' + headers + '\n' + f['label'][:max_chars // 4] + ': ')
                      for f in fields]
        for piece, offset, field, context in pieces:
            start = 0
            excerpt_limit = max_chars - len(context)
            while start < len(piece):
                stop = min(start + excerpt_limit, len(piece))
                if stop < len(piece):
                    candidates = list(re.finditer(r"[\n。！？；]", piece[start:stop]))
                    if candidates and candidates[-1].end() > excerpt_limit // 2:
                        stop = start + candidates[-1].end()
                excerpt = context + piece[start:stop]
                embedding_text = (section.heading + "\n" + excerpt).strip()
                content_hash = digest(embedding_text)
                locator = {**section.locator, "section": section_no,
                           "char_start": offset + start, "char_end": offset + stop}
                if field:
                    locator['field'] = field
                ordinal = len(chunks)
                chunks.append(Chunk(f"c-{ordinal}-{content_hash[:16]}", excerpt, embedding_text,
                                    content_hash, locator, ordinal, section.heading))
                if stop == len(piece):
                    break
                start = max(start + 1, stop - overlap)
    return tuple(chunks)


def parsed_hash(parsed: ParsedDocument) -> str:
    # Include structure/locators: equal visible text in a different table/page is not the same evidence.
    return digest(canonical({"sections": [{"text": s.text, "heading": s.heading, "locator": s.locator}
                                          for s in parsed.sections],
                             "warnings": parsed.warnings, "parser_version": parsed.parser_version}))
