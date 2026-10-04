import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.reference_lookup import ReferenceLookup, normalize_name
from app.store import Store


ROOT = Path(__file__).resolve().parents[1]
ITEM_HASH = "a" * 64
PO_HASH = "b" * 64
COLUMNS = {
    "item": {
        "description": ["ITEM_DESC", "SHORT_DESC"],
        "sku": ["VPN"],
        "gtin": ["ITEM"],
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
        "uom": [],
        "pack": [],
        "supplier": ["EBS_SUPPLIER_CODE", "SUP_NAME"],
        "site": ["EBS_SUPPLIER_CODE", "LOCATION"],
        "po": ["RMS_ORDER_NO", "EXT_ORDER_NO", "RMS_ITEM_ID", "LOCATION"],
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
            "ITEM": "ITEM-100",
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
    assert first["candidate_fields"]["gtin"] == ["ITEM-100"]
    assert first["candidate_fields"]["internal_item"] == ["ITEM-100"]
    assert first["candidate_field_sources"]["description"][0]["column"] == "ITEM_DESC"
    assert first["source_hash"] == ITEM_HASH
    assert first["source_sheet"] == "Item Master"
    assert first["source_row"] in (2, 3)

    exact = lookup.search("item", "SKU-A")
    assert exact["records"][0]["match"]["score"] == 1.0
    assert "exact_identifier" in exact["records"][0]["match"]["basis"]


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
