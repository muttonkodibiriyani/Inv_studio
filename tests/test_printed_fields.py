"""Synthetic regressions for printed fields the digital and scan readers left empty (T3)."""
from types import SimpleNamespace

import pytest

from app import engines, scan_guard
from app.docling_extract import extract_invoice_from_tables, heading_columns, line_columns
from app.models import Invoice

G1, G2 = "40000008", "96385074"  # GS1-valid synthetic EAN-8 codes
TEXT = "Tax Invoice\nInvoice No: SYN-INV-1\nTotal 25.00 AED\n"


def word(text, x, top, page=1):
    return {"text": text, "page": page, "box": [x, top, x + 6 * len(text), top + 8], "size": [600, 800]}


def phrase(spec, top):
    """Words of each (x, phrase) cell, 4 units apart inside a cell."""
    out = []
    for x, text in spec:
        for token in text.split():
            out.append(word(token, x, top))
            x += 6 * len(token) + 4
    return out


def heading(text, top=100):
    """One printed heading row, every word a measured box."""
    out, x = [], 10
    for token in text.split():
        out.append(word(token, x, top))
        x += 6 * len(token) + 8
    return out


HEADING = "SKU Barcode Description Qty Unit Price Total Net Weight TOTAL AMOUNT"


def ruled(vat=False, sku2="SYN-B2", gtin2=G2, net=("10.00 AED", "15.00 AED")):
    """A ruled grid whose first and last headings print outside the cells."""
    head = [None, "Barcode", "Description", "Qty", "Unit Price", "Total Net Weight", *(["VAT"] if vat else []), None]
    rows = [["SYN-A1", G1, "Synthetic soap", "2", "5.00 AED", "1.20 KG", *(["0.50 AED"] if vat else []), net[0]],
            [sku2, gtin2, "Synthetic towel", "3", "5.00 AED", "0.90 KG", *(["0.75 AED"] if vat else []), net[1]]]
    return [{"page": 1, "source": "pdfplumber", "rows": [head, *rows]}]


def test_edge_headings_outside_the_grid_are_read_and_weight_is_never_net():
    invoice = extract_invoice_from_tables(TEXT, ruled(), heading(HEADING))
    assert [line["sku"] for line in invoice["lines"]] == ["SYN-A1", "SYN-B2"]
    assert [line["net_amount"] for line in invoice["lines"]] == ["10.00", "15.00"]
    assert [line["price"] for line in invoice["lines"]] == ["5.00", "5.00"]
    assert invoice["net"] == "25.00"
    assert not any(value.endswith("KG") for line in invoice["lines"] for value in map(str, line.values()))
    assert line_columns(TEXT, ruled(), heading(HEADING)) == {"sku", "gtin", "description", "qty", "price", "net_amount"}


def test_total_net_weight_heading_never_maps_to_an_amount():
    tables = ruled()
    tables[0]["rows"][0][-1] = "Amount"
    tables[0]["rows"] = [[*row[:-1]] for row in tables[0]["rows"]]  # no line amount column at all
    invoice = extract_invoice_from_tables("Tax Invoice\nInvoice No: SYN-INV-1\n", tables, heading(HEADING))
    assert all("net_amount" not in line for line in invoice["lines"]) and "net" not in invoice


def test_total_amount_beside_a_vat_column_waits_for_the_printed_totals():
    words = heading("SKU Barcode Description Qty Unit Price Total Net Weight VAT TOTAL AMOUNT")
    # The page text prints the VAT heading, so the bare Total may be gross.
    unread = extract_invoice_from_tables(TEXT + " ".join(w["text"] for w in words) + "\n", ruled(vat=True), words)
    assert all("net_amount" not in line for line in unread["lines"]) and "net" not in unread
    printed = "Tax Invoice\nInvoice No: SYN-INV-1\nNet Total 25.00\nVAT 5% 1.25\nGrand Total 26.25\n"
    invoice = extract_invoice_from_tables(printed, ruled(vat=True), words)
    assert [line["net_amount"] for line in invoice["lines"]] == ["10.00", "15.00"] and invoice["net"] == "25.00"


