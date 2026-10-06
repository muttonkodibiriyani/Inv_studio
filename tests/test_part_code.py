"""Synthetic: a printed non-GTIN part code is kept whole on the line as part_code."""

from app.docling_extract import extract_invoice_from_tables
from app.models import Invoice, Line, extraction_schema, parse_ai_output


def word(text, left, top, *, width=18, page=1, size=(320, 100)):
    return {"text": text, "page": page, "box": [left, top, left + width, top + 6], "size": list(size)}


def measured_part_table():
    boxes = [word("SN", 8, 20, width=12), word("Barcode / Part", 24, 20, width=40),
             word("Description", 80, 20, width=42), word("Qty", 170, 20, width=16),
             word("Unit Price", 218, 20, width=35), word("Net Amount", 270, 20, width=42)]
    rows = [("SYN-VAR-01", "Synthetic cream"), ("4000000000006", "Synthetic soap"),
            ("PR-001234567890-OP", "Synthetic lotion SKU: SYN-B")]
    for n, (code, description) in enumerate(rows, start=1):
        top = 20 + 14 * n
        boxes += [word(str(n), 10, top, width=8), word(code, 24, top, width=50),
                  word(description, 80, top, width=85), word("2", 173, top, width=8),
                  word("4.00", 224, top, width=20), word("8.00", 276, top, width=20)]
    return boxes


def test_measured_words_table_keeps_non_gtin_part_codes_whole():
    result = extract_invoice_from_tables("Tax Invoice", [], boxes=measured_part_table())

    assert result is not None
    lines = result["lines"]
    assert len(lines) == 3
    assert lines[0]["part_code"] == "SYN-VAR-01"
    assert lines[0]["sku"] == "SYN-VAR-01"
    assert lines[1]["gtin"] == "4000000000006"
    assert "part_code" not in lines[1]
    # Embedded digits stay inside the whole printed code; they never become a GTIN.
    assert lines[2]["part_code"] == "PR-001234567890-OP"
    assert "gtin" not in lines[2]


def test_part_code_is_set_even_when_the_description_code_fills_sku():
    tables = [{"page": 1, "rows": [
        ["Barcode / Part Number", "Description", "Qty", "Unit Price"],
        ["SYN-VAR-02", "Synthetic gel SKU: SYN-C", "1", "3.00"],
        ["40000000", "Synthetic wipes SKU: SYN-D", "1", "2.00"],
    ]}]

    lines = extract_invoice_from_tables("Tax Invoice", tables)["lines"]

    assert lines[0]["sku"] == "SYN-C"
    assert lines[0]["part_code"] == "SYN-VAR-02"
    assert lines[1]["gtin"] == "40000000"
    assert "part_code" not in lines[1]
    assert Invoice.model_validate({"lines": lines}).lines[0].part_code == "SYN-VAR-02"


def test_a_table_without_a_part_column_sets_no_part_code():
    tables = [{"page": 1, "rows": [
        ["Description", "Qty", "Unit Price"],
        ["Synthetic towel SKU: SYN-E", "1", "5.00"],
    ]}]

    assert "part_code" not in extract_invoice_from_tables("Tax Invoice", tables)["lines"][0]


def test_ai_readers_are_never_asked_for_or_given_part_code():
    assert "part_code" in Line.model_fields
    assert "part_code" not in extraction_schema()["properties"]["lines"]["items"]["properties"]
    invoice, _evidence = parse_ai_output(
        {"lines": [{"sku": "SYN-F", "part_code": "SYN-VAR-03", "qty": "1", "price": "1.00"}]})
    assert invoice.lines[0].part_code is None
    assert invoice.lines[0].sku == "SYN-F"
