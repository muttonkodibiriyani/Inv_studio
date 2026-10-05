import gzip
import hashlib
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.cloud_store import CompatConnection
from app.reference_lookup import ReferenceLookup, _like_prefix, normalize_name
from app.store import Store


ROOT = Path(__file__).resolve().parents[1]
ITEM_HASH = "a" * 64
PO_HASH = "b" * 64
COLUMNS = {
    "item": {
        "description": ["ITEM_DESC", "SHORT_DESC"],
        "sku": ["VPN"],
        "gtin": ["ITEM"],
        "internal_item": ["ITEM_PARENT"],
        "uom": ["STANDARD_UOM"],
        "pack": ["SUPP_PACK_SIZE"],
        "supplier": ["SUPPLIER_NAME"],
        "site": ["SUPPLIER"],
        "identity": ["ITEM", "BRAND"],
    },
    "po": {
        "description": [],
        "sku": ["RMS_ITEM_ID"],
        "gtin": ["BARCODE"],
        "internal_item": ["RMS_ITEM_ID"],
        "uom": [],
        "pack": [],
        "supplier": ["EBS_SUPPLIER_CODE", "SUP_NAME"],
        "site": ["EBS_SUPPLIER_CODE", "LOCATION"],
        "po": ["RMS_ORDER_NO", "EXT_ORDER_NO"],
    },
}


def item(row, *, description="تفاح عضوي أحمر", site="SITE-1", sku="SKU-A", pack="1"):
    return {
        "kind": "item",
        "source_hash": ITEM_HASH,
        "source_sheet": "Item Master",
        "source_row": row,
        "keys": ["ITEM-100", sku, description],
        "data": {
            "ITEM": "GTIN-100",
            "ITEM_PARENT": "ITEM-100",
            "VPN": sku,
            "ITEM_DESC": description,
            "SHORT_DESC": description,
            "STANDARD_UOM": "EA",
            "SUPP_PACK_SIZE": pack,
            "SUPPLIER_NAME": "Fresh Foods",
            "SUPPLIER": site,
            "BRAND": "ORCHARD",
        },
        "flags": ["conflicting_site_sku"] if row == 3 else [],
    }


def po(row, *, order="PO-55"):
    return {
        "kind": "po",
        "source_hash": PO_HASH,
        "source_sheet": "PO Extract",
        "source_row": row,
        "keys": ["ITEM-100", order, "00012345678905"],
        "data": {
            "RMS_ITEM_ID": "ITEM-100",
            "BARCODE": "00012345678905",
            "RMS_ORDER_NO": order,
            "EXT_ORDER_NO": "EXT-55",
            "LOCATION": "SITE-1",
            "EBS_SUPPLIER_CODE": "SUP-1",
            "SUP_NAME": "Fresh Foods",
        },
        "flags": ["repeated_po_item_group"],
    }


def catalog(tmp_path, rows, *, name="catalog", counts=None):
    archive = tmp_path / f"{name}.ndjson.gz"
    with gzip.open(archive, "wb") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    actual = {"item": sum(row["kind"] == "item" for row in rows),
              "po": sum(row["kind"] == "po" for row in rows)}
    actual["total"] = actual["item"] + actual["po"]
    expected = counts or actual
    used = {row["source_hash"] for row in rows}
    sources = [{"label": "source", "hash": value, "count": sum(
        row["source_hash"] == value for row in rows)} for value in sorted(used)]
    manifest = {
        "manifest_version": "lookup-catalog.v1",
        "output_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "expected_counts": expected,
        "source_hashes": {"unused-example": "c" * 64, **{
            f"source-{index}": value for index, value in enumerate(sorted(used))}},
        "sources": sources,
        "columns": COLUMNS,
    }
    manifest_path = tmp_path / f"{name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return archive, manifest_path


@pytest.fixture
def lookup(tmp_path):
    return ReferenceLookup(Store(tmp_path / "data"))