def test_an_interior_empty_heading_stays_unmapped():
    tables = ruled()
    tables[0]["rows"][0][5] = None
    invoice = extract_invoice_from_tables(TEXT, tables, heading("SKU Barcode Description Qty Unit Price TOTAL AMOUNT"))
    assert [line["sku"] for line in invoice["lines"]] == ["SYN-A1", "SYN-B2"]
    # The last edge is filled only beside a printed neighbour heading.
    assert all("net_amount" not in line for line in invoice["lines"]) and "net" not in invoice
    assert tables[0]["rows"][0][0] is None  # the input table is never changed


def label_rows(invoice_date, due_date, po="SYN-PO-3"):
    labels = [(10, "Invoice No"), (110, "Invoice Date"), (210, "Due Date"), (310, "PO Number")]
    values = [(10, "SYN-INV-7"), (110, invoice_date), (210, due_date), (310, po)]
    return phrase(labels, 20) + phrase(values, 32)


@pytest.mark.parametrize(("printed", "due", "expected"), [
    ("15/01/2026", "20/02/2026", "2026-01-15"),
    ("2026-01-15", "2026-02-20", "2026-01-15"),
    ("05/01/2026", "20/02/2026", None),  # ambiguous day/month: never guessed
])
def test_header_labels_over_values_fill_beside_a_native_line_table(printed, due, expected):
    invoice = extract_invoice_from_tables("Tax Invoice\nTotal 25.00 AED\n", ruled(), label_rows(printed, due) + heading(HEADING))
    assert invoice["number"] == "SYN-INV-7" and invoice["po"] == "SYN-PO-3"
    assert invoice.get("date") == expected and invoice["date_printed"] == printed
    assert len(invoice["lines"]) == 2


def test_header_labels_never_replace_a_value_read_from_the_text():
    text = "Tax Invoice\nInvoice No: SYN-INV-1\nTotal 25.00 AED\n"
    invoice = extract_invoice_from_tables(text, ruled(), label_rows("15/01/2026", "20/02/2026") + heading(HEADING))
    assert invoice["number"] == "SYN-INV-1" and invoice["date"] == "2026-01-15"


@pytest.mark.parametrize(("text", "net"), [
    ("Total 25.00 AED", "25.00"),
    ("Total: AED 25.01", "25.01"),  # inside the existing lines_reconcile tolerance
    ("Total 25.05 AED", None),      # a gap beyond it: no net
    ("VAT 0.00\nTotal 25.00 AED", None),  # a tax label is printed: a bare Total may be gross
    ("Total 25.00 AED\nTotal 30.00 AED", None),  # two different bare totals
])
def test_a_bare_total_is_the_net_only_without_tax_and_when_the_lines_sum_to_it(text, net):
    invoice = extract_invoice_from_tables("Tax Invoice\nInvoice No: SYN-INV-1\n" + text + "\n", ruled(), heading(HEADING))
    assert invoice.get("net") == net


def test_barcode_codes_name_every_line_barcode_reason():
    assert set(scan_guard.BARCODE_CODES) == {scan_guard.MISPRINT, scan_guard.UNREADABLE, scan_guard.UNCONFIRMED}
    assert set(scan_guard.BARCODE_CODES.values()) == {"misprint", "unreadable", "unconfirmed"}


# --- guard_printed, on the final invoice ---

def test_guard_printed_flags_empty_always_printed_header_fields_only():
    invoice = {"supplier_name": None, "number": "SYN-1", "date": None, "net": "", "po": None, "lines": []}
    evidence = engines.guard_printed(invoice, {"header": {"number": {"quote": "SYN-1"}}}, "Tax Invoice", set(), "native")
    header = evidence["header"]
    assert {f for f, e in header.items() if "review" in e} == {"supplier_name", "date", "net"}
    for field in ("supplier_name", "date", "net"):
        assert header[field]["review"] == {"reason": engines.NOT_READ_ALWAYS, "other_value": None, "code": "not_read"}
    assert "po" not in header  # no PO label printed
    assert "review" not in header["number"]


