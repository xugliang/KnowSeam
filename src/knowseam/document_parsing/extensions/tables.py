from ...domain import Section, UnsupportedDocument
from .cleanup import clean_text


def table_sections(item, locator, heading):
    data = item.data
    if data.num_rows * data.num_cols > 2_000_000:
        raise UnsupportedDocument('parser_table_cells_limit')
    cells = data.table_cells
    headers = [[] for _ in range(data.num_cols)]
    # Only extend the header band as far as explicitly marked header spans.
    # XLSX marks the first row but vertically merged headers establish that
    # lower cells in the same band are leaf fields, not disposable data rows.
    header_end = max((cell.end_row_offset_idx for cell in cells if cell.column_header), default=0)
    header_rows = set(range(header_end))
    for cell in cells:
        if cell.column_header or cell.start_row_offset_idx < header_end:
            text = clean_text(cell.text)
            for col in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                if 0 <= col < len(headers) and text and text not in headers[col]:
                    headers[col].append(text)
    labels = [' / '.join(values) or f'列{index + 1}' for index, values in enumerate(headers)]
    rows = {}
    for cell in cells:
        if cell.column_header:
            continue
        text = clean_text(cell.text)
        for row in range(cell.start_row_offset_idx, cell.end_row_offset_idx):
            if row in header_rows:
                continue
            values = rows.setdefault(row, [''] * data.num_cols)
            for col in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                if 0 <= col < len(values):
                    values[col] = text
    for row, values in sorted(rows.items()):
        if not any(values):
            continue
        parts, fields, offset = [], [], 0
        for label, value in zip(labels, values, strict=True):
            part = f'{label}: {value}'
            fields.append({'label': label, 'start': offset + len(label) + 2, 'end': offset + len(part)})
            parts.append(part)
            offset += len(part) + 3
        text = ' | '.join(parts)
        context = f'{labels[0]}: {values[0]}' if values and len(values[0]) <= 80 else ''
        yield Section(text, {**locator, 'table': str(item.self_ref),
                             'row': locator.get('row_start', 1) + row, 'row_in_table': row + 1,
                             'headers': labels, 'row_context': context, 'fields': fields}, heading)
