"""One test per ULTA fine-grained rule ID. All rows below are synthetic."""

import io
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app import fine_rules as fr
from app.excel import HEADERS
from app.fine_rules_export import (EXCEPTIONS, POGRN_VALIDATION, RAW_INVOICE, TAX, WORKBENCH, review_workbook,
                                   target_workbook)
from app.matching import strip_ult as matching_strip_ult
from app.models import Invoice, Line


D = Decimal


def item(parent, barcode, vpn, site="22001", name="ABC001RA1KWD", desc="Glow Serum Rose 30ml", brand="BrandA", ref=1):
    return {"ITEM_PARENT": parent, "ITEM": barcode, "VPN": vpn, "SUPPLIER": site, "SUPPLIER_NAME": name,
            "SUPPLIER_COUNTRY": "Somewhere", "ITEM_DESC": desc, "ITEM_DESC_SECONDARY": "", "SHORT_DESC": "",
            "UDA_LV_1_VALUE": brand, "BRAND": "NotTheBrand", "_ref": f"Items!{ref}"}


def pogrn(order, location, qty, cost, ebs="ABC001", currency="KWD", ref=1, barcode="0012345678905"):
    return {"EBS_SUPPLIER_CODE": ebs, "LOCATION": location, "RMS_ORDER_NO": order, "BARCODE": barcode,
            "QTY_RECEIVED": qty, "UNIT_COST": "", "TOTAL COST": cost, "CURRENCY_CODE": currency,
            "RECEIPT_DATE": "2026-01-10 00:00:00", "SUP_NAME": "ABC", "_ref": f"POGRN!{ref}"}


ITEMS = [
    item("345000001", "ULT_0012345678905", "100001", ref=2),
    item("345000002", "ULT_0098765432109", "100002", desc="Matte Lipstick Red 4g", ref=3),
    item("345000003", "ULT_5550001112223", "100001", site="22002", name="XYZ002RB2SAR",
         desc="Other Serum", brand="BrandX", ref=4),
]
POGRN = [
    pogrn("13000001", "38091", "3", "30", ref=2),
    pogrn("13000001", "38091", "2", "40", ref=3, barcode="0098765432109"),
    pogrn("13000001", "38091", None, None, ref=4),
    pogrn("13000002", "800901", "99", "990", ref=5),
]
CONFIG = {"location_master": {"38091": {"type": fr.STORE, "market": "Kuwait"},
                              "800901": {"type": fr.WAREHOUSE, "market": "Kuwait"}},
          "supplier_site_currency": [{"supplier_site": "22001", "market": "Kuwait", "currency": "KWD"}],
          "version": "test-1"}


def lines(**overrides):
    first = dict(gtin="0012345678905", sku="100001", description="Glow Serum Rose 30 ml", qty=D(3), price=D(10),
                 net_amount=D(30), page=1)
    second = dict(gtin="0098765432109", sku="100002", description="Matte Lipstick Red 4g", qty=D(2), price=D(20),
                  net_amount=D(40), page=1)
    return [Line(**{**first, **overrides.get("first", {})}), Line(**{**second, **overrides.get("second", {})})]


def invoice(**changes):
    data = dict(number="INV-1", supplier_name="ABC Trading LLC", buyer_name="Ulta Buyer Co", date="2026-01-15",
                date_printed="15/01/2026", currency="KWD", taxCode="VAT0", net=D(70), tax=D(0), lines=lines())
    data.update(changes)
    return Invoice(**data)


def run(inv=None, items=ITEMS, rows=POGRN, config=CONFIG, **kwargs):
    return fr.run_invoice(inv or invoice(), fr.RowsSource(items, rows), fr.RulesConfig.from_dict(config), **kwargs)


def types(result, rule=None):
    return [e["Exception Type"] for e in result["exceptions"] if rule is None or e["Rule ID"] == rule]


def test_happy_path_is_approved():
    result = run()
    assert result["status"] == "Approved", result["exceptions"]
    assert result["header"]["Order No"] == "13000001"
    assert [l["Item"] for l in result["lines"]] == ["345000001", "345000002"]


# --------------------------------------------------------------------------- 01_Rulebook