def test_guard_printed_never_replaces_a_coded_review_and_codes_an_uncoded_one():
    header = {"date": {"quote": "", "review": {"reason": "Check: two dates", "other_value": None}},
              "net": {"quote": "", "review": {"reason": "x", "other_value": None, "code": "not_printed"}}}
    evidence = engines.guard_printed({"supplier_name": "SYN Supplier", "number": "1", "lines": []},
                                     {"header": header}, "", set(), "native")
    assert evidence["header"]["date"]["review"] == {"reason": "Check: two dates", "other_value": None, "code": "not_read"}
    assert evidence["header"]["net"]["review"]["code"] == "not_printed"
    assert header["date"]["review"].get("code") is None  # the input evidence is not changed


@pytest.mark.parametrize(("text", "flag"), [
    ("P.O. Box 1234\nDubai", None),
    ("PO Box: 55", None),
    ("PO Number:\nSYN", "PO Number"),
    ("Customer P.O. No.\n", "Customer P.O. No."),
    ("LPO\n", "LPO"),
    ("Purchase Order #", "Purchase Order #"),
    ("Delivery to P.O. Box 9, PO No.", None),  # a box address on the same line is not trusted
])
def test_po_is_flagged_only_under_a_printed_po_label(text, flag):
    invoice = {"supplier_name": "S", "number": "1", "date": "2026-01-15", "net": "1.00", "po": None, "lines": []}
    header = engines.guard_printed(invoice, {}, text, set(), "native")["header"]
    if flag is None:
        assert "po" not in header
    else:
        assert header["po"]["review"] == {"reason": f"'{flag}' label printed, no value read", "other_value": None,
                                          "code": "not_read"}


def test_po_review_already_present_is_kept():
    invoice = {"supplier_name": "S", "number": "1", "date": "2026-01-15", "net": "1.00", "po": None, "lines": []}
    box = {"po": {"quote": "P.O. Box 7", "review": {"reason": "P.O. Box, not a PO", "other_value": "7"}}}
    header = engines.guard_printed(invoice, {"header": box}, "PO Number:", set(), "native")["header"]
    assert header["po"] == box["po"]


def test_line_cells_under_a_printed_heading_are_flagged_and_evidence_has_one_entry_per_line():
    lines = [{"sku": "A", "gtin": G1, "qty": "1"}, {"sku": None, "gtin": None, "qty": "2", "description": ""},
             {"sku": None, "gtin": None, "barcode_unchecked": "40000009", "qty": "1"}]
    evidence = {"lines": [{"gtin": {"quote": G1}}]}
    out = engines.guard_printed({"supplier_name": "S", "number": "1", "date": "2026-01-15", "net": "1", "lines": lines},
                                evidence, "", {"sku", "gtin", "qty"}, "native")
    assert len(out["lines"]) == len(lines)
    assert out["lines"][0] == {"gtin": {"quote": G1}}
    assert out["lines"][1]["sku"]["review"] == {"reason": "SKU column present, cell empty", "other_value": None,
                                                "code": "not_read"}
    assert out["lines"][1]["gtin"]["review"]["reason"] == "Barcode column present, cell empty"
    assert "description" not in out["lines"][1]  # no Description heading was found
    assert "gtin" not in out["lines"][2]  # a misprint is kept aside under its own code
    assert out["lines"][2]["sku"]["review"]["code"] == "not_read"


def test_line_evidence_is_padded_never_truncated():
    lines = [{"sku": "A"}]
    out = engines.guard_printed({"supplier_name": "S", "number": "1", "date": "d", "net": "1", "lines": lines},
                                {"lines": [{}, {"sku": {"quote": "B"}}]}, "", {"sku"}, "native")
    assert len(out["lines"]) == 2
    out = engines.guard_printed({"supplier_name": "S", "number": "1", "date": "d", "net": "1", "lines": lines * 3},
                                {}, "", {"sku"}, "native")
    assert out["lines"] == [{}, {}, {}]


