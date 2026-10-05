import json
import hashlib

import pytest

from app.product_candidates import ProductCandidates
from app.reference_lookup import ReferenceLookup
from app.store import Store
from test_reference_lookup import catalog, item, po


def product(row, name, sku, *, site="SITE-1"):
    record = item(row, description=name, sku=sku, site=site)
    record["data"].update(ITEM_PARENT=sku, ITEM=f"GTIN-{sku}")
    return record


def order(row, sku, price, qty, *, currency="AED", site="SITE-1"):
    record = po(row)
    record["data"].update(RMS_ITEM_ID=sku, BARCODE=f"GTIN-{sku}", UNIT_COST=price,
                          QTY_ORDERED=qty, QTY_RECEIVED=qty, CURRENCY_CODE=currency,
                          LOCATION=site)
    return record


def loaded(tmp_path, rows):
    store = Store(tmp_path / "data")
    lookup = ReferenceLookup(store)
    lookup.import_archive(*catalog(tmp_path, rows), batch_size=100)
    return ProductCandidates(lookup), store


def test_typo_and_reordered_product_names_preserve_candidates_not_approvals(tmp_path):
    search, store = loaded(tmp_path, [
        product(2, "Citrus Fresh Soap 100 g", "SOAP-100"),
        product(3, "Citrus Fresh Soap 125 g", "SOAP-125"),
        product(4, "Blue travel hair dryer", "DRYER"),
    ])
    result = search.search("Fresh citurs soap")
    assert {r["candidate_fields"]["sku"][0] for r in result["records"][:2]} == {"SOAP-100", "SOAP-125"}
    assert result["requires_confirmation"] and not result["approved_for_matching"]
    assert all(not r["approved_for_matching"] for r in result["records"])
    flagged = next(r for r in result["records"] if r["source_row"] == 3)
    assert flagged["flags"] == ["conflicting_site_sku"]
    assert flagged["source_sheet"] == "Item Master"
    assert store.jobs() == []


def test_price_and_quantity_clues_rank_near_names_without_changing_values(tmp_path):
    search, store = loaded(tmp_path, [
        product(2, "Citrus Fresh Soap 100 g", "SOAP-100"),
        product(3, "Citrus Fresh Soap 125 g", "SOAP-125"),
        order(2, "SOAP-100", "30", "48"), order(3, "SOAP-125", "10", "12"),
    ])
    before = store.path.read_bytes()
    result = search.search("Citrus fresh soap", invoice_po="PO-55", price="10", qty="12", currency="AED", uom="EA")
    best = result["records"][0]
    assert best["candidate_fields"]["sku"] == ["SOAP-125"]
    assert {"po_price_similarity", "po_quantity_similarity"} <= set(best["match"]["basis"])
    clue = best["match"]["clues"][0]
    assert clue["price_similarity"] == 1 and clue["quantity_similarity"] == 1
    assert clue["unit_price"] == "10" and clue["ordered_qty"] == "12"
    assert clue["flags"] == ["repeated_po_item_group"]
    assert not clue["quantity_units_confirmed"]
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("currency,uom", [("KWD", "EA"), ("AED", "PACK"), ("", "EA"), ("AED", "")])
def test_price_not_scored_across_currency_or_unknown_unit_basis(tmp_path, currency, uom):
    search, _ = loaded(tmp_path, [product(2, "Citrus soap", "SOAP"), order(2, "SOAP", "10", "12")])
    best = search.search("Citrus soap", invoice_po="PO-55", price="10", currency=currency, uom=uom)["records"][0]
    assert "po_price_similarity" not in best["match"]["basis"]
    assert "price_similarity" not in best["match"]["clues"][0]


