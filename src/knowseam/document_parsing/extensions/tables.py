from ...domain import Section, UnsupportedDocument
from .cleanup import clean_text


def header_band(data):
    headers = [[] for _ in range(data.num_cols)]
    end = max((c.end_row_offset_idx for c in data.table_cells if c.column_header), default=0)
    for cell in data.table_cells:
        if cell.column_header or cell.start_row_offset_idx < end:
            text = clean_text(cell.text)
            for col in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                if 0 <= col < len(headers) and text and text not in headers[col]:
                    headers[col].append(text)
    return headers, end


def structured_table(item, locator, table_id):
    from ...tables.domain import Cell, Column, ParsedTable
    data = item.data
    cells = []
    headers, _ = header_band(data)
    for cell in data.table_cells:
        start, end = cell.start_col_offset_idx, cell.end_col_offset_idx
        cells.append(Cell(row=cell.start_row_offset_idx+1, column=start+1,
                          row_span=cell.end_row_offset_idx-cell.start_row_offset_idx,
                          column_span=end-start, raw_text=cell.text, value=cell.text, value_kind='unknown'))
    return ParsedTable(table_id=table_id, name=locator.get('sheet', ''), row_count=data.num_rows,
        columns=tuple(Column(column_id=f'c-{i+1}', index=i+1, header_path=tuple(values)) for i, values in enumerate(headers)),
        cells=tuple(cells), locator=locator)


def table_sections(item, locator, heading):
    data = item.data
    if data.num_rows * data.num_cols > 2_000_000:
        raise UnsupportedDocument('parser_table_cells_limit')
    cells = data.table_cells
    # Only extend the header band as far as explicitly marked header spans.
    # XLSX marks the first row but vertically merged headers establish that
    # lower cells in the same band are leaf fields, not disposable data rows.
    headers, header_end = header_band(data)
    header_rows = set(range(header_end))
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