def test_a_coded_line_barcode_review_is_never_replaced():
    review = {"quote": "", "review": {"reason": scan_guard.UNCONFIRMED, "other_value": G2, "code": "unconfirmed"}}
    out = engines.guard_printed({"supplier_name": "S", "number": "1", "date": "d", "net": "1", "lines": [{"gtin": None}]},
                                {"lines": [{"gtin": review}]}, "", {"gtin"}, "ai")
    assert out["lines"][0]["gtin"] == review


# --- process(), end to end ---

def run_digital(monkeypatch, tmp_path, text, tables, boxes):
    monkeypatch.setattr(engines, "capabilities", lambda: [{"id": x, "installed": True} for x in ("invoice2data", "docling")])

    def read(engine, *args, **kwargs):
        if engine == "invoice2data":
            return {"text": text, "boxes": boxes, "tables": tables,
                    "invoice": extract_invoice_from_tables(text, tables, boxes)}
        raise ValueError("not used")
    monkeypatch.setattr(engines, "local_read", read)
    opts = SimpleNamespace(engine="auto", provider="vertex", model="gemini-test", ai_fallback=False, language="en",
                           prefer_native_text=True)
    return engines.process(tmp_path / "digital.pdf", opts, SimpleNamespace(root=tmp_path), lambda *a: None)


def test_process_on_a_ruled_digital_invoice_validates_and_flags_only_printed_gaps(monkeypatch, tmp_path):
    text = "SYN Trading\nTax Invoice\nPO Number:\nTotal 25.00 AED\n" + "Synthetic line text " * 4
    words = label_rows("15/01/2026", "20/02/2026", po="") + heading(HEADING)
    result = run_digital(monkeypatch, tmp_path, text, ruled(sku2=None), words)
    stored = Invoice.model_validate(result["invoice"])  # extra=forbid: no side value on the invoice
    assert "_line_columns" not in result["invoice"] and not any(k.startswith("_") for k in result["invoice"])
    assert stored.number == "SYN-INV-7" and str(stored.net) == "25.00" and len(stored.lines) == 2
    assert stored.lines[1].sku is None and stored.lines[1].gtin == G2
    lines = result["evidence"]["lines"]
    assert len(lines) == len(result["invoice"]["lines"])
    assert lines[1]["sku"]["review"]["code"] == "not_read"
    assert not any("review" in entry for entry in lines[0].values() if isinstance(entry, dict))
    assert result["reason_codes"] == {"supplier_name": "not_read", "po": "not_read", "tax": "not_printed"}
    assert result["evidence"]["header"]["po"]["review"]["reason"] == "'PO Number' label printed, no value read"
    assert result["absent_fields"] == ["tax"]


def scan_box(text, x, top):
    return {"text": text, "page": 1, "geometry": "word", "box": [x, top, x + 6 * len(text), top + 8], "size": [600, 800]}


