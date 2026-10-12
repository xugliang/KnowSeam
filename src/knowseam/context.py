"""Pure, bounded context assembly. Original sections, not embedding augmentation."""
from collections import defaultdict

from .contracts import ContextRequest
from .domain import NotFound
from .capabilities import CapabilityUnavailable


def assemble_context(request: ContextRequest, chunks, sections, spans) -> dict:
    by_chunk = {row['chunk_id']: row for row in chunks}
    if any(key not in by_chunk for key in request.chunk_ids):
        raise NotFound('context_not_available')
    anchors = [by_chunk[key]['ordinal'] for key in request.chunk_ids]
    selected = [row for row in chunks if (row['chunk_id'] in request.chunk_ids if request.mode == 'section'
                else any(n - request.before <= row['ordinal'] <= n + request.after for n in anchors))]
    selected.sort(key=lambda row: row['ordinal'])
    section_map = {row['section_id']: row for row in sections}
    keys = {row['chunk_id'] for row in selected}
    selected_spans = [s for s in spans if s['chunk_id'] in keys]
    raw_available = bool(selected_spans) and keys <= {s['chunk_id'] for s in selected_spans}
    ranges = []
    if not raw_available:
        if request.mode == 'section':
            raise CapabilityUnavailable('structure_unavailable')
        ranges = [{'chunk_id': row['chunk_id'], 'text': row['text'], 'locator': row['locator'],
                   'evidence_kind': 'indexed_chunk'} for row in selected]
        status = 'legacy_neighbors'
    else:
        intervals = defaultdict(list)
        for span in selected_spans:
            if span['section_id'] not in section_map:
                raise CapabilityUnavailable('structure_unavailable')
            section = section_map[span['section_id']]
            start, end = ((0, len(section['text'])) if request.mode == 'section'
                          else (span['start_char'], span['end_char']))
            intervals[span['section_id']].append((start, end))
        for section_id in sorted(intervals, key=lambda key: section_map[key]['ordinal']):
            section = section_map[section_id]
            merged = []
            for start, end in sorted(intervals[section_id]):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            for start, end in merged:
                ranges.append({'section_id': section_id, 'start_char': start, 'end_char': end,
                               'text': section['text'][start:end], 'heading': section['heading'],
                               'locator': section['locator'], 'evidence_kind': 'original_section'})
        status = 'available'
    output, remaining = [], request.max_chars
    truncated = len(ranges) > 100
    for row in ranges[:100]:
        if remaining <= 0:
            truncated = True
            break
        if len(row['text']) > remaining:
            row = {**row, 'text': row['text'][:remaining]}
            if 'start_char' in row:
                row['end_char'] = row['start_char'] + remaining
            truncated = True
        remaining -= len(row['text'])
        output.append(row)
    return {'document_id': request.document_id, 'version_id': request.version_id, 'ranges': output,
            'citations': [{'document_id': request.document_id, 'version_id': request.version_id,
                           **{k: v for k, v in row.items() if k not in {'text', 'heading'}}} for row in output],
            'context_status': status, 'truncated': truncated}
