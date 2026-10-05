from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from contextlib import contextmanager

import pytest

from app.cloud_store import CompatConnection
from app.lookup_shadow_import import (
    IMPORT_ADVISORY_LOCK,
    ShadowLookupImporter,
)


ITEM_HASH = "a" * 64
PO_HASH = "b" * 64
COLUMNS = {
    "item": {
        "description": ["ITEM_DESC"],
        "sku": ["VPN"],
        "gtin": ["ITEM"],
        "uom": ["STANDARD_UOM"],
        "pack": [],
        "supplier": ["SUPPLIER_NAME"],
        "site": ["SUPPLIER"],
        "identity": [],
        "internal_item": [],
    },
    "po": {
        "description": [],
        "sku": ["RMS_ITEM_ID"],
        "gtin": ["BARCODE"],
        "uom": [],
        "pack": [],
        "supplier": ["SUP_NAME"],
        "site": ["LOCATION"],
        "po": ["RMS_ORDER_NO"],
        "identity": [],
        "internal_item": [],
    },
}


def _item(row, sku="SAMPLE-1"):
    return {
        "kind": "item",
        "source_hash": ITEM_HASH,
        "source_sheet": "Synthetic Items",
        "source_row": row,
        "keys": [sku, "Synthetic soap"],
        "data": {
            "ITEM": f"000000000000{row}",
            "VPN": sku,
            "ITEM_DESC": "Synthetic soap",
            "STANDARD_UOM": "EA",
            "SUPPLIER_NAME": "Example Supplier",
            "SUPPLIER": "SITE-1",
        },
        "flags": [],
    }


def _po(row):
    return {
        "kind": "po",
        "source_hash": PO_HASH,
        "source_sheet": "Synthetic Orders",
        "source_row": row,
        "keys": ["PO-100", "SAMPLE-1"],
        "data": {
            "RMS_ITEM_ID": "SAMPLE-1",
            "BARCODE": "0000000000001",
            "RMS_ORDER_NO": "PO-100",
            "LOCATION": "SITE-1",
            "SUP_NAME": "Example Supplier",
        },
        "flags": [],
    }


def _catalog(tmp_path, rows, name):
    archive = tmp_path / f"{name}.ndjson.gz"
    with gzip.open(archive, "wb") as output:
        for row in rows:
            output.write(json.dumps(row, separators=(",", ":")).encode() + b"\n")
    counts = {
        "item": sum(row["kind"] == "item" for row in rows),
        "po": sum(row["kind"] == "po" for row in rows),
    }
    counts["total"] = counts["item"] + counts["po"]
    used = sorted({row["source_hash"] for row in rows})
    manifest = {
        "manifest_version": "lookup-catalog.v1",
        "output_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "expected_counts": counts,
        "sources": [{"label": "synthetic", "hash": value, "count": sum(
            row["source_hash"] == value for row in rows
        )} for value in used],
        "columns": COLUMNS,
    }
    manifest_path = tmp_path / f"{name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return archive, manifest_path


class _SchemaStore:
    def __init__(self, database_url, schema, psycopg, sql):
        self.database_url = database_url
        self.schema = schema
        self.psycopg = psycopg
        self.sql = sql

    @contextmanager
    def connection(self, transaction=False):
        del transaction
        raw = self.psycopg.connect(self.database_url)
        raw.execute(self.sql.SQL("SET search_path TO {}").format(self.sql.Identifier(self.schema)))
        raw.commit()
        try:
            yield CompatConnection(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()


@pytest.fixture
def postgres_lookup_store():
    database_url = os.getenv("INV_STUDIO_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set INV_STUDIO_TEST_DATABASE_URL to run isolated PostgreSQL integration")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "lookup_shadow_test_" + uuid.uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = _SchemaStore(database_url, schema, psycopg, sql)
    with store.connection() as connection:
        connection.execute(
            "CREATE TABLE lookup_sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
        )
        connection.execute(
            """CREATE TABLE lookup_rows(
                id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_hash TEXT NOT NULL,
                sheet TEXT NOT NULL,row_number INTEGER NOT NULL,payload TEXT NOT NULL)"""
        )
        connection.execute(
            """CREATE TABLE lookup_terms(
                kind TEXT NOT NULL,term TEXT NOT NULL,row_id TEXT NOT NULL,
                PRIMARY KEY(kind,term,row_id))"""
        )
        connection.execute("CREATE INDEX lookup_rows_kind_id ON lookup_rows(kind,id)")
        connection.execute(
            "CREATE UNIQUE INDEX lookup_rows_source_row ON lookup_rows(source_hash,sheet,row_number)"
        )
    try:
        yield store
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _seed_live(store):
    payload = json.dumps({"id": "old-row", "kind": "item"})
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO lookup_rows VALUES (?,?,?,?,?,?)",
            ("old-row", "item", "f" * 64, "Old", 1, payload),
        )
        connection.execute(
            "INSERT INTO lookup_terms VALUES (?,?,?)", ("item", "i:old", "old-row")
        )
        connection.execute(
            "INSERT INTO lookup_sources VALUES (?,?)", ("old-source", "{}")
        )