def test_scan_read_by_the_ai_flags_an_empty_barcode_under_a_printed_barcode_heading(monkeypatch, tmp_path):
    """D162: a covered barcode on a scan is not a silent empty, whether or not the AI offered a code."""
    monkeypatch.setattr(engines, "capabilities", lambda: [{"id": x, "installed": True} for x in ("invoice2data", "paddleocr")])
    boxes, x = [], 10
    for token in "Barcode Description Qty Unit Price Amount".split():
        boxes.append(scan_box(token, x, 100))
        x += 6 * len(token) + 8
    for top, (code, desc, qty, net) in zip((120, 140), ((G1, "Soap", "2", "10.00"), ("####", "Towel", "3", "15.00"))):
        boxes += [scan_box(code, 10, top), scan_box(desc, 70, top), scan_box(qty, 150, top),
                  scan_box("5.00", 180, top), scan_box(net, 230, top)]
    ocr_text = "\n".join(b["text"] for b in boxes)

    def read(engine, *args, **kwargs):
        if engine == "invoice2data":
            return {"text": " ", "boxes": [], "invoice": None}
        if engine == "paddleocr":
            return {"text": ocr_text, "boxes": boxes, "invoice": None}
        raise ValueError("not used")
    monkeypatch.setattr(engines, "local_read", read)

    def ai_reader(*args):
        invoice = {"supplier_name": "SYN Supplier", "number": "SCAN-1", "date": "2026-01-15", "po": "PO-1",
                   "net": "25.00", "tax": "1.25", "lines": [
                       {"gtin": G1, "description": "Soap", "qty": "2", "price": "5.00", "net_amount": "10.00", "page": 1},
                       {"gtin": None, "description": "Towel", "qty": "3", "price": "5.00", "net_amount": "15.00", "page": 1}]}
        return Invoice.model_validate(invoice), {"evidence": {"net": {"quote": "Net 25.00", "page": 1},
                                                              "tax": {"quote": "VAT 1.25", "page": 1}}}
    opts = SimpleNamespace(engine="auto", provider="vertex", model="gemini-test", ai_fallback=True, language="en",
                           prefer_native_text=True)
    result = engines.process(tmp_path / "scan.pdf", opts, SimpleNamespace(root=tmp_path), ai_reader)
    Invoice.model_validate(result["invoice"])
    lines = result["evidence"]["lines"]
    assert [line["gtin"] for line in result["invoice"]["lines"]] == [G1, None]
    assert len(lines) == 2 and lines[1]["gtin"]["review"]["code"] == "not_read"
    assert "review" not in lines[0].get("gtin", {})


G3 = "40000015"  # GS1-valid synthetic EAN-8

# A heading printed on three stacked lines, every word its own OCR line. Qty and Price never share a row,
# so no measured-word table accepts a line heading here.
STACKED = [("SKU", 10, 88), ("Qty", 250, 88), ("Barcode", 50, 96), ("Description", 130, 96),
           ("Price", 300, 104), ("Line", 360, 104), ("Amount", 390, 104)]


def stacked_scan(rows, body_barcode=False):
    boxes = [scan_box(text, x, top) for text, x, top in STACKED]
    for n, (code, desc, qty, net) in enumerate(rows):
        top = 140 + 20 * n
        boxes += [scan_box(str(n + 1), 10, top), scan_box(code, 50, top), scan_box(desc, 130, top),
                  scan_box(qty, 250, top), scan_box("5.00", 300, top), scan_box(net, 360, top)]
    if body_barcode:
        boxes += [scan_box("Barcode", 130, 300), scan_box("scanner", 180, 300), scan_box("12", 250, 300)]
    return boxes


def run_scan(monkeypatch, tmp_path, boxes, ai_lines):
    monkeypatch.setattr(engines, "capabilities", lambda: [{"id": x, "installed": True} for x in ("invoice2data", "paddleocr")])
    ocr_text = "\n".join(b["text"] for b in boxes)

    def read(engine, *args, **kwargs):
        if engine == "invoice2data":
            return {"text": " ", "boxes": [], "invoice": None}
        if engine == "paddleocr":
            return {"text": ocr_text, "boxes": boxes, "invoice": None}
        raise ValueError("not used")
    monkeypatch.setattr(engines, "local_read", read)

    def ai_reader(*args):
        invoice = {"supplier_name": "SYN Supplier", "number": "SCAN-2", "date": "2026-01-15", "po": "PO-2",
                   "net": "35.00", "tax": "1.75", "lines": [
                       {"gtin": gtin, "description": desc, "qty": "1", "price": "5.00", "net_amount": net, "page": 1}
                       for gtin, desc, net in ai_lines]}
        return Invoice.model_validate(invoice), {"evidence": {"net": {"quote": "Net 35.00", "page": 1},
                                                              "tax": {"quote": "VAT 1.75", "page": 1}}}
    opts = SimpleNamespace(engine="auto", provider="vertex", model="gemini-test", ai_fallback=True, language="en",
                           prefer_native_text=True)
    result = engines.process(tmp_path / "scan.pdf", opts, SimpleNamespace(root=tmp_path), ai_reader)
    Invoice.model_validate(result["invoice"])
    return result