@pytest.fixture
def postgres_lookup(tmp_path):
    database_url = os.getenv("INV_STUDIO_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set INV_STUDIO_TEST_DATABASE_URL for PostgreSQL lookup regression")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "reference_lookup_test_" + uuid.uuid4().hex
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    class SchemaStore:
        root = tmp_path / "pg-data"

        @contextmanager
        def connection(self):
            raw = psycopg.connect(database_url)
            raw.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            try:
                yield CompatConnection(raw)
                raw.commit()
            except BaseException:
                raw.rollback()
                raise
            finally:
                raw.close()

    store = SchemaStore()
    store.root.mkdir()
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
            "CREATE UNIQUE INDEX lookup_rows_source_row "
            "ON lookup_rows(source_hash,sheet,row_number)"
        )
    try:
        yield ReferenceLookup(store), store
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class _CatalogCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _CatalogConnection:
    def __init__(self, existing):
        self.connection = type("RawConnection", (), {"cursor": lambda self: None})()
        self.existing = existing
        self.statements = []

    def execute(self, sql, params=()):
        del params
        self.statements.append(" ".join(sql.split()))
        if "to_regclass" in sql:
            return _CatalogCursor(self.existing)
        return _CatalogCursor(None)


class _CatalogStore:
    def __init__(self, existing):
        self.connection_value = _CatalogConnection(existing)

    @contextmanager
    def connection(self):
        yield self.connection_value


def test_existing_postgres_lookup_schema_skips_startup_ddl():
    store = _CatalogStore(("lookup_sources", "lookup_rows", "lookup_terms",
                           "lookup_rows_kind_id", "lookup_rows_source_row"))

    ReferenceLookup(store)

    assert len(store.connection_value.statements) == 1
    assert "to_regclass" in store.connection_value.statements[0]


def test_incomplete_postgres_lookup_schema_runs_creation_ddl():
    store = _CatalogStore(("lookup_sources", "lookup_rows", "lookup_terms", None, None))

    ReferenceLookup(store)

    assert len(store.connection_value.statements) == 6
    assert any("CREATE INDEX IF NOT EXISTS lookup_rows_kind_id" in statement
               for statement in store.connection_value.statements)


def test_import_and_search_preserve_ambiguity_provenance_and_manual_confirmation(tmp_path, lookup):
    archive, manifest = catalog(tmp_path, [
        item(2, site="SITE-1", pack="1"),
        item(3, site="SITE-2", pack="6"),
        po(2),
    ])
    result = lookup.import_archive(archive, manifest, batch_size=100)

    assert result["source"]["counts"] == {"item": 2, "po": 1, "total": 3}
    assert lookup.summary()["approved_for_matching"] is False
    found = lookup.search("item", "تفاح عض", limit=10)
    assert len(found["records"]) == 2
    assert found["requires_confirmation"] is True
    assert all(row["approved_for_matching"] is False for row in found["records"])
    assert all("description_prefix" in row["match"]["basis"] for row in found["records"])
    assert {row["candidate_fields"]["pack"][0] for row in found["records"]} == {"1", "6"}
    first = found["records"][0]
    assert first["candidate_fields"]["sku"] == ["SKU-A"]
    assert first["candidate_fields"]["gtin"] == ["GTIN-100"]
    assert first["candidate_fields"]["internal_item"] == ["ITEM-100"]
    assert first["candidate_fields"]["gtin"] != first["candidate_fields"]["internal_item"]
    assert first["candidate_field_sources"]["description"][0]["column"] == "ITEM_DESC"
    assert first["source_hash"] == ITEM_HASH
    assert first["source_sheet"] == "Item Master"
    assert first["source_row"] in (2, 3)

    exact = lookup.search("item", "SKU-A")
    assert exact["records"][0]["match"]["score"] == 1.0
    assert "exact_identifier" in exact["records"][0]["match"]["basis"]


def test_literal_like_prefix_escapes_wildcards_for_both_database_dialects():
    assert _like_prefix(r"t:50%_off\code") == "t:50\\%\\_off\\\\code%"


def test_exact_filters_and_exact_po_context_join_do_not_fall_back(tmp_path, lookup):
    archive, manifest = catalog(tmp_path, [item(2, site="SITE-1"), item(3, site="SITE-2"), po(2)])
    lookup.import_archive(archive, manifest, batch_size=100)

    site = lookup.search("item", "تفاح", site="SITE-1")
    assert len(site["records"]) == 1
    assert site["records"][0]["candidate_fields"]["site"] == ["SITE-1"]
    assert "exact_site_context" in site["records"][0]["match"]["basis"]
    assert lookup.search("item", "تفاح", site="missing")["records"] == []

    supplier = lookup.search("item", "تفاح", supplier="Fresh Foods")
    assert len(supplier["records"]) == 2
    narrowed = lookup.search("item", "تفاح", po="PO-55")
    assert len(narrowed["records"]) == 2
    assert all("exact_po_context" in row["match"]["basis"] for row in narrowed["records"])
    assert lookup.search("item", "تفاح", po="PO-404")["records"] == []

    po_result = lookup.search("po", "ITEM-100", po="PO-55")
    assert len(po_result["records"]) == 1
    assert po_result["records"][0]["candidate_fields"]["po"] == ["PO-55", "EXT-55"]
    assert "LOCATION" not in [entry["column"] for entry in
                              po_result["records"][0]["candidate_field_sources"]["po"]]