def test_shadow_import_publishes_complete_catalog_and_removes_shadow_tables(
    tmp_path, postgres_lookup_store
):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(1), _po(1)], "complete")

    result = ShadowLookupImporter(postgres_lookup_store).import_archive(
        archive, manifest, batch_size=100, throttle_seconds=0
    )

    assert result["source"]["counts"] == {"item": 1, "po": 1, "total": 2}
    assert result["resumed_from_line"] == 0
    with postgres_lookup_store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM lookup_rows").fetchone()[0] == 2
        assert connection.execute(
            "SELECT 1 FROM lookup_rows WHERE id=?", ("old-row",)
        ).fetchone() is None
        assert connection.execute(
            "SELECT COUNT(*) FROM lookup_sources"
        ).fetchone()[0] == 1
        leftovers = connection.execute(
            "SELECT relname FROM pg_class WHERE relname LIKE 'lookup_%%_shadow_%%' "
            "OR relname LIKE 'lookup_%%_batch_%%' OR relname LIKE 'lookup_import_meta_%%'"
        ).fetchall()
        connection.execute("SET LOCAL enable_seqscan=off")
        plan = " ".join(
            row[0]
            for row in connection.execute(
                """EXPLAIN (COSTS OFF) SELECT row_id FROM lookup_terms
                   WHERE kind='item' AND term LIKE ? ESCAPE '\\'""",
                ("t:syn%",),
            )
        )
    assert leftovers == []
    assert "lookup_terms_kind_term_pattern" in plan
    assert "~>=~" in plan and "~<~" in plan


def test_failed_cutover_preserves_live_catalog_and_retry_resumes_shadow(
    tmp_path, postgres_lookup_store
):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(2, "SAMPLE-2"), _po(2)], "resume")
    blocker = postgres_lookup_store.psycopg.connect(postgres_lookup_store.database_url)
    blocker.execute(
        postgres_lookup_store.sql.SQL("SET search_path TO {}").format(
            postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema)
        )
    )
    blocker.execute("LOCK TABLE lookup_rows IN ACCESS SHARE MODE")
    try:
        with pytest.raises(RuntimeError, match="two-second lock"):
            ShadowLookupImporter(postgres_lookup_store).import_archive(
                archive, manifest, batch_size=100, throttle_seconds=0
            )
        with postgres_lookup_store.connection() as connection:
            assert connection.execute(
                "SELECT id FROM lookup_rows"
            ).fetchone()[0] == "old-row"
    finally:
        blocker.rollback()
        blocker.close()

    # Simulate a shadow created by the preceding importer version. Resume must
    # add the new pattern index to the shadow without touching the live tables.
    with postgres_lookup_store.connection() as connection:
        pattern_index = connection.execute(
            """SELECT relname FROM pg_class
               WHERE relname LIKE 'lookup_terms_shadow_%%_kind_term_pattern'"""
        ).fetchone()[0]
        connection.execute(f"DROP INDEX {pattern_index}")
        assert connection.execute(
            "SELECT id FROM lookup_rows"
        ).fetchone()[0] == "old-row"

    result = ShadowLookupImporter(postgres_lookup_store).import_archive(
        archive, manifest, batch_size=100, throttle_seconds=0
    )

    assert result["resumed_from_line"] == 2
    with postgres_lookup_store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM lookup_rows").fetchone()[0] == 2
        assert connection.execute(
            "SELECT 1 FROM lookup_rows WHERE id=?", ("old-row",)
        ).fetchone() is None