@pytest.mark.parametrize("reason", ["unsafe", "cross_site"])
def test_unsafe_or_conflicting_po_context_is_visible_but_never_scored(tmp_path, reason):
    context = order(2, "SOAP", "10", "12", site="SITE-9" if reason == "cross_site" else "SITE-1")
    if reason == "unsafe":
        context["flags"] = ["shifted_source_row"]
    search, _ = loaded(tmp_path, [product(2, "Citrus soap", "SOAP", site="SITE-1"), context])

    best = search.search(
        "Citrus soap", invoice_po="PO-55", price="10", qty="12",
        currency="AED", uom="EA",
    )["records"][0]

    clue = best["match"]["clues"][0]
    assert clue["scoring_eligible"] is False
    assert "not_scored_reason" in clue
    assert "price_similarity" not in clue and "quantity_similarity" not in clue
    assert "not scored" in clue["price_note"].lower()
    assert "not scored" in clue["quantity_note"].lower()
    assert not {"po_price_similarity", "po_quantity_similarity"}.intersection(best["match"]["basis"])


def test_explicit_filters_are_never_relaxed_but_bad_invoice_po_is_only_a_clue(tmp_path):
    search, _ = loaded(tmp_path, [product(2, "Citrus soap", "SOAP"), order(2, "SOAP", "10", "12")])
    assert not search.search("Citrus soap", site="MISSING")["records"]
    assert not search.search("Citrus soap", supplier="MISSING")["records"]
    assert not search.search("Citrus soap", po="MISSING")["records"]
    assert search.search("Citrus soap", invoice_po="MISSING", qty="12")["records"][0]["match"]["clues"] == []
    assert search.search("SOAP")["records"][0]["match"]["basis"] == ["exact_identifier"]


def test_missing_catalog_notice_and_cache_refresh(tmp_path):
    store = Store(tmp_path / "data")
    lookup = ReferenceLookup(store)
    search = ProductCandidates(lookup)
    assert "not loaded" in search.search("Citrus")["notice"]
    lookup.import_archive(*catalog(tmp_path, [product(2, "Citrus soap", "SOAP")]), batch_size=100)
    assert search.search("Citurs soap")["records"]


@pytest.mark.parametrize("clue", ["NaN", "Infinity", "not-a-number"])
def test_invalid_numeric_clue_fails_without_writes(tmp_path, clue):
    search, store = loaded(tmp_path, [product(2, "Citrus soap", "SOAP")])
    with pytest.raises(ValueError, match="finite numbers"):
        search.search("Citrus soap", price=clue)
    assert json.loads(json.dumps(store.jobs())) == []


@pytest.mark.parametrize("clue", ["1,2,3", "1e9", "1e999999999", "1000000000000000001"])
def test_malformed_or_unbounded_numeric_clue_is_rejected(tmp_path, clue):
    search, _ = loaded(tmp_path, [product(2, "Citrus soap", "SOAP")])
    with pytest.raises(ValueError, match="finite numbers"):
        search.search("Citrus soap", qty=clue)


def test_fuzzy_term_weighting_prevents_broad_category_from_crowding_out_product(
    tmp_path,
):
    row_ids = range(1, 702)
    target_row = max(
        row_ids,
        key=lambda row: hashlib.sha256(
            ("a" * 64 + "\x00Item Master\x00" + str(row)).encode()
        ).hexdigest(),
    )
    rows = []
    for row in row_ids:
        if row == target_row:
            record = product(row, "Citrus Fresh Soap 100 g", "TARGET-SOAP")
        else:
            record = product(row, f"Unrelated appliance model {row}", f"OTHER-{row}")
        record["data"]["DEPT_NAME"] = "Citrus Fresh Soap"
        rows.append(record)
    archive, manifest = catalog(tmp_path, rows, name="broad-category")
    body = json.loads(manifest.read_text())
    body["columns"]["item"]["description"].append("DEPT_NAME")
    manifest.write_text(json.dumps(body))
    store = Store(tmp_path / "broad-data")
    lookup = ReferenceLookup(store)
    lookup.import_archive(archive, manifest, batch_size=100)

    result = ProductCandidates(lookup).search("Citrus Frseh Soap 100 g")

    assert result["records"][0]["candidate_fields"]["sku"] == ["TARGET-SOAP"]
    assert result["records"][0]["match"]["basis"] == ["approximate_product_name"]
