import json
from app import evidence as E
from app.models import HEADER_EVIDENCE_FIELDS, Invoice, ai_schema, parse_ai_output


def word(text, x0, top, page=1, size=(600, 800)):
    return {"text": text, "page": page, "box": [x0, top, x0 + 6 * len(text), top + 10], "size": list(size)}


def boxes():
    return [word("Invoice", 30, 50), word("No:", 80, 50), word("INV-10045", 110, 50),
            word("ACME", 30, 100), word("Trading", 70, 100), word("LLC", 120, 100),
            word("Total", 30, 400), word("1,234.50", 300, 400),
            word("4-pack", 30, 200, page=2), word("Widget", 80, 200, page=2), word("1234567890128", 200, 200, page=2)]


def test_locate_returns_quote_page_and_a_normalised_box():
    entry = E.locate("INV-10045", boxes())
    assert entry["quote"] == "INV-10045" and entry["page"] == 1
    assert entry["box"][0] == round(110 / 600, 4) and entry["box"][1] == round(50 / 800, 4)
    assert all(0 <= v <= 1 for v in entry["box"])
    assert E.locate("ACME Trading LLC", boxes())["quote"] == "ACME Trading LLC"
    assert E.locate("acme trading llc", boxes()) is not None
    assert E.locate("INV-10046", boxes()) is None
    assert E.locate("", boxes()) is None


def test_amounts_match_across_thousands_separators_and_trailing_zeros():
    assert E.locate("1234.50", boxes(), numeric=True)["quote"] == "1,234.50"
    assert E.locate("1234.5", boxes(), numeric=True)["quote"] == "1,234.50"
    assert E.locate("1234.51", boxes(), numeric=True) is None
    assert E.locate("1234.5", boxes()) is None


def test_near_miss_is_a_same_length_token_one_character_away():
    layout = E.Layout(boxes())
    assert layout.near_miss("INV-10046") == "INV-10045"
    assert layout.near_miss("INV-10045") is None
    assert layout.near_miss("INV-19946") is None
    assert layout.near_miss("LLX") is None


def native_invoice():
    return {"number": "INV-10045", "supplier_name": "ACME Trading LLC", "net": "1234.50", "date": "2026-03-07",
            "lines": [{"gtin": "1234567890128", "description": "4-pack Widget", "qty": "2",
                       "evidence": "4-pack Widget 2", "page": 2}]}


def test_build_locates_native_values_and_falls_back_to_line_quotes():
    evidence = E.build(native_invoice(), boxes(), "native")
    header = evidence["header"]
    assert set(header) == {"number", "supplier_name", "net"}
    assert header["number"]["source"] == "native" and "box" in header["number"]
    assert header["net"]["quote"] == "1,234.50"
    line = evidence["lines"][0]
    assert line["gtin"]["page"] == 2 and "box" in line["gtin"]
    assert line["description"]["quote"] == "4-pack Widget" and "box" in line["description"]
    assert line["qty"] == {"quote": "4-pack Widget 2", "page": 2, "source": "native"}


def test_build_keeps_ai_quotes_adds_boxes_and_drops_evidence_for_empty_fields():
    header = {"number": {"quote": "Invoice No: INV-10045", "page": 1}, "po": {"quote": "PO 77", "page": 1}}
    evidence = E.build(native_invoice(), boxes(), "ai", header=header)
    assert evidence["header"]["number"]["quote"] == "Invoice No: INV-10045"
    assert evidence["header"]["number"]["source"] == "ai" and "box" in evidence["header"]["number"]
    assert "po" not in evidence["header"]
    assert evidence["header"]["supplier_name"]["source"] == "ai"
    without_layer = E.build(native_invoice(), [], "ai", header=header)
    assert without_layer["header"] == {"number": {"quote": "Invoice No: INV-10045", "page": 1, "source": "ai"}}
    assert without_layer["lines"][0]["qty"] == {"quote": "4-pack Widget 2", "page": 2, "source": "ai"}


def test_ocr_verification_adds_boxes_and_flags_a_near_miss_without_changing_values():
    invoice = {"number": "INV-10046", "supplier_name": "ACME Trading LLC", "net": "1234.50",
               "lines": [{"gtin": "1234567890128", "qty": "2"}]}
    before = json.dumps(invoice, sort_keys=True)
    evidence = {"header": {"number": {"quote": "INV-10046", "page": 1, "source": "ai"}}, "lines": []}
    result = E.verify_with_ocr(invoice, evidence, boxes())
    number = result["header"]["number"]
    assert number["quote"] == "INV-10046" and number["source"] == "ai" and "box" not in number
    assert number["review"] == {"reason": "The local OCR text layer reads a different value here",
                                "other_value": "INV-10045"}
    assert "box" in result["header"]["supplier_name"] and result["header"]["net"]["quote"] == "1,234.50"
    assert "box" in result["lines"][0]["gtin"] and "qty" not in result["lines"][0]
    assert json.dumps(invoice, sort_keys=True) == before
    assert evidence["header"]["number"].get("review") is None


def test_filename_digits_one_character_away_are_flagged_never_corrected():
    assert E.filename_review("INV-10045", "10046-scan.pdf") == {
        "reason": "The file name carries digits one character away from the invoice number", "other_value": "10046"}
    assert E.filename_review("10045", "scan-10045.pdf") is None
    assert E.filename_review("10045", "scan-20045-10045.pdf") is None
    assert E.filename_review("10045", "scan.pdf") is None
    assert E.filename_review("1045", "1046.pdf") is None
    annotated = E.annotate({"header": {}, "lines": [{}]}, {"number": "INV-10045", "lines": [{}]}, filename="10046.pdf")
    assert annotated["header"]["number"]["review"]["other_value"] == "10046"
    assert annotated["header"]["number"]["source"] == "ai" and annotated["lines"] == [{}]
    assert E.annotate(None, {"number": "INV-10045"}, filename="INV-10045.pdf") == {"header": {}, "lines": []}


def test_ai_schema_asks_for_header_evidence_outside_the_invoice():
    schema = ai_schema()
    assert "header_evidence" in schema["required"]
    evidence = schema["properties"]["header_evidence"]
    assert tuple(evidence["properties"]) == HEADER_EVIDENCE_FIELDS and evidence["additionalProperties"] is False
    assert evidence["properties"]["buyer_name"]["required"] == ["quote", "page"]
    assert "header_evidence" not in Invoice.model_fields


def test_parse_ai_output_splits_evidence_and_drops_blank_or_impossible_entries():
    payload = {"number": "SYN-1", "lines": [], "header_evidence": {
        "number": {"quote": " Invoice SYN-1 ", "page": 1}, "buyer_name": {"quote": "", "page": 1},
        "net": {"quote": "Total 10.00", "page": 99}, "tax": None, "po": "not an entry"}}
    invoice, evidence = parse_ai_output(json.dumps(payload))
    assert invoice.number == "SYN-1" and not hasattr(invoice, "header_evidence")
    assert evidence == {"number": {"quote": "Invoice SYN-1", "page": 1}, "net": {"quote": "Total 10.00", "page": None}}
    assert parse_ai_output({"number": "SYN-2", "lines": []})[1] == {}