def test_invalid_archive_does_not_touch_live_catalog(tmp_path, postgres_lookup_store):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(3)], "invalid")
    body = json.loads(manifest.read_text())
    body["expected_counts"] = {"item": 2, "po": 0, "total": 2}
    manifest.write_text(json.dumps(body))

    with pytest.raises(ValueError, match="counts do not match"):
        ShadowLookupImporter(postgres_lookup_store).import_archive(
            archive, manifest, batch_size=100, throttle_seconds=0
        )

    with postgres_lookup_store.connection() as connection:
        assert connection.execute("SELECT id FROM lookup_rows").fetchone()[0] == "old-row"


def test_import_fails_fast_when_another_lookup_import_holds_the_session_lock(
    tmp_path, postgres_lookup_store
):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(4)], "locked")
    blocker = postgres_lookup_store.psycopg.connect(postgres_lookup_store.database_url)
    blocker.execute("SELECT pg_advisory_lock(%s)", (IMPORT_ADVISORY_LOCK,))
    try:
        with pytest.raises(RuntimeError, match="already running"):
            ShadowLookupImporter(postgres_lookup_store).import_archive(
                archive, manifest, batch_size=100, throttle_seconds=0
            )
    finally:
        blocker.execute("SELECT pg_advisory_unlock(%s)", (IMPORT_ADVISORY_LOCK,))
        blocker.close()

    with postgres_lookup_store.connection() as connection:
        assert connection.execute("SELECT id FROM lookup_rows").fetchone()[0] == "old-row"


def test_manifest_mapping_change_uses_a_distinct_resumable_shadow(
    tmp_path, postgres_lookup_store, monkeypatch
):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(5), _po(5)], "identity")
    blocker = postgres_lookup_store.psycopg.connect(postgres_lookup_store.database_url)
    blocker.execute(
        postgres_lookup_store.sql.SQL("SET search_path TO {}").format(
            postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema)
        )
    )
    blocker.execute("LOCK TABLE lookup_rows IN ACCESS SHARE MODE")
    try:
        with pytest.raises(RuntimeError, match="two-second lock"):
            ShadowLookupImporter(postgres_lookup_store).import_archive(
                archive, manifest, batch_size=100, throttle_seconds=0
            )
        changed = json.loads(manifest.read_text())
        changed["columns"]["item"]["description"].append("UNUSED_DESCRIPTION")
        manifest.write_text(json.dumps(changed))

        import app.lookup_shadow_import as shadow_module

        writes = 0
        original_write = shadow_module._write_batch

        def counted_write(*args, **kwargs):
            nonlocal writes
            writes += 1
            return original_write(*args, **kwargs)

        monkeypatch.setattr(shadow_module, "_write_batch", counted_write)
        with pytest.raises(RuntimeError, match="two-second lock"):
            ShadowLookupImporter(postgres_lookup_store).import_archive(
                archive, manifest, batch_size=100, throttle_seconds=0
            )
        assert writes == 1
    finally:
        blocker.rollback()
        blocker.close()

    result = ShadowLookupImporter(postgres_lookup_store).import_archive(
        archive, manifest, batch_size=100, throttle_seconds=0
    )
    assert result["resumed_from_line"] == 2