def test_R_001_raw_capture_unchanged_and_unreadable_invoice_blocked():
    inv = invoice(lines=lines(first={"description": "  100001 Glow  SERUM, 30ml  "}))
    raw = run(inv, filename="a.pdf")["raw_invoice"]
    assert raw[0]["Description Raw"] == inv.lines[0].description and raw[0]["Invoice File"] == "a.pdf"
    empty = run(invoice(lines=[]))
    assert "Unreadable Invoice" in types(empty) and empty["status"] == "Blocked"


def test_R_002_leading_number_is_vpn_and_several_numbers_need_unique_master_match():
    leading = run(invoice(lines=lines(first={"gtin": None, "sku": None, "description": "100001 Glow Serum Rose"})))
    assert leading["lines"][0]["Item"] == "345000001"
    unique_secondary = run(invoice(lines=lines(first={"gtin": None, "sku": None,
                                                      "description": "Glow Serum ref 100001 lot 999999"})))
    assert unique_secondary["lines"][0]["Item"] == "345000001"
    several = run(invoice(lines=lines(first={"gtin": None, "sku": None,
                                             "description": "Glow Serum 100001 or 100002"})))
    assert several["lines"][0]["Item"] == "" and "Item Exception" in types(several, "R-002")


def test_R_003_six_digit_number_with_no_or_many_master_rows_is_item_exception():
    none = run(invoice(lines=lines(first={"gtin": None, "sku": None, "description": "777777 Unknown thing"})))
    assert none["lines"][0]["Item"] == "" and "Item Exception" in types(none, "R-003")
    many = run(invoice(supplier_name=None, lines=lines(first={"gtin": None, "sku": "100001"})), items=ITEMS[::2])
    assert many["lines"][0]["Item"] == "" and "Item Exception" in types(many)


def test_R_004_strip_only_ult_prefix_and_keep_leading_zeros():
    for strip in (fr.strip_ult, matching_strip_ult):
        assert strip("ULT_0012345678905") == "0012345678905"
        assert strip("ult_00123") == "00123"
        assert strip("ULT_ARCA01") == "ARCA01"
        assert strip("XULT_123") == "XULT_123"
        assert strip("0012") == "0012"


def test_R_005_route_order_barcode_vpn_item_parent_then_description_only_as_review():
    assert run()["lines"][0]["Match Method"] == "Barcode exact"
    vpn = run(invoice(lines=lines(first={"gtin": None})))
    assert vpn["lines"][0]["Match Method"] == "VPN exact"
    parent = run(invoice(lines=lines(first={"gtin": None, "sku": None, "item_id": "345000001",
                                            "description": "Glow"})))
    assert parent["lines"][0]["Match Method"] == "ITEM_PARENT exact" and parent["lines"][0]["Item"] == "345000001"
    fuzzy = run(invoice(lines=lines(first={"gtin": None, "sku": None, "description": "Glow Serum Rose"})))
    assert fuzzy["lines"][0]["Match Method"] == "Description candidate"
    assert fuzzy["lines"][0]["Item"] == "" and fuzzy["status"] != "Approved"


def test_R_006_supplier_name_to_item_master_and_several_sites_is_exception():
    by_name = run(invoice(supplier_name="ABC001RA1KWD", lines=lines(first={"gtin": "1"}, second={"gtin": "2"})))
    assert by_name["header"]["Supplier Site"] == "22001"
    twin = ITEMS + [item("345000009", "ULT_1112223334445", "100009", site="22009", ref=9)]
    several = run(invoice(supplier_name="ABC001RA1KWD", lines=[]), items=twin)
    assert several["header"]["Supplier Site"] == "" and "Supplier Exception" in types(several, "R-006")


def test_R_007_brand_from_uda_lv_1_value_never_brand_column():
    result = run()
    assert result["lines"][0]["Brand"] == "BrandA"
    no_uda = [{**r, "UDA_LV_1_VALUE": ""} for r in ITEMS]
    blank = run(items=no_uda)
    assert blank["lines"][0]["Brand"] == "" and "Data Quality" in types(blank, "ALG-022")


