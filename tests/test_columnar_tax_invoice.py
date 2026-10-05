"""Columnar 'TAX INVOICE' layout read from generated PDFs. Invented parties, numbers and values only."""

from decimal import Decimal

import pytest

from app import columnar_tax_invoice as cti
from app.engines import native_pdf_quality
from app.models import Invoice
from app.ocr_worker import digital, structured_extract

canvas = pytest.importorskip("reportlab.pdfgen.canvas")

# Column x positions of the table: no, description, brand, qty, UOM, price, amount, VAT %, VAT, total.
X = (34, 56, 181, 233, 278, 331, 380, 447, 476, 534)
HEADER = [(X[0], "#"), (X[1], "Description"), (X[2], "Brand"), (X[3], "Quantity"), (X[4], "UOM"),
          (X[5], "Price (Excl. VAT),"), (X[6], "Amount (Excl. VAT),"), (X[7], "VAT,"), (X[8], "VAT Amount,"),
          (X[9], "Amount (Incl. VAT),")]
CURRENCY = [(X[5], "XYZ"), (X[6], "XYZ"), (X[8], "XYZ"), (X[9], "XYZ")]
FOOTER = [(30, "Made with Sample Ledger")]


def gtin(stem):
    return next(stem + str(d) for d in range(10) if cti.gs1_valid(stem + str(d)))


GTIN_A, GTIN_B, GTIN_C = gtin("400000000001"), gtin("400000000002"), gtin("400000000003")


def heading(number="ZZTI26-00000042", issued="March 7, 2026"):
    return [[(220, "TAX INVOICE")], [(31, f"# {number}")], [(31, f"Date of Issuing: {issued}")],
            [(31, "Date of Supply: March 7, 2026")], [(31, "Issued By:"), (300, "Issued To:")],
            [(31, "Example Trading LLC"), (300, "Sample Retail Co")],
            [(31, "Reference #: 13000042"), (200, "Reference Date:")]]


def item(no, desc, brand, qty, uom, price, amount, rate, vat, total):
    return [(X[0], str(no)), (X[1], desc), (X[2], brand), (X[3], qty), (X[4], uom), (X[5], price),
            (X[6], amount), (X[7], rate), (X[8], vat), (X[9], total)]


def totals(net, vat, total, words=True):
    first = [(36, "TOTAL OF SUPPLY: Invented amount in words")] if words else []
    return [first + [(379, f"Sub Total, XYZ: {net}")], [(379, f"Total VAT, XYZ: {vat}")],
            [(379, f"Total, XYZ: {total}")], [(30, "Terms and Conditions:")]]


def pdf(tmp_path, pages):
    path = tmp_path / "synthetic.pdf"
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    for rows in pages:
        y = 50
        for row in rows:
            for x, value in row:
                c.setFont("Helvetica", 7)
                c.drawString(x, 842 - y, value)
            y += 11
        c.showPage()
    c.save()
    return path


def read(path):
    text, boxes, tables = digital(path, True)
    return text, boxes, tables


def one_page():
    return [heading() + [HEADER, CURRENCY,
            item(1, "Hydra Gel Cream 50ml,", "NORTHLEAF", "3.000", "Pcs", "12.50", "37.50", "5", "1.88", "39.38"),
            [(X[1], GTIN_A), (X[2], "BEAUTY")],
            item(2, "Lip Tint Coral", "NORTHLEAF", "2.000", "Pcs", "8.25", "16.50", "5", "0.83", "17.33"),
            [(X[1], f"LTC-04, {GTIN_B}")]] + totals("54.00", "2.71", "56.71") + [FOOTER]]


def test_one_page_reads_header_lines_and_barcodes(tmp_path):
    text, boxes, tables = read(pdf(tmp_path, one_page()))
    invoice, rec = cti.extract(text, boxes)
    assert invoice["number"] == "ZZTI26-00000042"
    assert (invoice["supplier_name"], invoice["buyer_name"]) == ("Example Trading LLC", "Sample Retail Co")
    assert (invoice["date_printed"], invoice["date"]) == ("March 7, 2026", "2026-03-07")
    assert (invoice["currency"], invoice["net"], invoice["tax"]) == ("XYZ", Decimal("54.00"), Decimal("2.71"))
    first, second = invoice["lines"]
    assert (first["description"], first["gtin"], first["qty"], first["uom"]) == (
        "Hydra Gel Cream 50ml", GTIN_A, Decimal("3.000"), "Pcs")
    assert (first["price"], first["net_amount"], first["tax_amount"]) == (Decimal("12.50"), Decimal("37.50"),
                                                                          Decimal("1.88"))
    assert (second["description"], second["gtin"]) == ("Lip Tint Coral LTC-04", GTIN_B)
    assert all(line["reconciled"] for line in rec["lines"])
    assert rec["line_numbers_contiguous"] and rec["lines_sum_to_net"]
    assert rec["line_vat_sums_to_vat"] and rec["net_plus_vat_is_total"]
    parsed, method = structured_extract(text, boxes, [], tables)
    assert method == "columnar_tax_invoice" and parsed["po"] == "13000042"
    assert native_pdf_quality(Invoice.model_validate(parsed), text) == (True, [])


