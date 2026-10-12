CREATE TABLE sk_structure_manifests (
    version_id text PRIMARY KEY REFERENCES sk_versions(id) ON DELETE CASCADE,
    tables_available boolean NOT NULL
);
CREATE TABLE sk_tables (
    version_id text NOT NULL REFERENCES sk_versions(id) ON DELETE CASCADE,
    table_id text NOT NULL,
    name text NOT NULL,
    row_count integer NOT NULL CHECK (row_count BETWEEN 0 AND 1000000),
    columns jsonb NOT NULL CHECK (jsonb_typeof(columns)='array'),
    locator jsonb NOT NULL CHECK (jsonb_typeof(locator)='object'),
    PRIMARY KEY (version_id,table_id)
);
CREATE TABLE sk_table_cells (
    version_id text NOT NULL,
    table_id text NOT NULL,
    row_no integer NOT NULL CHECK (row_no BETWEEN 1 AND 1000000),
    column_no integer NOT NULL CHECK (column_no BETWEEN 1 AND 16384),
    row_span integer NOT NULL CHECK (row_span>0),
    column_span integer NOT NULL CHECK (column_span>0),
    cell jsonb NOT NULL CHECK (jsonb_typeof(cell)='object'),
    PRIMARY KEY (version_id,table_id,row_no,column_no),
    FOREIGN KEY (version_id,table_id) REFERENCES sk_tables(version_id,table_id) ON DELETE CASCADE
);