def test_postgres_locale_prefix_and_po_identity_join_match_like_sqlite(
    tmp_path, postgres_lookup
):
    lookup, store = postgres_lookup
    archive, manifest = catalog(
        tmp_path,
        [
            item(2, description="Fresh Apple Red", site="SITE-1"),
            item(3, description="Blue Apricot", site="SITE-2"),
            po(2),
        ],
        name="postgres-prefix",
    )
    lookup.import_archive(archive, manifest, batch_size=100)

    prefix = lookup.search("item", "fresh app", limit=10)
    assert len(prefix["records"]) == 1
    assert prefix["records"][0]["candidate_fields"]["description"] == [
        "Fresh Apple Red"
    ]
    joined = lookup.search("item", "fresh app", po="PO-55")
    assert len(joined["records"]) == 1
    assert "exact_po_context" in joined["records"][0]["match"]["basis"]

    with store.connection() as connection:
        connection.execute("SET LOCAL enable_seqscan=off")
        plan = " ".join(
            row[0]
            for row in connection.execute(
                """EXPLAIN SELECT row_id FROM lookup_terms
                   WHERE kind='item' AND term LIKE ? ESCAPE '\\'""",
                (_like_prefix("t:app"),),
            )
        )
    assert "lookup_terms_pkey" in plan


def test_ranked_cursor_returns_every_ambiguous_candidate_once_and_is_query_bound(tmp_path, lookup):
    rows = [item(number, description="Organic Apple Red", sku=f"SKU-{number}") for number in range(2, 9)]
    archive, manifest = catalog(tmp_path, rows)
    lookup.import_archive(archive, manifest, batch_size=100)

    identifiers = []
    cursor = ""
    while True:
        page = lookup.search("item", "organic app", cursor=cursor, limit=2)
        identifiers.extend(row["id"] for row in page["records"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(identifiers) == len(set(identifiers)) == 7

    first = lookup.search("item", "organic app", limit=2)
    with pytest.raises(ValueError, match="stale page cursor"):
        lookup.search("item", "different query", cursor=first["next_cursor"], limit=2)


def test_failed_duplicate_or_count_validation_keeps_previous_lookup_atomic(tmp_path, lookup):
    archive, manifest = catalog(tmp_path, [item(2)])
    lookup.import_archive(archive, manifest, batch_size=100)
    before = lookup.summary()

    duplicate_archive, duplicate_manifest = catalog(tmp_path, [item(9), item(9)], name="duplicate")
    with pytest.raises(ValueError, match="repeats a source hash"):
        lookup.import_archive(duplicate_archive, duplicate_manifest, batch_size=100)
    assert lookup.summary()["counts"] == before["counts"]
    assert len(lookup.search("item", "SKU-A")["records"]) == 1

    wrong_archive, wrong_manifest = catalog(
        tmp_path, [item(12)], name="wrong-count", counts={"item": 2, "po": 0, "total": 2}
    )
    with pytest.raises(ValueError, match="counts do not match"):
        lookup.import_archive(wrong_archive, wrong_manifest, batch_size=100)
    assert lookup.summary()["counts"] == before["counts"]


def test_unicode_normalization_and_local_cli_import(tmp_path):
    assert normalize_name("  مُنتَج—كويتي  ") == "منتج كويتي"
    archive, manifest = catalog(tmp_path, [item(2)])
    data_dir = tmp_path / "cli-data"

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/import_reference_lookup.py"), str(archive),
         str(manifest), "--data-dir", str(data_dir), "--batch-size", "100"],
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["imported"] is True
    assert ReferenceLookup(Store(data_dir)).summary()["counts"]["total"] == 1