def test_R_008_suffix_is_mapped_through_entity_table_only():
    hint = fr.entity_hint("ABC001RA1KWD")
    assert hint["entity"] == "RA1" and hint["currency_hint"] == "KWD" and hint["mapped"]["market"] == "Kuwait"
    other = {**CONFIG, "location_master": {"38091": {"type": fr.STORE, "market": "UAE"}}}
    result = run(config=other)
    assert result["header"]["Market"] == "UAE" and "Market Mapping" in types(result)


def test_R_009_confirmed_entities_only_and_re2_is_unconfirmed():
    assert {k: v["market"] for k, v in fr.ENTITY_MAP.items()} == {"RA1": "Kuwait", "RB2": "KSA", "RA4": "UAE"}
    assert fr.entity_hint("ABC001RE2KWD")["mapped"] is None
    re2 = [{**r, "SUPPLIER_NAME": "ABC001RE2KWD"} for r in ITEMS]
    assert "Entity Hint" in types(run(items=re2), "R-009")


def test_R_010_location_master_wins_over_8000_prefix():
    config = fr.RulesConfig.from_dict({"location_master": {"800777": {"type": fr.STORE}}})
    result = fr.location_type("800777", config)
    assert result["type"] == fr.STORE and result["conflict"] is True


def test_R_011_location_from_pogrn_and_missing_master_market_is_exception():
    result = run(config={**CONFIG, "location_master": {}})
    assert result["header"]["Location"] == "" and result["header"]["Market"] == ""
    validation = [g for g in result["pogrn_validation"] if g["POGRN RMS Order No"] == "13000001"][0]
    assert validation["POGRN Location ID"] == "38091" and validation["Market Check"] == "Fail"


def test_R_012_currency_only_from_supplier_site_market_map():
    result = run(config={**CONFIG, "supplier_site_currency": []})
    assert result["header"]["Currency"] == "" and "Currency Mapping" in types(result, "R-012")
    assert run()["header"]["Currency"] == "KWD"


def test_R_013_cross_border_market_from_receiving_location_not_supplier():
    saudi = [{**r, "SUPPLIER_NAME": "ABC001RB2SAR"} for r in ITEMS]
    result = run(items=saudi, config={**CONFIG, "supplier_site_currency": []})
    assert result["header"]["Market"] == "Kuwait" and "Cross-border" in types(result)


def test_R_014_usd_invoice_goes_to_manual_review():
    run_state = fr.Run(invoice(currency="USD"), fr.RulesConfig.from_dict(CONFIG), "")
    assert fr.resolve_currency(run_state, "22001", "Kuwait", None) is None
    assert [e["Exception Type"] for e in run_state.exceptions] == ["USD Review"]


def test_R_015_auto_match_only_when_supplier_and_identifier_give_one_record():
    unconstrained = run(invoice(supplier_name=None, lines=lines(first={"gtin": None})[:1]))
    assert unconstrained["lines"][0]["Item"] == "" and "Item Exception" in types(unconstrained, "ALG-018")
    constrained = run(invoice(supplier_name="ABC001RA1KWD", lines=lines(first={"gtin": None})[:1]))
    assert constrained["lines"][0]["Item"] == "345000001"


def test_R_016_target_tabs_only_for_approved_invoices():
    with pytest.raises(ValueError):
        target_workbook([run(config={})])
    book = load_workbook(io.BytesIO(target_workbook([run()])))
    assert book.sheetnames == list(HEADERS)
    for name, columns in HEADERS.items():
        assert [c.value for c in book[name][1]] == columns


def test_R_017_feedback_is_proposed_until_a_named_approver_decides():
    log = fr.feedback_entry([], "INV-1/1", "345000001", "345000002", "wrong shade", "photo of label")
    assert log[0]["Decision"] == "Proposed" and log[0]["Approver"] == ""
    with pytest.raises(ValueError):
        fr.feedback_entry(log, "INV-1/1", "a", "b", "c", "")
    with pytest.raises(ValueError):
        fr.decide_feedback(log, "FB-0001", "", "Approved")
    decided = fr.decide_feedback(log, "FB-0001", "Item steward", "Approved", "rules-2")
    assert decided[0]["Implemented Version"] == "rules-2"
    with pytest.raises(ValueError):
        fr.decide_feedback(decided, "FB-0001", "Item steward", "Rejected")


