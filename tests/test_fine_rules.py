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


PARENT_OF = {"0012345678905": "345000001", "0098765432109": "345000002"}


def pogrn(order, location, qty, cost, ebs="ABC001", currency="KWD", ref=1, barcode="0012345678905", item=None):
    return {"EBS_SUPPLIER_CODE": ebs, "LOCATION": location, "RMS_ORDER_NO": order, "BARCODE": "ULT_" + barcode,
            "RMS_ITEM_ID": item or PARENT_OF.get(barcode, ""),
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
    pogrn("13000002", "800901", "99", "990", ref=5, ebs="XYZ002"),  # another 6-character code: never a candidate
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
    return [e["Engine Type"] for e in result["exceptions"] if rule is None or e["Rule ID"] == rule]


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


def test_R_002_only_the_leading_six_digit_number_is_a_vpn():
    leading = run(invoice(lines=lines(first={"gtin": None, "sku": None, "description": "100001 Glow Serum Rose"})))
    assert leading["lines"][0]["Item"] == "345000001" and leading["lines"][0]["Match Method"] == "VPN exact"
    secondary = run(invoice(lines=lines(first={"gtin": None, "sku": None,
                                               "description": "Glow Serum ref 100001 lot 999999"})))
    assert secondary["lines"][0]["Item"] == "" and secondary["status"] != "Approved"
    assert fr.vpn_candidates(Line(description="Glow Serum 100001 or 100002")) == []


def test_e71a_line_10_leading_vpn_resolves_after_barcode_and_vpn_column_fail():
    """Must-pass (owner answer, ITM-004): a line whose barcode is unknown and whose VPN column is empty resolves
    through the leading six-digit number of the description, constrained to the invoice supplier."""
    line = {"gtin": "4006000000001", "sku": None, "description": "100002 Matte Lipstick Red 4g 12pcs 2024"}
    result = run(invoice(lines=lines(second=line)))
    assert result["lines"][1]["Item"] == "345000002" and result["lines"][1]["Match Method"] == "VPN exact"
    other = ITEMS + [item("345000099", "ULT_7770001112223", "100002", site="22099", name="QRS001RA1KWD", ref=9)]
    assert run(invoice(lines=lines(second=line)), items=other)["lines"][1]["Item"] == "345000002"


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
    # ITM-006: an ITEM_PARENT printed on the invoice is only a review candidate, never a fill.
    assert parent["lines"][0]["Match Method"] == "ITEM_PARENT candidate" and parent["lines"][0]["Item"] == ""
    assert parent["status"] != "Approved"
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
    assert result["header"]["Location"] == "38091" and result["header"]["Market"] == ""
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
    assert [e["Engine Type"] for e in run_state.exceptions] == ["USD Review"]


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


def both(order, location, q1="3", c1="30", q2="2", c2="40", **kw):
    """One POGRN order/location carrying both synthetic invoice items."""
    return [pogrn(order, location, q1, c1, **kw), pogrn(order, location, q2, c2, barcode="0098765432109", **kw)]


def test_R_020_no_pogrn_rows_for_ebs_code_is_missing_po():
    result = run(rows=[pogrn("1", "38091", "5", "70", ebs="ZZZ999")])
    assert result["header"]["Order No"] == "" and "POGRN Supplier Exception" in types(result, "SUP-002")


def test_R_021_grn_quantity_difference_is_a_warning_on_the_invoice_figures():
    # Owner form 01a10c4d qty_cost_tolerance = invoice_flag: POG-006 and C-11 warn, they do not block.
    result = run(invoice(po="13000001"), rows=both("13000001", "38091", q2="1"))
    assert result["header"]["Order No"] == "13000001" and result["pogrn_validation"][0]["Qty Match"] == "Fail"
    assert result["pogrn_validation"][0]["Validation Status"] == "Pass"
    assert [line["Quantity"] for line in result["lines"]] == [D(3), D(2)]  # the invoice figures
    warnings = [e for e in result["exceptions"] if e["Engine Type"] in ("Quantity Mismatch", "Item Quantity Mismatch")]
    assert {e["Rule ID"] for e in warnings} == {"POG-006", "C-11"} and not any(e["blocking"] for e in warnings)
    # Still Review only through the owner's item-line check (as defined: lines whose quantity agrees).
    assert {e["Rule ID"] for e in result["exceptions"] if e["blocking"]} == {"ITEM-LINE-95"}


def test_R_022_value_mismatch_beyond_tolerance_is_never_auto_approved():
    result = run(invoice(po="13000001"), rows=both("13000001", "38091", c2="45"))
    assert result["header"]["Order No"] == "13000001" and result["pogrn_validation"][0]["Pre-Tax Value Match"] == "Fail"
    assert "Value Mismatch" in types(result, "POG-007") and result["status"] == "Review"


def test_POG_007_owner_tolerance_is_one_kwd_or_two_aed_at_two_decimals():
    within = run(rows=both("13000001", "38091", c2="40.9990000001"))
    assert within["pogrn_validation"][0]["Value Result"] == "Pass" and within["status"] == "Approved"
    assert "01a10c4f" in within["pogrn_validation"][0]["Value Tolerance Source"]
    aed = both("13000001", "38091", c2="41.99", currency="AED")
    assert run(invoice(currency="AED"), rows=aed)["pogrn_validation"][0]["Value Result"] == "Pass"
    assert run(rows=both("13000001", "38091", c2="41.01"))["pogrn_validation"][0]["Value Result"] == "Fail"
    usd = fr.RulesConfig.from_dict(CONFIG)
    assert fr._value_variance(usd, D("70"), D("70.004"), "USD") == (D("0.00"), True)
    assert fr._value_variance(usd, D("70"), D("70.01"), "USD")[1] is False


def test_R_023_several_orders_carrying_every_item_is_ambiguous_po():
    result = run(rows=both("13000001", "38091") + both("13000009", "38091"))
    assert result["header"]["Order No"] == "" and "Ambiguous PO" in types(result, "POG-001")
    assert result["po_candidates"] == 2 and result["header"]["Location"] == ""


def test_R_024_printed_po_is_kept_and_flagged_when_not_in_pogrn():
    result = run(invoice(po="99999999"))
    assert result["header"]["Order No"] == "99999999" and "Missing PO" in types(result, "POG-001")
    assert "25 Sep" in next(e["Candidates / Evidence"] for e in result["exceptions"] if e["Rule ID"] == "POG-001")
    assert result["status"] != "Approved" and result["header"]["Location"] == ""


def test_POG_001_printed_po_is_searched_first_and_needs_the_supplier_ebs_code():
    rows = POGRN + both("13000077", "800901", ebs="ZZZ999")
    result = run(invoice(po="13000001"), rows=rows)
    assert result["header"]["Order No"] == "13000001" and result["po"]["source"] == fr.FROM_INVOICE
    assert result["status"] == "Approved", result["exceptions"]
    # strict_6 (owner form 01a10c4d): a printed order of another 6-character EBS code is not filled.
    other = run(invoice(po="13000077"), rows=rows)
    assert other["header"]["Order No"] == "" and other["header"]["Location"] == ""
    assert "POGRN Supplier Exception" in types(other, "SUP-002") and other["status"] == "Review"


def test_strict_6_orders_come_only_from_the_ebs_code_never_from_items():
    # Decision 16: within the 6-character code, POG-001 picks the one order/location whose quantity and value agree.
    picked = run(rows=POGRN + both("13000003", "38091", q1="9"))
    assert picked["header"]["Order No"] == "13000001" and picked["po_candidates"] == 1
    trace = next(x for x in picked["lineage"] if x["target"] == "Order No")
    assert "decision 16" in trace["source"] and "POGRN!2" in trace["reference"]
    assert trace["evidence_kind"] == fr.EVIDENCE_SELECTED and "among 2 order/location candidates" in trace["source"]
    printed = run(invoice(po="13000001"))
    assert next(x for x in printed["lineage"] if x["target"] == "Order No")["evidence_kind"] == fr.EVIDENCE_PRINTED
    # A lone order that disagrees is not linked; items never rescue it.
    lone = run(rows=[pogrn("13000001", "38091", "6", "70")])
    assert lone["header"]["Order No"] == "" and "Missing PO" in types(lone, "POG-001")
    # The agreeing order of another code is never a candidate.
    other = run(rows=both("13000009", "38091", ebs="XYZ002") + [pogrn("13000001", "38091", "6", "70")])
    assert other["header"]["Order No"] == ""
    # Two agreeing order/location groups are ambiguous, even when only one carries the invoice items.
    two = run(rows=POGRN + [pogrn("13000003", "38091", "5", "70", item="345000099")])
    assert two["header"]["Order No"] == "" and "Ambiguous PO" in types(two, "POG-001") and two["po_candidates"] == 2
    split = run(rows=both("13000001", "38091") + both("13000001", "38092"))
    assert split["header"]["Order No"] == "" and "Ambiguous PO" in types(split, "POG-001")


# --------------------------------------------------------------------------- owner POGRN rules


def test_R_025_location_is_exact_order_location_and_never_combined():
    result = run(invoice(po="13000001"), rows=both("13000001", "38091") + both("13000001", "38092"))
    assert result["header"]["Order No"] == "13000001" and result["header"]["Location"] == ""
    assert "Location ID" in types(result, "POG-002")
    assert all("several locations" in g["Exception Reason"] for g in result["pogrn_validation"])
    blank = run(invoice(po="13000001"), rows=both("13000001", ""))
    assert "Location ID blank" in blank["pogrn_validation"][0]["Exception Reason"]
    assert blank["header"]["Location"] == "" and blank["status"] != "Approved"


def test_R_026_location_type_prefix_master_override_and_mismatch():
    assert fr.location_type("800901")["type"] == fr.WAREHOUSE
    assert fr.location_type("38091")["type"] == fr.STORE
    assert fr.location_type("38126")["type"] is None
    config = fr.RulesConfig.from_dict({"location_master": {"38091": {"type": fr.WAREHOUSE}}})
    assert fr.location_type("38091", config)["conflict"] is True
    result = run(config={**CONFIG, "location_master": {"38091": {"type": fr.WAREHOUSE, "market": "Kuwait"}}})
    assert "disagrees with prefix" in result["pogrn_validation"][0]["Exception Reason"]
    assert result["header"]["Location Type"] == "" and result["status"] != "Approved"


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
    loose = run(rows=both("13000001", "38091", q2="1"), config={**CONFIG, "qty_tolerance": "1"})
    assert loose["header"]["Order No"] == "13000001" and "Quantity Mismatch" not in types(loose)


def test_R_029_currency_checked_before_pre_tax_value():
    result = run(rows=both("13000001", "38091", currency="AED"))
    group = result["pogrn_validation"][0]
    assert group["Value Result"] == "Fail" and "Currency differs" in group["Exception Reason"]
    assert group["Aggregated TOTAL COST"] == D(70)


def test_R_030_unique_order_is_derived_and_unmapped_market_stays_blank_and_flagged():
    result = run()
    assert result["header"]["Order No"] == "13000001" and result["po"]["source"] == fr.DERIVED_FROM_POGRN
    unmapped = run(config={**CONFIG, "location_master": {}})
    assert unmapped["header"]["Order No"] == "13000001" and unmapped["header"]["Location"] == "38091"
    assert unmapped["header"]["Market"] == "" and unmapped["status"] != "Approved"
    assert "Market unresolved" in unmapped["pogrn_validation"][0]["Exception Reason"]


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
    assert site["rule"] == "SUP-003" and "Items!2" in site["reference"]
    other_site = ITEMS + [item("345000001", "ULT_0012345678905", "100001", site="22009", name="ABC001RZ1ZZD", ref=12)]
    cited = next(x for x in run(items=other_site)["lineage"] if x["target"] == "Supplier Site")["reference"]
    assert "Items!2" in cited and "Items!12" not in cited  # only the chosen site's rows are cited


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


def test_ALG_009_missing_po_derived_from_the_only_order_carrying_every_item():
    result = run()
    assert result["po"]["status"] == "Approved" and result["po_candidates"] == 1
    assert [g["Validation Status"] for g in result["pogrn_validation"]] == ["Pass"]


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
    # A printed barcode absent from the Item Master is not found, not a disagreement; the VPN route follows.
    assert no_barcode["lines"][0]["Barcode Check"] == fr.ITEM_NOT_FOUND
    assert no_barcode["lines"][0]["Item"] == "345000001" and no_barcode["lines"][0]["Match Method"] == "VPN exact"
    clash = ITEMS + [item("345000011", "ULT_9999999999999", "100011", ref=11)]
    conflict = run(invoice(lines=lines(first={"gtin": "9999999999999"})), items=clash)
    assert conflict["lines"][0]["Item"] == "" and "Item Exception" in types(conflict, "ALG-016") or \
        "Item Conflict" in types(conflict, "ALG-020")


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
    rows = both("13000001", "38091", currency="USD")
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
    "Supplier Site": ("header", "22001", "SUP-003"),
    "Order No": ("header", "13000001", "POG-001"),
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


# --------------------------------------------------------------------------- owner rulesheet (2026-10-05)


def test_shifted_pogrn_rows_are_never_lookup_keys_or_evidence_and_flag_the_invoice():
    shifted = {**pogrn("13000001", "38091", "3", "30", ref=9), "RMS_ITEM_ID": "", "CREATED_DATE": "KWD"}
    assert fr.malformed_row(shifted) and fr.malformed_row({"RMS_ITEM_ID": "ABC", "CREATED_DATE": "2026-01-01"})
    assert not fr.malformed_row({**POGRN[0], "CREATED_DATE": "2026-01-01T00:00:00"})
    printed = run(invoice(po="13000001"), rows=POGRN + [shifted])
    assert "Malformed Source Row" in types(printed, "POG-001") and printed["status"] == "Review"
    assert all("POGRN!9" not in x["reference"] for x in printed["lineage"])
    assert printed["exceptions"][0]["Exception Type"] == "Audit Exception"
    derived = run(rows=POGRN + [shifted])
    assert derived["header"]["Order No"] == "13000001" and "Malformed Source Row" in types(derived)
    elsewhere = {**shifted, "RMS_ORDER_NO": "13000077", "EBS_SUPPLIER_CODE": "XYZ002"}
    assert "Malformed Source Row" not in types(run(rows=POGRN + [elsewhere]))


def party_boxes(seller, buyer, page=1, labels=("Issued By:", "Issued To:")):
    """Synthetic two-column party row: labels at x 140 / 410, names below from x 30 / 306."""
    boxes = []

    def put(phrase, x, top):
        for word in phrase.split():
            boxes.append({"text": word, "page": page, "box": [x, top, x + 6 * len(word), top + 8]})
            x += 6 * len(word) + 4
    put(labels[0], 140, 95)
    put(labels[1], 410, 95)
    put(seller, 30, 108)
    put(buyer, 306, 108)
    put("Total 70", 30, 400)
    return boxes


OWNER = {**CONFIG, "buyer_name": "Synthowner LLC"}


def test_buyer_name_matches_the_entity_on_the_buyer_side_with_the_printed_line_as_evidence():
    boxes = party_boxes("ABC Trading LLC", "SYNTHOWNER INTERNATIONAL CO. L.L.C")
    result = run(invoice(buyer_name="CO. L.L.C"), config=OWNER, boxes=boxes)
    assert result["header"]["Buyer Name"] == "Synthowner LLC" and "Buyer Review" not in types(result)
    trace = next(x for x in result["lineage"] if x["target"] == "Buyer Name")
    assert trace["original"] == "SYNTHOWNER INTERNATIONAL CO. L.L.C" and trace["reference"] == "page 1"
    assert trace["evidence_kind"] == fr.EVIDENCE_PRINTED
    assert result["status"] == "Approved", result["exceptions"]


def test_buyer_name_without_printed_evidence_uses_the_owner_rule():
    missed = run(invoice(buyer_name="CO. L.L.C"), config=OWNER)
    trace = next(x for x in missed["lineage"] if x["target"] == "Buyer Name")
    assert missed["header"]["Buyer Name"] == "Synthowner LLC" and "Buyer Review" not in types(missed)
    assert trace["evidence_kind"] == fr.EVIDENCE_OWNER_RULE and trace["reference"] == fr.BUYER_RULE_EVIDENCE
    assert run()["header"]["Buyer Name"] == ""


def test_buyer_review_only_for_a_buyer_side_company_without_the_entity():
    other = run(invoice(buyer_name="CO. L.L.C"), config=OWNER, boxes=party_boxes("ABC Trading LLC", "Othername Co. L.L.C"))
    assert other["header"]["Buyer Name"] == "Synthowner LLC" and other["status"] == "Review"
    assert any(e["Description"] == "printed buyer differs from owner entity" for e in other["exceptions"])
    # The entity on the seller side is not the buyer; a group company may sell to the owner.
    seller_side = run(invoice(buyer_name=None), config=OWNER, boxes=party_boxes("Synthowner Beauty LLC", "Othername LLC"))
    assert "Buyer Review" in types(seller_side)
    assert "Buyer Review" in types(run(invoice(buyer_name="Othername Co. L.L.C"), config=OWNER))
    # Whole words only: a longer word that merely contains the entity is not it.
    assert "Buyer Review" in types(run(config=OWNER, boxes=party_boxes("ABC Trading LLC", "Synthownerx LLC")))


def test_supplier_candidates_drop_the_buyer_side_by_position_never_by_name():
    scan = fr.scan_pages(boxes=party_boxes("Synthowner Beauty LLC", "SYNTHOWNER INTERNATIONAL CO. L.L.C"))
    page, rows = fr.buyer_party(scan)
    assert page == 1 and rows == ["SYNTHOWNER INTERNATIONAL CO. L.L.C"]
    names = [c["name"] for c in fr.supplier_candidates(scan, buyer_name="CO. L.L.C", buyer_rows=rows)]
    assert names == ["Synthowner Beauty LLC"]
    # A reader supplier field loses buyer-side text, whole or joined on across the columns; a seller-side group
    # company is kept.
    bled = fr.supplier_candidates(scan, printed_name="Synthsell Trading FZCO SYNTHOWNER", buyer_rows=rows)
    assert bled[0]["name"] == "Synthsell Trading FZCO"
    swapped = fr.supplier_candidates(scan, printed_name="SYNTHOWNER INTERNATIONAL CO. L.L.C", buyer_rows=rows)
    assert [c["name"] for c in swapped] == ["Synthowner Beauty LLC"]
    seller = fr.supplier_candidates(scan, printed_name="Synthowner Beauty LLC", buyer_rows=rows)
    assert [c["method"] for c in seller][0] == "extracted supplier field"
    assert fr.buyer_party(fr.scan_pages("Issued By: Issued To:\nA LLC B LLC")) == (None, [])


def test_SUP_001_bridge_and_site_chosen_by_order_ebs_code_and_location_entity():
    family_items = ITEMS + [item("345000001", "ULT_0012345678905", "100001", site="22005", name="ABC001RB2SAR",
                                ref=7),
                            item("345000002", "ULT_0098765432109", "100002", site="22005", name="ABC001RB2SAR",
                                 ref=8)]
    sites = [{"supplier_site": s, "currency": "KWD", "status": "Active", "supplier_code": "1",
              "supplier_name": "ABC Trading LLC", "site_name": n} for s, n in (("22001", "a"), ("22005", "b"))]
    master = {"38091": {"type": fr.STORE, "market": "Kuwait", "entity_currency": "RA1KWD"}}
    config = {**CONFIG, "supplier_sites": sites, "location_master": master}
    result = run(items=family_items, config=config)
    assert result["header"]["Supplier Site"] == "22001" and result["header"]["Order No"] == "13000001"
    trace = [x for x in result["lineage"] if x["target"] == "Supplier Site"][-1]
    assert trace["rule"] == "SUP-003" and "LOCATIONS entity" in trace["source"]
    unknown = {**config, "location_master": {"38091": {"type": fr.STORE, "market": "Kuwait"}}}
    review = run(items=family_items, config=unknown)
    assert review["header"]["Supplier Site"] == "" and "Supplier Site Exception" in types(review, "SUP-003")


def test_R_006_two_supplier_codes_resolved_by_the_printed_deliver_to_location_entity():
    # One printed supplier name, two Active codes; the Item Master relates the items to a site of each code.
    items = ITEMS + [item(p, b, v, site="92006", name="DEF002RB2SAR", ref=r)
                     for p, b, v, r in (("345000001", "ULT_0012345678905", "100001", 7),
                                        ("345000002", "ULT_0098765432109", "100002", 8))]
    sites = [{"supplier_site": s, "currency": c, "status": "Active", "supplier_code": code,
              "supplier_name": "ABC Trading LLC", "site_name": n}
             for s, c, code, n in (("22001", "KWD", "1", "ABC001RA1KWD"), ("92006", "SAR", "2", "DEF002RB2SAR"))]
    master = {"38091": {"type": fr.STORE, "market": "Kuwait", "entity_currency": "RA1KWD"},
              "38092": {"type": fr.STORE, "market": "KSA", "entity_currency": "RB2SAR"}}
    config = {**CONFIG, "supplier_sites": sites, "location_master": master}
    no_po = invoice(po=None)

    picked = run(no_po, items=items, config=config, text_value="Bill To: Ulta Buyer Co   Deliver To: Store 38091")
    header = picked["header"]
    assert (header["Supplier Site"], header["Order No"], header["Location"]) == ("22001", "13000001", "38091")
    code = next(x for x in picked["lineage"] if x["target"] == "Supplier Code")
    assert code["rule"] == "R-006" and code["value"] == "1"
    assert "LOCATIONS 38091|test-1" in code["reference"] and "22001|test-1" in code["reference"]
    site = [x for x in picked["lineage"] if x["target"] == "Supplier Site"][0]
    assert "LOCATIONS 38091|test-1" in site["reference"]
    assert "SUP-001" not in {e["Rule ID"] for e in picked["exceptions"]} and picked["supplier_site_candidates"] == []

    other = run(no_po, items=items, config=config, text_value="DELIVERY ADDRESS: 38092")
    assert other["header"]["Supplier Site"] == "92006" and other["header"]["Order No"] == ""

    def reason(result):
        assert result["header"]["Supplier Site"] == "" and result["header"]["Order No"] == ""
        assert "Supplier Exception" in types(result, "SUP-001")
        return " ".join(e["Description"] for e in result["exceptions"] if e["Rule ID"] == "R-006")

    assert "not printed as a LOCATION id" in reason(run(no_po, items=items, config=config,
                                                        text_value="Deliver To: Store Name, City\nP.O. Box 38091"))
    assert "not a LOCATION id" in reason(run(no_po, items=items, config=config, text_value="Deliver To: 99999"))
    assert "2 different LOCATIONS ids" in reason(run(no_po, items=items, config=config,
                                                     text_value="Deliver To: 38091 or 38092"))
    both = config | {"supplier_sites": sites + [{**sites[1], "supplier_site": "92007", "site_name": "DEF002RA1KWD"}]}
    tied = run(no_po, items=items, config=both, text_value="Deliver To: Store 38091")
    assert "2 of the 2 supplier codes" in reason(tied)
    # The codes left are offered as cited candidates for the reviewer's pick; the field itself stays empty.
    offered = tied["supplier_site_candidates"]
    assert [(c["supplier_code"], [s["supplier_site"] for s in c["sites"]]) for c in offered] == [
        ("1", ["22001"]), ("2", ["92007"])]
    assert offered[1]["sites"][0]["reference"] == "92007|test-1" and offered[0]["location_reference"] == \
        "LOCATIONS 38091|test-1"
    unprinted = run(no_po, items=items, config=config, text_value="Deliver To: Store Name")
    assert [c["supplier_code"] for c in unprinted["supplier_site_candidates"]] == ["1", "2"]
    no_entity = config | {"location_master": {"38091": {"type": fr.STORE, "market": "Kuwait"}}}
    assert "no ENTITY AND CURENCY" in reason(run(no_po, items=items, config=no_entity,
                                                 text_value="Deliver To: Store 38091"))


def test_02A_line_below_quantity_x_cost_is_explained_by_a_printed_invoice_discount():
    def unit_cost_lines(first, second, net, text_value):
        inv = invoice(net=D(net), lines=lines(first={"net_amount": D(first)}, second={"net_amount": D(second)}))
        result = run(inv, text_value=text_value)
        return [e["Line No."] for e in result["exceptions"] if e["Rule ID"] == "02A-Unit Cost"]

    printed = "Invoice\fTotal 70.00\nDiscount -15.00\nNet Total 55.00"
    assert unit_cost_lines("15", "40", "55", printed) == []
    assert unit_cost_lines("0", "40", "40", "Total 70.000\nLess: Discount (30.000)\nNet 40.000") == []
    assert unit_cost_lines("15", "40", "55", printed.replace("Discount", "Less")) == [1]
    assert unit_cost_lines("15", "40", "55", printed.replace("-15.00", "-10.00")) == [1]
    assert unit_cost_lines("15", "40", "55", printed.replace("Total 70.00", "Total")) == [1]
    assert unit_cost_lines("15", "40", "60", printed) == [1]
    assert unit_cost_lines("45", "10", "55", printed) == [1]


def test_TGT_001_totals_are_traced_to_their_printed_page_else_flagged():
    traced = run(text_value="Net 70.000\fTax 0\nTotal 70")
    refs = {x["target"]: x["reference"] for x in traced["lineage"]}
    assert refs["Net Amount"] == "page 1" and refs["Tax Amount"] in ("page 1", "page 2")
    assert traced["header"]["Gross Amount"] == D(70)
    missing = run()
    assert "Totals Audit" in types(missing) and missing["status"] == "Approved"


def test_TGT_001_amount_printed_with_two_decimals_or_thousands_separators_is_found():
    scan = {"pages": {1: "Invoice", 2: "Net Total 520.00\nVAT 0.00"}}
    assert fr._printed_page(scan, D("520.0")) == 2
    assert fr._printed_page({"pages": {1: "Total 1,520.00"}}, D("1520.0")) == 1
    assert fr._printed_page({"pages": {1: "Total 1,520"}}, D("1520.0")) == 1
    assert fr._printed_page({"pages": {1: "Total KWD 1,520.000"}}, D("1520.0")) == 1
    assert fr._printed_page({"pages": {1: "Net 520.000"}}, D("520.0")) == 1
    assert fr._printed_page({"pages": {1: "Net 520.0005"}}, D("520.0")) is None
    assert fr._printed_page(scan, D("520.5")) is None
    assert fr._printed_page({"pages": {1: "Total 520.50"}}, D("520.0")) is None
    assert fr._printed_page({"pages": {1: "Total 1,520.00"}}, D("520.0")) is None


def test_item_resolution_counts_lines_whose_quantity_agrees_with_the_order():
    good = run()["item_resolution"]
    assert (good["resolved"], good["total"], good["below"]) == (2, 2, False)
    short = run(invoice(po="13000001"), rows=both("13000001", "38091", q2="1"))
    assert short["item_resolution"]["resolved"] == 1 and short["item_resolution"]["below"] is True
    assert "Item Quantity Mismatch" in types(short, "C-11") and "Owner Validation" in types(short, "ITEM-LINE-95")


def test_upc_trace_records_the_ult_prefix_removal():
    result = run(invoice(lines=lines(first={"gtin": "ULT_0012345678905"})))
    trace = next(x for x in result["lineage"] if x["target"] == "UPC" and x["line"] == 1)
    assert result["lines"][0]["UPC"] == "0012345678905" and "ULT_ prefix removed" in trace["rule"]


def test_exception_types_are_the_owner_checklist_failure_statuses():
    result = run(invoice(po="99999999"), rows=both("13000001", "38091", q2="1"))
    by_kind = {e["Engine Type"]: (e["Exception Type"], e["Check ID"]) for e in result["exceptions"]}
    assert by_kind["Missing PO"] == ("Missing/Ambiguous PO", "C-12")
    assert fr.FAILURE_STATUS["Value Mismatch"] == ("Value Mismatch", "C-16")
    assert fr.FAILURE_STATUS["Quantity Mismatch"] == ("Quantity Mismatch", "C-15")
    assert fr.FAILURE_STATUS["Malformed Source Row"] == ("Audit Exception", "C-17")