def test_corrupt_resume_state_is_discarded_without_touching_live_catalog(
    tmp_path, postgres_lookup_store
):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(6), _po(6)], "corrupt")
    blocker = postgres_lookup_store.psycopg.connect(postgres_lookup_store.database_url)
    blocker.execute(
        postgres_lookup_store.sql.SQL("SET search_path TO {}").format(
            postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema)
        )
    )
    blocker.execute("LOCK TABLE lookup_rows IN ACCESS SHARE MODE")
    try:
        with pytest.raises(RuntimeError, match="two-second lock"):
            ShadowLookupImporter(postgres_lookup_store).import_archive(
                archive, manifest, batch_size=100, throttle_seconds=0
            )
    finally:
        blocker.rollback()
        blocker.close()

    with postgres_lookup_store.connection() as connection:
        terms_table = connection.execute(
            """SELECT relname FROM pg_class
               WHERE relname LIKE 'lookup_terms_shadow_%%'"""
        ).fetchone()[0]
        connection.execute(
            f"DELETE FROM {terms_table} WHERE ctid IN (SELECT ctid FROM {terms_table} LIMIT 1)"
        )

    with pytest.raises(ValueError, match="progress is inconsistent"):
        ShadowLookupImporter(postgres_lookup_store).import_archive(
            archive, manifest, batch_size=100, throttle_seconds=0
        )
    with postgres_lookup_store.connection() as connection:
        assert connection.execute("SELECT id FROM lookup_rows").fetchone()[0] == "old-row"
        assert connection.execute(
            "SELECT COUNT(*) FROM pg_class WHERE relname LIKE 'lookup_%%_shadow_%%'"
        ).fetchone()[0] == 0

    result = ShadowLookupImporter(postgres_lookup_store).import_archive(
        archive, manifest, batch_size=100, throttle_seconds=0
    )
    assert result["resumed_from_line"] == 0


def test_declared_per_source_counts_are_enforced(tmp_path, postgres_lookup_store):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(7), _po(7)], "source-counts")
    body = json.loads(manifest.read_text())
    body["sources"][0]["count"] = 0
    body["sources"][1]["count"] = 2
    manifest.write_text(json.dumps(body))

    with pytest.raises(ValueError, match="source counts do not match"):
        ShadowLookupImporter(postgres_lookup_store).import_archive(
            archive, manifest, batch_size=100, throttle_seconds=0
        )
    with postgres_lookup_store.connection() as connection:
        assert connection.execute("SELECT id FROM lookup_rows").fetchone()[0] == "old-row"


def test_cutover_preserves_select_grant_for_runtime_role(tmp_path, postgres_lookup_store):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(8), _po(8)], "grants")
    role = "lookup_reader_" + uuid.uuid4().hex
    with postgres_lookup_store.psycopg.connect(
        postgres_lookup_store.database_url, autocommit=True
    ) as admin:
        admin.execute(postgres_lookup_store.sql.SQL("CREATE ROLE {}").format(
            postgres_lookup_store.sql.Identifier(role)
        ))
        admin.execute(postgres_lookup_store.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema),
            postgres_lookup_store.sql.Identifier(role),
        ))
        admin.execute(postgres_lookup_store.sql.SQL(
            "GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}"
        ).format(
            postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema),
            postgres_lookup_store.sql.Identifier(role),
        ))
    try:
        ShadowLookupImporter(postgres_lookup_store).import_archive(
            archive, manifest, batch_size=100, throttle_seconds=0
        )
        with postgres_lookup_store.psycopg.connect(
            postgres_lookup_store.database_url
        ) as runtime:
            runtime.execute(postgres_lookup_store.sql.SQL("SET search_path TO {}").format(
                postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema)
            ))
            runtime.execute(postgres_lookup_store.sql.SQL("SET ROLE {}").format(
                postgres_lookup_store.sql.Identifier(role)
            ))
            assert runtime.execute("SELECT COUNT(*) FROM lookup_rows").fetchone()[0] == 2
    finally:
        with postgres_lookup_store.psycopg.connect(
            postgres_lookup_store.database_url, autocommit=True
        ) as admin:
            admin.execute(postgres_lookup_store.sql.SQL(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}"
            ).format(
                postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema),
                postgres_lookup_store.sql.Identifier(role),
            ))
            admin.execute(postgres_lookup_store.sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(
                postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema),
                postgres_lookup_store.sql.Identifier(role),
            ))
            admin.execute(postgres_lookup_store.sql.SQL("DROP ROLE {}").format(
                postgres_lookup_store.sql.Identifier(role)
            ))