def test_R_018_every_populated_output_field_has_lineage():
    result = run()
    traced = {(x["target"], x["line"]) for x in result["lineage"]}
    for name in fr.MANDATORY_HEADER:
        assert (name, None) in traced
    for n, line in enumerate(result["lines"], 1):
        for name in ("Item", "UPC", "Unit Cost", "Quantity", "Brand", "Unit Tax Code"):
            assert line[name] and (name, n) in traced
    order = next(x for x in result["lineage"] if x["target"] == "Order No")
    assert order["rule"] and order["source"] and "POGRN!2" in order["reference"]


def test_R_019_ebs_key_is_first_six_characters_of_supplier_name():
    assert fr.ebs_key("ABC001RA1KWD") == "ABC001" and fr.ebs_key("ABCD") is None
    short = run(items=[{**r, "SUPPLIER_NAME": "ABCD"} for r in ITEMS])
    assert "Supplier Exception" in types(short, "R-019")


def test_R_020_no_pogrn_rows_for_ebs_code_is_missing_po():
    result = run(rows=[pogrn("1", "38091", "5", "70", ebs="ZZZ999")])
    assert result["header"]["Order No"] == "" and "Missing PO" in types(result, "R-020")


def test_R_021_quantity_mismatch_is_never_auto_approved():
    result = run(rows=[pogrn("13000001", "38091", "4", "70")])
    assert result["header"]["Order No"] == "" and result["pogrn_validation"][0]["Qty Match"] == "Fail"


def test_R_022_value_mismatch_is_never_auto_approved():
    result = run(rows=[pogrn("13000001", "38091", "5", "71")])
    assert result["header"]["Order No"] == "" and result["pogrn_validation"][0]["Pre-Tax Value Match"] == "Fail"


def test_R_023_several_passing_orders_is_ambiguous_po():
    result = run(rows=[pogrn("13000001", "38091", "5", "70"), pogrn("13000009", "38091", "5", "70")])
    assert result["header"]["Order No"] == "" and "Ambiguous PO" in types(result, "R-030")


def test_R_024_invoice_po_is_never_overwritten_by_derived_po():
    result = run(invoice(po="99999999"))
    assert result["header"]["Order No"] == "99999999" and "PO Conflict" in types(result, "R-024")
    assert result["status"] != "Approved"


# --------------------------------------------------------------------------- owner POGRN rules


def test_R_025_location_is_exact_order_location_and_never_combined():
    split = [pogrn("13000001", "38091", "3", "30"), pogrn("13000001", "38092", "2", "40")]
    result = run(rows=split)
    assert result["header"]["Location"] == ""
    assert all("several locations" in g["Exception Reason"] for g in result["pogrn_validation"])
    blank = run(rows=[pogrn("13000001", "", "5", "70")])
    assert "Location ID blank" in blank["pogrn_validation"][0]["Exception Reason"]


def test_R_026_location_type_prefix_master_override_and_mismatch():
    assert fr.location_type("800901")["type"] == fr.WAREHOUSE
    assert fr.location_type("38091")["type"] == fr.STORE
    assert fr.location_type("38126")["type"] is None
    config = fr.RulesConfig.from_dict({"location_master": {"38091": {"type": fr.WAREHOUSE}}})
    assert fr.location_type("38091", config)["conflict"] is True
    result = run(config={**CONFIG, "location_master": {"38091": {"type": fr.WAREHOUSE, "market": "Kuwait"}}})
    assert result["header"]["Order No"] == "" and "disagrees with prefix" in result["pogrn_validation"][0]["Exception Reason"]


def test_R_027_market_only_from_location_mapping():
    config = fr.RulesConfig.from_dict({"location_market": {"38091": "Kuwait"},
                                       "location_master": {"38092": {"market": "UAE"}}})
    assert fr.location_market("38091", config) == ("Kuwait", 1)
    assert fr.location_market("38099", config) == (None, 0)
    clash = fr.RulesConfig.from_dict({"location_market": {"38091": "Kuwait"},
                                      "location_master": {"38091": {"market": "UAE"}}})
    assert fr.location_market("38091", clash) == (None, 2)