def gtin_codes(result):
    rows = result["evidence"]["lines"]
    return [(rows[n].get("gtin") or {}).get("review", {}).get("code") if line["gtin"] in (None, "") else "read"
            for n, line in enumerate(result["invoice"]["lines"])]


def test_heading_words_on_stacked_ocr_lines_name_the_columns_and_the_first_block_only():
    boxes = stacked_scan([(G1, "Soap", "1", "5.00")])
    boxes += [scan_box("Net Amount", 300, 400), scan_box("VAT Amount", 400, 400)]
    assert heading_columns(boxes[-2:]) == {"net_amount", "tax_amount"}  # alone, the totals band qualifies
    assert line_columns(" ", [], boxes) == set()  # the table route accepts no heading row here
    assert heading_columns(boxes) >= {"gtin", "description", "qty", "price"}
    assert "tax_amount" not in heading_columns(boxes)  # the totals block below the lines adds nothing


def test_a_body_word_barcode_is_never_a_heading():
    body = [scan_box(t, x, 300) for t, x in (("Barcode", 130), ("scanner", 180), ("12", 250))]
    assert heading_columns(body) == set()
    one = body + [scan_box("Qty", 10, 200), scan_box("7", 40, 200)]
    assert heading_columns(one) == set()


def test_scan_with_an_unparseable_heading_row_flags_every_covered_barcode(monkeypatch, tmp_path):
    """D179: no silent empty barcode when the heading is found only in the OCR words."""
    rows = [(G1, "Soap", "1", "5.00"), ("####", "Towel", "1", "5.00"), ("####", "Brush", "1", "25.00")]
    result = run_scan(monkeypatch, tmp_path, stacked_scan(rows, body_barcode=True),
                      [(G1, "Soap", "5.00"), (None, "Towel", "5.00"), (None, "Brush", "25.00")])
    assert gtin_codes(result) == ["read", "not_read", "not_read"]
    assert len(result["evidence"]["lines"]) == 3
    assert not any(k.startswith("_") for k in result["invoice"])


def test_scan_with_only_a_body_word_barcode_adds_no_line_flags(monkeypatch, tmp_path):
    boxes = [scan_box(t, x, 140 + 20 * n) for n in range(3) for t, x in ((str(n + 1), 10), ("Item", 50), ("5.00", 300))]
    boxes += [scan_box("Barcode", 130, 300), scan_box("scanner", 180, 300), scan_box("12", 250, 300)]
    result = run_scan(monkeypatch, tmp_path, boxes,
                      [(G1, "Soap", "5.00"), (None, "Towel", "5.00"), (None, "Brush", "25.00")])
    assert gtin_codes(result)[1:] == [None, None]
    assert not any((cell.get("review") or {}).get("code") == "not_read"
               for row in result["evidence"]["lines"] for cell in row.values())


def test_a_failed_column_read_is_traced_not_silent(monkeypatch, tmp_path):
    import app.docling_extract as docling_extract

    def boom(*args, **kwargs):
        raise ValueError("synthetic")
    monkeypatch.setattr(docling_extract, "line_columns", boom)
    rows = [(G1, "Soap", "1", "5.00"), ("####", "Towel", "1", "5.00"), ("####", "Brush", "1", "25.00")]
    result = run_scan(monkeypatch, tmp_path, stacked_scan(rows),
                      [(G1, "Soap", "5.00"), (None, "Towel", "5.00"), (None, "Brush", "25.00")])
    assert {"engine": "guard_printed", "status": "columns_failed", "reason": "ValueError"} in result["trace"]