def test_table_continues_on_second_page_without_header(tmp_path):
    page1 = heading() + [HEADER, CURRENCY,
                         item(1, "Hydra Gel Cream 50ml,", "NORTHLEAF", "3.000", "Pcs", "12.50", "37.50", "5", "1.88",
                              "39.38"),
                         [(X[1], GTIN_A)], FOOTER]
    page2 = [item(2, "Body Oil Amber,", "NORTHLEAF", "1.000", "Pcs", "20.00", "20.00", "5", "1.00", "21.00"),
             [(X[1], GTIN_C), (X[2], "HOME")]] + totals("57.50", "2.88", "60.38", words=False) + [FOOTER]
    text, boxes, _ = read(pdf(tmp_path, [page1, page2]))
    invoice, rec = cti.extract(text, boxes)
    assert [(line["page"], line["gtin"]) for line in invoice["lines"]] == [(1, GTIN_A), (2, GTIN_C)]
    assert invoice["lines"][1]["description"] == "Body Oil Amber"
    assert all(line["reconciled"] for line in rec["lines"]) and rec["lines_sum_to_net"]


def test_non_reconciling_line_is_kept_as_printed_for_review(tmp_path):
    rows = one_page()
    rows[0][9] = item(1, "Hydra Gel Cream 50ml,", "NORTHLEAF", "3.000", "Pcs", "12.50", "37.00", "5", "1.85",
                      "38.85")
    text, boxes, _ = read(pdf(tmp_path, rows))
    invoice, rec = cti.extract(text, boxes)
    assert invoice["lines"][0]["net_amount"] == Decimal("37.00")
    assert [line["reconciled"] for line in rec["lines"]] == [False, True]
    assert not rec["lines_sum_to_net"]


def test_barcode_needs_a_valid_check_digit_and_may_carry_a_suffix():
    wrong = GTIN_A[:-1] + str((int(GTIN_A[-1]) + 1) % 10)
    assert cti._split_barcode(f"Serum 30ml, {wrong}") == (f"Serum 30ml, {wrong}", None)
    assert cti._split_barcode(f"Serum 30ml, {GTIN_A} - D") == ("Serum 30ml - D", GTIN_A)
    assert cti._split_barcode("6") == ("6", None)


def test_other_layouts_are_not_claimed(tmp_path):
    rows = [r for r in one_page()[0] if not (r and r[0][1].startswith("Date of Issuing"))]
    text, boxes, tables = read(pdf(tmp_path, [rows]))
    assert not cti.detect(text, boxes) and cti.extract(text, boxes) is None
    assert structured_extract(text, boxes, [], tables)[1] != "columnar_tax_invoice"
    plain = pdf(tmp_path, [[[(40, "INVOICE")], [(40, "Invoice No: INV-1")], [(40, "Total 10.00")]]])
    assert cti.extract(*read(plain)[:2]) is None


def ocr_style(boxes, scale=1.7):
    """Pixel coordinates and split-off punctuation, as an OCR reader returns words."""
    out = []
    for b in boxes:
        x0, top, x1, bottom = (v * scale for v in b["box"])
        text, height = b["text"], bottom - top
        if len(text) > 1 and text[-1] in ":,":
            cut = x1 - height * 0.3
            out += [{**b, "text": text[:-1], "box": [x0, top, cut - height * 0.5, bottom]},
                    {**b, "text": text[-1], "box": [cut, top, x1, bottom]}]
        else:
            out.append({**b, "box": [x0, top, x1, bottom]})
    return out


def test_ocr_words_with_split_punctuation_read_the_same(tmp_path):
    text, boxes, _ = read(pdf(tmp_path, one_page()))
    native, _ = cti.extract(text, boxes)
    split = ocr_style(boxes)
    assert any(b["text"] == ":" for b in split)
    ocr, rec = cti.extract(text, split)
    assert {k: v for k, v in ocr.items() if k != "lines"} == {k: v for k, v in native.items() if k != "lines"}
    assert [(line["gtin"], line["net_amount"]) for line in ocr["lines"]] == [
        (line["gtin"], line["net_amount"]) for line in native["lines"]]
    assert all(line["reconciled"] for line in rec["lines"])


def test_thousands_group_split_by_ocr_rejoins():
    row = [{"text": "1,", "page": 1, "x0": 0, "x1": 10, "top": 0, "bottom": 20},
           {"text": "234.50", "page": 1, "x0": 18, "x1": 60, "top": 0, "bottom": 20},
           {"text": "4006381333931", "page": 1, "x0": 90, "x1": 200, "top": 0, "bottom": 20}]
    assert [w["text"] for w in cti._join_split_tokens(row)] == ["1,234.50", "4006381333931"]
    barcode = [{**row[0], "text": "30,"}, {**row[2], "x0": 14}]
    assert [w["text"] for w in cti._join_split_tokens(barcode)] == ["30,", "4006381333931"]