def test_R_028_quantity_received_aggregated_per_order_location_with_tolerance():
    group = [g for g in run()["pogrn_validation"] if g["POGRN RMS Order No"] == "13000001"][0]
    assert group["Aggregated QTY_RECEIVED"] == D(5) and group["Quantity Variance"] == D(0)
    loose = run(rows=[pogrn("13000001", "38091", "4", "70")], config={**CONFIG, "qty_tolerance": "1"})
    assert loose["header"]["Order No"] == "13000001"


def test_R_029_currency_checked_before_pre_tax_value():
    result = run(rows=[pogrn("13000001", "38091", "5", "70", currency="AED")])
    group = result["pogrn_validation"][0]
    assert group["Value Result"] == "Fail" and "Currency differs" in group["Exception Reason"]
    assert group["Aggregated TOTAL COST"] == D(70)


def test_R_030_unique_fully_passing_order_is_derived_else_candidate_shown_for_review():
    result = run()
    assert result["header"]["Order No"] == "13000001" and result["po"]["source"] == fr.DERIVED_FROM_POGRN
    unmapped = run(config={**CONFIG, "location_master": {}})
    missing = [e for e in unmapped["exceptions"] if e["Rule ID"] == "R-030"][0]
    assert "13000001@38091" in missing["Candidates / Evidence"] and "V-007" in missing["Proposed Resolution"]


# --------------------------------------------------------------------------- 01A_Algorithm_Rules


def test_ALG_001_every_page_scanned_and_missing_page_flagged():
    boxes = [{"text": "Invoice", "page": 1, "box": [0, 10, 5, 12]}, {"text": "Total", "page": 3, "box": [0, 5, 5, 7]}]
    scan = fr.scan_pages("", boxes, page_count=3)
    assert scan["readable_pages"] == [1, 3] and scan["unreadable_pages"] == [2]
    assert "OCR Review" in types(run(boxes=boxes, page_count=3), "ALG-001")
    assert fr.scan_pages("one\fTwo")["page_count"] == 2


def test_ALG_002_supplier_searched_on_every_page_including_footer():
    scan = fr.scan_pages("Bill To: Ulta Buyer Co\nItems\f\nThanks\nGlow Beauty Trading LLC")
    names = fr.supplier_candidates(scan, buyer_name="Ulta Buyer Co")
    assert names[0]["name"] == "Glow Beauty Trading LLC" and names[0]["page"] == 2 and names[0]["region"] == "bottom"
    assert all("Ulta" not in c["name"] for c in names)
    nothing = run(invoice(supplier_name=None, lines=[]))
    assert "Supplier Exception" in types(nothing, "ALG-002")


def test_ALG_003_document_unchanged_blank_or_duplicate_is_header_exception():
    assert run()["header"]["Document"] == "INV-1"
    assert "Header Exception" in types(run(invoice(number=None)), "ALG-003")
    results = fr.run_batch([{"invoice": invoice()}, {"invoice": invoice()}], fr.RowsSource(ITEMS, POGRN),
                           fr.RulesConfig.from_dict(CONFIG))
    assert "Header Exception" not in types(results[0]) and "Header Exception" in types(results[1], "ALG-003")


def test_ALG_004_raw_and_parsed_date_kept_and_ambiguous_date_reviewed():
    assert fr.parse_printed_date("15/01/2026", "2026-01-15") == {"raw": "15/01/2026", "parsed": "2026-01-15",
                                                                 "ambiguous": False}
    ambiguous = run(invoice(date_printed="03/04/2026", date="2026-04-03"))
    assert ambiguous["header"]["Document Date"] == "" and "Date Review" in types(ambiguous, "ALG-004")
    assert fr.parse_printed_date("04/04/2026", "2026-04-04")["ambiguous"] is False


def test_ALG_005_supplier_site_is_item_master_supplier_as_text():
    result = run()
    assert result["header"]["Supplier Site"] == "22001"
    site = next(x for x in result["lineage"] if x["target"] == "Supplier Site")
    assert site["rule"] == "ALG-005" and "Items!2" in site["reference"]


def test_ALG_006_pogrn_searched_by_six_character_ebs_key():
    result = run()
    assert result["ebs_supplier_code"] == "ABC001"
    assert {g["EBS Supplier Code 6D"] for g in result["pogrn_validation"]} == {"ABC001"}


