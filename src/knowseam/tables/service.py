from ..domain import NotFound


class TableRequestError(ValueError):
    pass


def selected_columns(request, columns):
    ids = set(request.column_ids or (c.column_id for c in columns))
    selected = [c for c in columns if c.column_id in ids]
    if len(selected) != len(ids):
        raise NotFound('column_not_available')
    if len(selected) > 32:
        raise TableRequestError('explicit_column_selection_required')
    return selected


def read_table(request, table):
    columns = selected_columns(request, table.columns)
    indices = {c.index for c in columns}
    end = min(table.row_count, request.row_start + request.row_limit - 1)
    cells, citations = [], []
    for cell in table.cells:
        if cell.row > end or cell.row + cell.row_span <= request.row_start:
            continue
        if not any(cell.column <= index < cell.column + cell.column_span for index in indices):
            continue
        value = cell.model_dump()
        value['column_id'] = next(c.column_id for c in table.columns if c.index == cell.column)
        value['anchor_outside_window'] = cell.row < request.row_start or cell.column not in indices
        cells.append(value)
        citations.append({'document_id': request.document_id, 'version_id': request.version_id,
            'locator': {**table.locator, 'table_id': table.table_id, 'row': cell.row,
                        'column_id': value['column_id'], 'column': cell.column}})
    if len(cells) > 4096:
        raise TableRequestError('table_window_exceeds_budget')
    return {'document_id': request.document_id, 'version_id': request.version_id, 'table_id': table.table_id,
            'columns': [c.model_dump(mode='json') for c in columns], 'cells': cells, 'citations': citations,
            'next_row': end+1 if end < table.row_count else None, 'row_count': table.row_count,
            'table_status': 'available', 'truncated': end < table.row_count,
            'warnings': ['公式不执行；unknown 类型不得作为可靠数值汇总。']}
