from psycopg.types.json import Jsonb

from ..capabilities import CapabilityUnavailable
from ..context_store import require_document
from ..domain import NotFound
from .contracts import TableReader
from .domain import Column, ParsedTable, validate_tables
from .service import read_table, selected_columns, TableRequestError


def write_tables(connection, version_id, parsed):
    tables = validate_tables(parsed.tables)
    connection.execute('INSERT INTO sk_structure_manifests VALUES(%s,%s)', (version_id, parsed.structure_version == 2))
    for table in tables:
        connection.execute('INSERT INTO sk_tables(version_id,table_id,name,row_count,columns,locator) VALUES(%s,%s,%s,%s,%s,%s)',
            (version_id, table.table_id, table.name, table.row_count,
             Jsonb([c.model_dump(mode='json') for c in table.columns]), Jsonb(table.locator)))
        with connection.cursor() as cursor:
            cursor.executemany('INSERT INTO sk_table_cells VALUES(%s,%s,%s,%s,%s,%s,%s)', [
                (version_id, table.table_id, c.row, c.column, c.row_span, c.column_span, Jsonb(c.model_dump(mode='json')))
                for c in table.cells])


class PostgresTableReader(TableReader):
    def __init__(self, store):
        self.store = store

    def _require(self, connection, principal, request):
        doc = require_document(self.store, connection, principal, request.document_id, request.version_id)
        manifest = connection.execute('SELECT tables_available FROM sk_structure_manifests WHERE version_id=%s',
                                      (request.version_id,)).fetchone()
        if not manifest or not manifest['tables_available']:
            raise CapabilityUnavailable('table_structure_unavailable')
        return doc

    def _revalidate(self, principal, request):
        with self.store.connect() as c:
            return self._require(c, principal, request)

    def list_tables(self, principal, request):
        with self.store.connect() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            self._require(c, principal, request)
            rows = c.execute('SELECT table_id,name,row_count,jsonb_array_length(columns) AS column_count,locator '
                'FROM sk_tables WHERE version_id=%s ORDER BY table_id LIMIT %s OFFSET %s',
                (request.version_id, request.limit+1, request.offset)).fetchall()
        doc = self._revalidate(principal, request)
        return {'document_id': request.document_id, 'version_id': request.version_id, **doc,
                'tables': rows[:request.limit], 'next_offset': request.offset+request.limit if len(rows) > request.limit else None,
                'table_status': 'available'}

    def read_table(self, principal, request):
        with self.store.connect() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            self._require(c, principal, request)
            row = c.execute('SELECT table_id,name,row_count,columns,locator FROM sk_tables WHERE version_id=%s AND table_id=%s',
                            (request.version_id, request.table_id)).fetchone()
            if row is None:
                raise NotFound('table_not_available')
            columns = tuple(Column.model_validate(value) for value in row['columns'])
            selected = selected_columns(request, columns)
            # Include merged anchors outside the window, but only when their span intersects it.
            cells = c.execute('SELECT cell FROM sk_table_cells WHERE version_id=%s AND table_id=%s '
                'AND row_no<%s AND row_no+row_span>%s AND EXISTS '
                '(SELECT 1 FROM unnest(%s::int[]) col WHERE col>=column_no AND col<column_no+column_span) '
                'ORDER BY row_no,column_no LIMIT 4097',
                (request.version_id, request.table_id, request.row_start+request.row_limit,
                 request.row_start, [col.index for col in selected])).fetchall()
            if len(cells) > 4096:
                raise TableRequestError('table_window_exceeds_budget')
            table = ParsedTable.model_validate({**row, 'cells': [cell['cell'] for cell in cells]})
        result = read_table(request, table)
        doc = self._revalidate(principal, request)
        result['collection_id'] = doc['collection_id']
        result['citations'] = [{**value, **doc} for value in result['citations']]
        return result