def test_ALG_007_suffix_is_only_a_hint():
    other = {**CONFIG, "location_master": {"38091": {"type": fr.STORE, "market": "UAE"}},
             "supplier_site_currency": [{"supplier_site": "22001", "market": "UAE", "currency": "KWD"}]}
    result = run(config=other)
    assert result["header"]["Market"] == "UAE" and result["entity_hint"]["mapped"]["market"] == "Kuwait"


def test_ALG_008_printed_po_validated_against_pogrn_before_use():
    result = run(invoice(po="13000001"))
    assert result["header"]["Order No"] == "13000001" and result["po"]["source"] == fr.FROM_INVOICE
    assert result["status"] == "Approved"


def test_ALG_009_missing_po_recovered_by_quantity_and_value():
    result = run()
    assert result["po"]["status"] == "Approved"
    assert [g["Validation Status"] for g in result["pogrn_validation"]] == ["Pass", "Fail"]


def test_ALG_010_derived_order_written_with_source_only_when_unique():
    result = run()
    assert result["workbench"][0]["PO Source"] == fr.DERIVED_FROM_POGRN
    assert next(x for x in result["lineage"] if x["target"] == "Order No")["confidence"] == fr.DERIVED_FROM_POGRN
    two = run(rows=[pogrn("13000001", "38091", "5", "70"), pogrn("13000009", "38091", "5", "70")])
    assert two["header"]["Order No"] == "" and two["workbench"][0]["PO Source"] == ""


def test_ALG_011_location_from_accepted_order_never_item_master_or_invoice():
    with_loc = [{**r, "LOCATION": "38091"} for r in ITEMS]
    result = run(invoice(location="38091"), items=with_loc, rows=[])
    assert result["header"]["Location"] == "" and "Location ID" in types(result, "ALG-011")


def test_ALG_012_location_800_is_warehouse():
    assert fr.location_type("800901") == {"type": fr.WAREHOUSE, "source": "prefix fallback",
                                          "prefix_type": fr.WAREHOUSE, "conflict": False}


def test_ALG_013_location_380_is_store():
    assert fr.location_type("38091")["type"] == fr.STORE and fr.location_type("381")["type"] is None


def test_ALG_014_master_overrides_prefix_and_keeps_prefix_for_comparison():
    config = fr.RulesConfig.from_dict({"location_master": {"38091": {"type": fr.STORE}, "12345": {"type": fr.WAREHOUSE}}})
    assert fr.location_type("12345", config) == {"type": fr.WAREHOUSE, "source": "location master",
                                                 "prefix_type": None, "conflict": False}
    with pytest.raises(ValueError):
        fr.RulesConfig.from_dict({"location_master": {"1": {"type": "Depot"}}})


def test_ALG_015_barcode_column_first_then_description_numbers_normalized():
    line = Line(gtin="0012 3456-78905", description="Serum EAN 0098-7654-32109 size 30")
    assert [c["value"] for c in fr.barcode_candidates(line)] == ["0012345678905", "0098765432109"]
    assert fr.normalize_barcode("00 12-34") == "001234"


def test_ALG_016_barcode_matches_item_without_ult_and_many_parents_is_exception():
    assert run()["workbench"][0]["Rule ID"] == "ALG-016"
    twin = ITEMS + [item("345000010", "ULT_0012345678905", "100010", ref=10)]
    result = run(items=twin)
    assert result["lines"][0]["Item"] == "" and "Item Exception" in types(result, "ALG-016")
    no_barcode = run(invoice(lines=lines(first={"gtin": "9999999999999"})))
    assert no_barcode["lines"][0]["Barcode Check"] == "Fail"


def test_ALG_017_vpn_column_first_else_leading_six_digits():
    assert fr.vpn_candidates(Line(sku="ABC-1", description="100001 x"))[0]["value"] == "ABC-1"
    assert fr.vpn_candidates(Line(description="100001 Serum"))[0] == {"value": "100001", "origin": "description start",
                                                                      "primary": True}
    assert all(not c["primary"] for c in fr.vpn_candidates(Line(description="1000011 Serum 100002")))


def test_ALG_018_vpn_exact_constrained_by_supplier():
    result = run(invoice(lines=lines(first={"gtin": None})))
    assert result["lines"][0]["Item"] == "345000001" and result["workbench"][0]["Rule ID"] == "ALG-018"