def test_batches_are_bounded_by_serialized_bytes(tmp_path, postgres_lookup_store, monkeypatch):
    rows = [_item(row, f"SAMPLE-{row}") for row in range(20, 40)]
    archive, manifest = _catalog(tmp_path, rows, "bounded")

    import app.lookup_shadow_import as shadow_module

    monkeypatch.setattr(shadow_module, "MAX_BATCH_BYTES", 5_000)
    observed = []
    original_write = shadow_module._write_batch

    def bounded_write(connection, names, batch, *args):
        observed.append(sum(shadow_module._record_load(record)[0] for record in batch))
        return original_write(connection, names, batch, *args)

    monkeypatch.setattr(shadow_module, "_write_batch", bounded_write)
    ShadowLookupImporter(postgres_lookup_store).import_archive(
        archive, manifest, batch_size=100, throttle_seconds=0
    )

    assert len(observed) > 1
    assert max(observed) <= 5_000


def test_term_primary_key_writes_are_sorted_with_bounded_local_memory(
    tmp_path, postgres_lookup_store, monkeypatch
):
    rows = [_item(row, f"SAMPLE-{row}") for row in range(40, 45)]
    archive, manifest = _catalog(tmp_path, rows, "sorted-terms")

    import app.lookup_shadow_import as shadow_module

    statements = []
    original_write = shadow_module._write_batch

    class RecordingConnection:
        def __init__(self, connection):
            self.delegate = connection
            self.connection = connection.connection

        def execute(self, sql, params=()):
            statements.append(" ".join(sql.split()))
            return self.delegate.execute(sql, params)

    def recording_write(connection, names, batch, *args):
        return original_write(RecordingConnection(connection), names, batch, *args)

    monkeypatch.setattr(shadow_module, "_write_batch", recording_write)
    ShadowLookupImporter(postgres_lookup_store).import_archive(
        archive, manifest, batch_size=100, throttle_seconds=0
    )

    assert "SET LOCAL work_mem='16MB'" in statements
    term_insert = next(
        statement for statement in statements
        if statement.startswith("INSERT INTO lookup_terms_shadow_")
    )
    assert term_insert.endswith("ORDER BY kind,term,row_id")


def test_incompatible_shadow_index_is_discarded_and_live_catalog_is_preserved(
    tmp_path, postgres_lookup_store
):
    _seed_live(postgres_lookup_store)
    archive, manifest = _catalog(tmp_path, [_item(9), _po(9)], "schema")
    blocker = postgres_lookup_store.psycopg.connect(postgres_lookup_store.database_url)
    blocker.execute(
        postgres_lookup_store.sql.SQL("SET search_path TO {}").format(
            postgres_lookup_store.sql.Identifier(postgres_lookup_store.schema)
        )
    )
    blocker.execute("LOCK TABLE lookup_rows IN ACCESS SHARE MODE")
    try:
        with pytest.raises(RuntimeError, match="two-second lock"):
            ShadowLookupImporter(postgres_lookup_store).import_archive(
                archive, manifest, batch_size=100, throttle_seconds=0
            )
    finally:
        blocker.rollback()
        blocker.close()

    with postgres_lookup_store.connection() as connection:
        index_name = connection.execute(
            """SELECT relname FROM pg_class
               WHERE relname LIKE 'lookup_rows_shadow_%%_kind_id'"""
        ).fetchone()[0]
        connection.execute(f"DROP INDEX {index_name}")

    with pytest.raises(ValueError, match="index definitions are incompatible"):
        ShadowLookupImporter(postgres_lookup_store).import_archive(
            archive, manifest, batch_size=100, throttle_seconds=0
        )
    with postgres_lookup_store.connection() as connection:
        assert connection.execute("SELECT id FROM lookup_rows").fetchone()[0] == "old-row"
        assert connection.execute(
            "SELECT COUNT(*) FROM pg_class WHERE relname LIKE 'lookup_%%_shadow_%%'"
        ).fetchone()[0] == 0
