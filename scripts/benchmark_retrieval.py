"""Synthetic retrieval benchmark. Only a loopback database named knowseam_test.

No model inference, production configuration, private source data or host tuning.
Invoke in a disposable container via tests/test_benchmark_postgres.py and
scripts/verify_postgres.py. One size per container; output has no credentials.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import statistics
import time
import uuid

from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from knowseam.chunking import digest
from knowseam.domain import Principal
from knowseam.indexing.cache import payload_key
from knowseam.migrations.runner import MigrationRunner
from knowseam.observability import trace_scope
from knowseam.storage import PostgresStore, vector_literal
from knowseam.tokenization import JiebaTokenizer


def checked_size(value):
    if type(value) is not int or value not in (1000, 10000, 100000):
        raise ValueError("benchmark_size_must_be_1k_10k_100k")
    return value


def checked_store(dsn):
    info = conninfo_to_dict(dsn)
    if info.get("dbname") != "knowseam_test" or info.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("benchmark_requires_loopback_knowseam_test")
    store = PostgresStore(dsn, acl_ttl_seconds=86400)
    with store.connect() as c:
        if c.execute("SELECT current_database() name").fetchone()["name"] != "knowseam_test":
            raise ValueError("benchmark_database_identity_mismatch")
    return store


def summary(values):
    ordered = sorted(values)
    return dict(
        p50_ms=round(statistics.median(ordered), 3),
        p95_ms=round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    )


def node_metrics(node):
    fields = (
        "Node Type",
        "Actual Rows",
        "Actual Loops",
        "Shared Hit Blocks",
        "Shared Read Blocks",
        "Temp Read Blocks",
        "Temp Written Blocks",
        "Sort Method",
        "Sort Space Used",
        "Peak Memory Usage",
    )
    return {
        **{k: node[k] for k in fields if k in node},
        "children": [node_metrics(p) for p in node.get("Plans", ())],
    }


@contextmanager
def capture_sql(store, captured):
    original = store.connect

    class Connection:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, params=None):
            if isinstance(sql, str) and (sql.startswith("WITH eligible") or "1-(ch.embedding <=>" in sql):
                captured["lexical" if sql.startswith("WITH eligible") else "dense"] = (sql, params)
            return self.c.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.c, name)

    @contextmanager
    def connect():
        with original() as c:
            yield Connection(c)

    store.connect = connect
    try:
        yield
    finally:
        store.connect = original


def populate(store, company, source, count, prefix, *, visible, tokenizer):
    with store.connect() as c:
        c.execute(
            "CREATE TEMP TABLE benchmark_templates(i integer,payload_id text,text text,tokens text,n integer) ON COMMIT DROP"
        )
        for i in range(100):
            text = (
                "alpha " + ("filler " * (i % 10)) + str(i) if visible else "alpha " * 20 + "private " + str(i)
            )
            tokens = tokenizer.fts_text(text).split()
            key = payload_key(company, tokenizer.version, text)
            c.execute(
                "INSERT INTO sk_token_cache VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (key, company, tokenizer.version, digest(text), len(tokens), Jsonb(tokens)),
            )
            for term, tf in Counter(tokens).items():
                c.execute(
                    "INSERT INTO sk_term_payloads VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (key, term, tf)
                )
            c.execute(
                "INSERT INTO benchmark_templates VALUES(%s,%s,%s,%s,%s)",
                (i, key, text, " ".join(tokens), len(tokens)),
            )
        c.execute(
            "INSERT INTO sk_documents(id,source_id,external_id,title,source_url,public,acl_verified_at,status) "
            "SELECT %s||g,%s,%s||g,'synthetic','',%s,now(),'ready' FROM generate_series(0,%s) g",
            (prefix, source, prefix, visible, count // 100 - 1),
        )
        c.execute(
            "INSERT INTO sk_versions(id,document_id,raw_hash,content_hash,pipeline_id,model_id,dimensions,source_revision,raw_path,warnings) "
            "SELECT id||'-v',id,'synthetic','synthetic','benchmark','synthetic-d32',32,'1','','[]'::jsonb "
            "FROM sk_documents WHERE source_id=%s AND starts_with(id,%s)",
            (source, prefix),
        )
        vector = vector_literal([1] + [0.1] * 31, 32)
        c.execute(
            "INSERT INTO sk_chunks(version_id,chunk_id,ordinal,text,heading,embedding_text,content_hash,locator,tokens,embedding,token_count) "
            "SELECT %s||(g/100)||'-v','c-'||lpad(g::text,7,'0'),g%%100,t.text,'',t.text,md5(t.text),'{}'::jsonb,"
            "to_tsvector('simple',t.tokens),%s::vector,t.n FROM generate_series(0,%s) g JOIN benchmark_templates t ON t.i=g%%100",
            (prefix, vector, count - 1),
        )
        c.execute(
            "INSERT INTO sk_index_revisions SELECT v.id,%s,'shadow',true FROM sk_versions v "
            "JOIN sk_documents d ON d.id=v.document_id WHERE d.source_id=%s AND starts_with(d.id,%s)",
            (tokenizer.version, source, prefix),
        )
        c.execute(
            "INSERT INTO sk_term_postings SELECT ch.version_id,ch.chunk_id,t.payload_id FROM sk_chunks ch "
            "JOIN sk_versions v ON v.id=ch.version_id JOIN sk_documents d ON d.id=v.document_id "
            "JOIN benchmark_templates t ON t.i=ch.ordinal WHERE d.source_id=%s AND starts_with(d.id,%s)",
            (source, prefix),
        )
        c.execute(
            "INSERT INTO sk_chunk_terms SELECT p.version_id,p.chunk_id,t.term,t.tf FROM sk_term_postings p "
            "JOIN sk_term_payloads t USING(payload_id) JOIN sk_versions v ON v.id=p.version_id "
            "JOIN sk_documents d ON d.id=v.document_id WHERE d.source_id=%s AND starts_with(d.id,%s)",
            (source, prefix),
        )
        c.execute(
            "UPDATE sk_documents SET active_version=id||'-v' WHERE source_id=%s AND starts_with(id,%s)",
            (source, prefix),
        )
        for table in (
            "sk_chunks",
            "sk_versions",
            "sk_documents",
            "sk_chunk_terms",
            "sk_term_postings",
            "sk_term_payloads",
        ):
            c.execute("ANALYZE " + table)


def benchmark(store, size, repeats=20):
    checked_size(size)
    if type(repeats) is not int or not 5 <= repeats <= 50:
        raise ValueError("benchmark_repeats_out_of_bounds")
    store.initialize()
    MigrationRunner(store.connect).apply()
    prefix = "bench-" + uuid.uuid4().hex[:12]
    company, collection, source = prefix + "-co", prefix + "-col", prefix + "-src"
    store.configure(
        company_id=company,
        collections=[{"id": collection, "title": "synthetic"}],
        sources=[{"id": source, "collection_id": collection, "config_hash": "benchmark-v1"}],
        employees=[{"actor_id": "reader", "active": True}],
    )
    principal = Principal("reader", company, "benchmark", (collection,))
    tokenizer = JiebaTokenizer()
    populate(store, company, source, size, prefix + "-visible-", visible=True, tokenizer=tokenizer)
    report = {
        "manifest": dict(
            version=1,
            visible_chunks=size,
            hidden_growth=100000,
            dimensions=32,
            model="synthetic-d32",
            inference=False,
            candidate_limit=40,
            query_terms=["alpha"],
            templates_per_scope=100,
            tokenizer_fingerprint=tokenizer.version,
            repeats=repeats,
            cache="first_call_then_warm_host_cache_unknown",
        ),
        "scenarios": [],
    }
    expected = None
    for hidden in (0, 100000):
        if hidden:
            populate(store, company, source, hidden, prefix + "-hidden-", visible=False, tokenizer=tokenizer)
        for reuse in (False, True):
            store.indexing_enabled = reuse
            captured, values = {}, {"total": [], "dense": [], "lexical": []}
            with capture_sql(store, captured):
                for turn in range(repeats + 3):
                    start = time.perf_counter()
                    with trace_scope() as trace:
                        dense, lexical = store.retrieve(
                            principal,
                            [collection],
                            vector=[1] + [0.1] * 31,
                            model_id="synthetic-d32",
                            dimensions=32,
                            tokens=["alpha"],
                            limit=40,
                        )
                    elapsed = (time.perf_counter() - start) * 1000
                    scores = [(h.chunk_id, h.score) for h in lexical]
                    if expected is None:
                        expected = scores
                    if scores != expected:
                        raise AssertionError("authorized_ranking_changed")
                    if turn >= 3:
                        values["total"].append(elapsed)
                        for stage in trace.stages:
                            if stage.phase in values:
                                values[stage.phase].append(stage.duration_ms)
            plans = {}
            with store.connect() as c:
                for kind, (sql, args) in captured.items():
                    raw = c.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql, args).fetchone()[
                        "QUERY PLAN"
                    ][0]
                    plans[kind] = dict(execution_ms=raw["Execution Time"], plan=node_metrics(raw["Plan"]))
                sizes = c.execute(
                    "SELECT relname,pg_relation_size(relid) AS table_bytes,pg_indexes_size(relid) AS index_bytes "
                    "FROM pg_catalog.pg_statio_user_tables WHERE relname LIKE 'sk_%%'"
                ).fetchall()
            report["scenarios"].append(
                dict(
                    hidden_chunks=hidden,
                    index="shared_postings" if reuse else "legacy",
                    latency={k: summary(v) for k, v in values.items()},
                    ranking_equal=True,
                    plans=plans,
                    storage=sizes,
                    failures=0,
                    timeouts=0,
                    serial_qps=round(1000 / (sum(values["total"]) / repeats), 3),
                )
            )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = benchmark(checked_store(os.environ.get("SUITE_KNOWLEDGE_TEST_DSN", "")), args.size)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