def test_ALG_019_description_normalization_keeps_size_and_shade():
    assert fr.normalize_description("100001 Glow SERUM, 30 ML Rosé #2", remove=["100001"]) == "glow serum 30ml rose #2"


def test_ALG_020_triple_assurance_conflict_and_description_never_overrides():
    conflict = run(invoice(lines=lines(first={"sku": "100002"})))
    assert conflict["lines"][0]["Item"] == "" and "Item Conflict" in types(conflict, "ALG-020")
    weak = run(invoice(lines=lines(first={"description": "Totally different words"})))
    assert weak["lines"][0]["Item"] == "345000001" and weak["lines"][0]["Description Check"] == "Weak"
    assert weak["status"] == "Approved"


def test_ALG_021_item_column_is_item_parent_never_ult_value():
    result = run()
    assert result["lines"][0]["Item"] == "345000001"
    assert not any(str(v).upper().startswith("ULT_") for l in result["lines"] for v in l.values())


def test_ALG_022_brand_missing_or_not_unique_is_a_warning_only():
    mixed = ITEMS + [{**ITEMS[0], "UDA_LV_1_VALUE": "BrandB", "ITEM": "ULT_0012345678906", "_ref": "Items!11"}]
    result = run(items=mixed)
    assert result["lines"][0]["Brand"] == "" and "Data Quality" in types(result, "ALG-022")
    assert result["status"] == "Approved"


def test_ALG_023_currency_from_map_not_supplier_suffix():
    no_map = run(config={**CONFIG, "supplier_site_currency": []})
    assert no_map["entity_hint"]["currency_hint"] == "KWD" and no_map["header"]["Currency"] == ""


def test_ALG_024_usd_only_with_explicit_supplier_site_exception():
    usd = {**CONFIG, "supplier_site_currency": [{"supplier_site": "22001", "market": "Kuwait", "currency": "USD"}]}
    rows = [pogrn("13000001", "38091", "5", "70", currency="USD")]
    blocked = run(invoice(currency="USD"), rows=rows, config=usd)
    assert blocked["header"]["Currency"] == "" and "USD Review" in types(blocked, "ALG-024")
    allowed = run(invoice(currency="USD"), rows=rows, config={**usd, "usd_exceptions": ["22001"]})
    assert allowed["header"]["Currency"] == "USD"


def test_ALG_025_cross_border_pair_accepted_only_when_configured():
    saudi = [{**r, "SUPPLIER_NAME": "ABC001RB2SAR"} for r in ITEMS]
    result = run(items=saudi)
    assert result["header"]["Market"] == "Kuwait" and result["header"]["Currency"] == "KWD"


def test_ALG_026_line_exceptions_do_not_block_other_lines():
    result = run(invoice(lines=lines(second={"price": None, "net_amount": None})))
    assert [l["Validation Status"] for l in result["lines"]] == ["Matched", "Exception"]
    assert result["status"] == "Review"


def test_ALG_027_upc_is_normalized_invoice_barcode_as_text():
    book = load_workbook(io.BytesIO(target_workbook([run()])))
    upc = book["Details"].cell(2, 3)
    assert upc.value == "0012345678905" and upc.data_type == "s"


def test_ALG_028_one_failing_invoice_does_not_stop_the_batch():
    class Broken(fr.RowsSource):
        def items_by_barcode(self, value):
            if value == "0000000000000":
                raise RuntimeError("synthetic failure")
            return super().items_by_barcode(value)

    entries = [{"invoice": invoice(number="BAD", lines=lines(first={"gtin": "0000000000000"}))},
               {"invoice": invoice(number="GOOD")}]
    results = fr.run_batch(entries, Broken(ITEMS, POGRN), fr.RulesConfig.from_dict(CONFIG))
    assert [r["status"] for r in results] == ["Blocked", "Approved"]
    assert results[0]["exceptions"][0]["Rule ID"] == "ALG-028"


def test_ALG_029_approval_needs_one_unique_item_and_one_unique_po():
    twin = ITEMS + [item("345000010", "ULT_0012345678905", "100010", ref=10)]
    assert run(items=twin)["status"] == "Review"
    two = [pogrn("13000001", "38091", "5", "70"), pogrn("13000009", "38091", "5", "70")]
    assert run(rows=two)["status"] == "Review"


