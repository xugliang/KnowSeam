import pytest
from pydantic import ValidationError


def test_table_request_strict_and_bounded():
    from knowseam.tables.contracts import TableReadRequest
    for changes in ({'row_start': True}, {'row_limit': 101}, {'column_ids': []},
                    {'column_ids': ['c-1', 'c-1']}, {'sql': 'select 1'}):
        with pytest.raises(ValidationError):
            TableReadRequest(document_id='d', version_id='v', table_id='t', **changes)


def test_merge_anchor_outside_window_and_budget():
    from knowseam.tables.domain import Cell, Column, ParsedTable
    from knowseam.tables.contracts import TableReadRequest
    from knowseam.tables.service import read_table
    table = ParsedTable(table_id='t', row_count=5,
        columns=(Column(column_id='c-1', index=1), Column(column_id='c-2', index=2)),
        cells=(Cell(row=1, column=1, row_span=4, column_span=2, raw_text='merged', value='merged', value_kind='text'),))
    request = TableReadRequest(document_id='d', version_id='v', table_id='t', row_start=3, row_limit=1, column_ids=['c-2'])
    value = read_table(request, table)
    assert len(value['cells']) == 1 and value['cells'][0]['anchor_outside_window'] is True
    assert value['cells'][0]['row'] == 1
    assert value['next_row'] == 4
    assert value['citations'][0]['locator']['row'] == 1