def test_ALG_030_nothing_invented_when_sources_are_empty():
    result = run(items=[], rows=[], config={})
    header = result["header"]
    for name in ("Supplier Site", "Order No", "Location", "Location Type", "Currency", "Market"):
        assert header[name] == ""
    assert all(l["Item"] == "" and l["Brand"] == "" for l in result["lines"])
    assert result["lines"][0]["UPC"] == "0012345678905" and result["status"] == "Review"


# --------------------------------------------------------------------------- 02A_Target_Mapping


TARGET_02A = {
    "Document": ("header", "INV-1", "ALG-003"),
    "Supplier Site": ("header", "22001", "ALG-005"),
    "Order No": ("header", "13000001", "ALG-010"),
    "Location": ("header", "38091", "ALG-011"),
    "Location Type": ("header", fr.STORE, "ALG-014"),
    "Document Date": ("header", "2026-01-15", "ALG-004"),
    "Item": ("line", "345000001", "ALG-021"),
    "UPC": ("line", "0012345678905", "ALG-027"),
    "Unit Cost": ("line", D(10), "ALG-026"),
    "Quantity": ("line", D(3), "ALG-026"),
    "Brand": ("line", "BrandA", "ALG-022"),
    "Currency": ("header", "KWD", "ALG-023"),
}


@pytest.mark.parametrize("column", list(TARGET_02A), ids=[f"02A-{c}" for c in TARGET_02A])
def test_02A_target_mapping(column):
    where, expected, rule = TARGET_02A[column]
    result = run()
    actual = result["header"][column] if where == "header" else result["lines"][0][column]
    assert actual == expected
    line = None if where == "header" else 1
    assert any(x["target"] == column and x["line"] == line and x["rule"] == rule for x in result["lineage"])


def test_02A_unit_cost_line_value_reconciliation():
    result = run(invoice(lines=lines(first={"net_amount": D(31)})))
    assert "Line Exception" in types(result, "02A-Unit Cost") and result["lines"][0]["Validation Status"] == "Exception"


# --------------------------------------------------------------------------- working sheets / reference sheets


def test_working_sheets_have_exact_rulebook_headers():
    results = [run(), run(invoice(number="INV-2"), config={})]
    log = fr.feedback_entry([], "INV-2/1", "", "345000001", "steward", "label photo")
    book = load_workbook(io.BytesIO(review_workbook(results, log)))
    expected = {"06_Raw_Invoice": RAW_INVOICE, "07_Match_Workbench": WORKBENCH,
                "06A_POGRN_Validation": POGRN_VALIDATION, "08_Exceptions": EXCEPTIONS,
                "09_Output_Header": fr.TARGET_HEADER, "10_Output_Lines": fr.TARGET_LINE, "11_Output_Tax": TAX,
                "12_Feedback_Log": fr.FEEDBACK_COLUMNS}
    for name, columns in expected.items():
        assert [c.value for c in book[name][1]] == columns
    assert book["09_Output_Header"].max_row == 3 and book["12_Feedback_Log"]["J2"].value == "Proposed"
    assert book["10_Output_Lines"]["C2"].data_type == "s"


def test_review_workbook_writes_formula_like_text_as_text():
    result = run(invoice(lines=lines(first={"description": "=HYPERLINK(\"x\")"})))
    book = load_workbook(io.BytesIO(review_workbook([result])))
    cell = book["06_Raw_Invoice"]["E2"]
    assert cell.value == "=HYPERLINK(\"x\")" and cell.data_type == "s"


def test_config_rejects_unapproved_currency_rows_and_negative_tolerance():
    with pytest.raises(ValueError):
        fr.RulesConfig.from_dict({"supplier_site_currency": [{"supplier_site": "1", "market": "K", "currency": "kd"}]})
    with pytest.raises(ValueError):
        fr.RulesConfig.from_dict({"supplier_site_currency": [
            {"supplier_site": "1", "market": "K", "currency": "KWD"},
            {"supplier_site": "1", "market": "K", "currency": "USD"}]})
    with pytest.raises(ValueError):
        fr.RulesConfig.from_dict({"qty_tolerance": "-1"})
