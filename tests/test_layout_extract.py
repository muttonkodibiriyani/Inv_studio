from __future__ import annotations

from decimal import Decimal

from app.layout_extract import extract_invoice
from app.matching import validate
from app.models import Invoice, Policy


def _box(
    text: str,
    x: float,
    y: float,
    *,
    page: int = 1,
    width: float | None = None,
) -> dict[str, object]:
    word_width = width if width is not None else max(12.0, len(text) * 6.0)
    return {
        "text": text,
        "page": page,
        "box": [x, y, x + word_width, y + 10.0],
        "size": [600.0, 800.0],
    }


def _table_header(*, page: int, y: float, item_label: str = "Item No") -> list[dict[str, object]]:
    return [
        _box(item_label, 18, y, page=page, width=54),
        _box("Description", 135, y, page=page, width=70),
        _box("UOM", 285, y, page=page, width=30),
        _box("Qty", 345, y, page=page, width=25),
        _box("Unit Price", 410, y, page=page, width=62),
    ]


def _table_row(
    sku: str,
    description: str,
    uom: str,
    qty: str,
    price: str,
    *,
    page: int,
    y: float,
) -> list[dict[str, object]]:
    return [
        _box(sku, 18, y, page=page, width=72),
        _box(description, 135, y, page=page, width=135),
        _box(uom, 285, y, page=page, width=30),
        _box(qty, 345, y, page=page, width=25),
        _box(price, 410, y, page=page, width=50),
    ]


def test_extracts_labeled_fields_and_multipage_spatial_lines() -> None:
    text = """TAX INVOICE
Supplier: Example Supply House
Bill To: Synthetic Buyer Company
Invoice No: INV-00073
Document Date: 14-JUL-2026
Ref: LPO- 00045678
Currency: AED
Net Amount: 25.00
Tax Amount: 1.25
Total Amount Including VAT: 26.25
"""
    boxes = [
        *_table_header(page=1, y=190),
        *_table_row("SAMPLE-A01", "Synthetic hand soap", "EA", "2", "4.50", page=1, y=215),
        *_table_row("00004567", "Synthetic paper roll", "PK", "1.5", "8.00", page=1, y=240),
        *_table_header(page=2, y=170),
        *_table_row("SAMPLE-C03", "Synthetic floor pad", "EA", "4", "2.00", page=2, y=195),
    ]

    result = extract_invoice(text, boxes)

    invoice = Invoice.model_validate(result)
    assert invoice.number == "INV-00073"
    assert invoice.supplier_name == "Example Supply House"
    assert invoice.buyer_name == "Synthetic Buyer Company"
    assert invoice.po == "00045678"
    assert invoice.date == "2026-07-14"
    assert invoice.currency == "AED"
    assert invoice.net == Decimal("25.00")
    assert invoice.tax == Decimal("1.25")
    assert invoice.seller is None
    assert invoice.site is None
    assert invoice.buyer is None
    assert invoice.location is None
    assert invoice.taxCode is None
    assert invoice.origin is None
    assert invoice.market is None
    assert [line.sku for line in invoice.lines] == ["SAMPLE-A01", "00004567", "SAMPLE-C03"]
    assert invoice.lines[1].qty == Decimal("1.5")
    assert invoice.lines[1].price == Decimal("8.00")
    assert invoice.lines[2].page == 2
    assert "SAMPLE-C03" in (invoice.lines[2].evidence or "")


def test_extracts_scan_style_row_with_ean_continuation_and_explicit_currency() -> None:
    text = """Tax Invoice
Document No: TX-0091
Document Date: 2026-08-19
LPO No: 00009001
Sub Total: 12.50
VAT Amount: 0.63
Total in AED: 13.13
"""
    boxes = [
        *_table_header(page=1, y=220, item_label="Item Code"),
        *_table_row("SYN-COS-01", "Sample face cream", "PCS", "1", "12.50", page=1, y=250),
        _box("EAN: 00012345678905", 135, 264, page=1, width=120),
    ]

    result = extract_invoice(text, boxes)

    invoice = Invoice.model_validate(result)
    assert invoice.number == "TX-0091"
    assert invoice.po == "00009001"
    assert invoice.date == "2026-08-19"
    assert invoice.currency == "AED"
    assert invoice.net == Decimal("12.50")
    assert invoice.tax == Decimal("0.63")
    assert len(invoice.lines) == 1
    assert invoice.lines[0].sku == "SYN-COS-01"
    assert invoice.lines[0].gtin == "00012345678905"
    assert invoice.lines[0].description == "Sample face cream"
    assert "EAN: 00012345678905" in (invoice.lines[0].evidence or "")


def test_preserves_nonsequential_rows_without_inventing_a_missing_item() -> None:
    text = "TAX INVOICE\nInvoice No: SAMPLE-4\n"
    boxes = [
        *_table_header(page=1, y=100),
        _box("1", 2, 125, width=8),
        *_table_row("CODE-01", "First sample item", "EA", "1", "1.00", page=1, y=125),
        _box("2", 2, 150, width=8),
        *_table_row("CODE-02", "Second sample item", "EA", "1", "2.00", page=1, y=150),
        _box("4", 2, 175, width=8),
        *_table_row("CODE-04", "Fourth sample item", "EA", "1", "4.00", page=1, y=175),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert [line.sku for line in invoice.lines] == ["CODE-01", "CODE-02", "CODE-04"]
    assert len(invoice.lines) == 3


def test_rejects_purchase_order_even_when_it_mentions_invoice_fields() -> None:
    text = """PURCHASE ORDER
PO No: 00004012
Invoice No: SUPPLIER-REFERENCE
Please send a Tax Invoice with the delivered goods.
"""

    assert extract_invoice(text, []) is None


def test_requires_a_strong_invoice_heading() -> None:
    text = "Invoice No: INV-44\nDocument Date: 2026-02-01\nNet Amount: 9.00"

    assert extract_invoice(text, []) is None


def test_conflicting_labels_and_ambiguous_date_are_left_unset() -> None:
    text = """TAX INVOICE
Invoice No: INV-A
Invoice No: INV-B
Date: 02/03/2026
Net Amount: 10.00
Net Amount: 12.00
Tax Amount: 0.50
"""

    invoice = Invoice.model_validate(extract_invoice(text, []))

    assert invoice.number is None
    assert invoice.date is None
    assert invoice.net is None
    assert invoice.tax == Decimal("0.50")


def test_does_not_infer_totals_currency_or_internal_codes_from_line_arithmetic() -> None:
    text = "TAX INVOICE\nInvoice No: SAFE-001\n"
    boxes = [
        *_table_header(page=1, y=100),
        *_table_row("00000123", "Sample item", "EA", "2", "5.00", page=1, y=125),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert invoice.lines[0].sku == "00000123"
    assert invoice.net is None
    assert invoice.tax is None
    assert invoice.currency is None
    assert invoice.taxCode is None


def test_ignores_malformed_boxes_and_non_numeric_item_rows() -> None:
    text = "TAX INVOICE\nInvoice No: SAFE-002\nTax Amount: 0.00\n"
    boxes: list[dict[str, object]] = [
        {"text": "broken"},
        {"text": "bad", "page": "one", "box": [1, 2], "size": [600, 800]},
        *_table_header(page=1, y=100),
        *_table_row("CODE-X", "Not a priced row", "EA", "two", "=1+1", page=1, y=125),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert invoice.number == "SAFE-002"
    assert invoice.lines == []


def test_extracts_header_fields_without_boxes() -> None:
    text = """COMMERCIAL INVOICE
Invoice #: CI-0008
Invoice Date: 31/12/2026
PO Number: 00007770
Currency: USD
Net Amount 1,234.50
VAT Amount 0.00
"""

    invoice = Invoice.model_validate(extract_invoice(text, []))

    assert invoice.number == "CI-0008"
    assert invoice.date == "2026-12-31"
    assert invoice.po == "00007770"
    assert invoice.currency == "USD"
    assert invoice.net == Decimal("1234.50")
    assert invoice.tax == Decimal("0.00")
    assert invoice.lines == []


def test_accepts_credit_invoice_heading_and_inline_labeled_date() -> None:
    text = """CREDIT INVOICE
Invoice No: CR-0004
Ship To: Sample Branch    Date: 17-SEP-2026
Tax Amount: 0.00
"""

    invoice = Invoice.model_validate(extract_invoice(text, []))

    assert invoice.number == "CR-0004"
    assert invoice.date == "2026-09-17"


def test_reads_one_prominent_number_beside_heading_and_lpo_without_colon() -> None:
    text = """Tax Invoice
Document Date 22-OCT-2026
LPO No 00008123
Tax Amount 0.00
"""
    boxes = [
        _box("Tax Invoice", 30, 80, width=100),
        _box("SYN-DXB-00017", 260, 80, width=120),
        *_table_header(page=1, y=180, item_label="Item Code"),
        *_table_row(
            "SYN-01",
            "Sample appliance EAN: 00012345678905",
            "EA",
            "1",
            "9.50",
            page=1,
            y=205,
        ),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert invoice.number == "SYN-DXB-00017"
    assert invoice.po == "00008123"
    assert invoice.date == "2026-10-22"
    assert invoice.lines[0].gtin == "00012345678905"
    assert invoice.lines[0].description == "Sample appliance"


def test_reads_split_scan_headers_wrapped_identifiers_and_printed_amounts() -> None:
    text = """TAX INVOICE
Invoice No: SCAN-001
Document Date: 01-MAY-2026
Total in AED
"""
    boxes = [
        _box("LPO", 410, 65, width=35),
        _box("No", 450, 65, width=20),
        _box("00081234", 480, 65, width=65),
        _box("Unit", 405, 90, width=38),
        _box("VAT", 640, 90, width=28),
        _box("Item", 30, 105, width=35),
        _box("Code", 70, 105, width=35),
        _box("Description", 145, 105, width=80),
        _box("Uom", 285, 105, width=30),
        _box("Qty", 345, 105, width=25),
        _box("Price", 405, 105, width=38),
        _box("Value", 455, 105, width=38),
        _box("Disc", 495, 105, width=30),
        _box("Sub", 530, 105, width=25),
        _box("Total", 558, 105, width=35),
        _box("Rate", 600, 105, width=30),
        _box("Amt", 640, 105, width=28),
        _box("Total", 690, 105, width=35),
        _box("SRK-", 30, 137, width=40),
        _box("SYNTH-01", 75, 137, width=70),
        _box("EAN", 260, 137, width=28),
        _box(":", 290, 137, width=5),
        _box("00012345678905", 175, 137, width=80),
        _box("Synthetic item", 145, 137, width=100),
        _box("EA", 290, 137, width=20),
        _box("3.00", 345, 137, width=30),
        _box("999.00", 405, 137, width=45),
        _box("2", 548, 137, width=5),
        _box(",", 554, 137, width=4),
        _box("997.00", 560, 137, width=40),
        _box("5", 605, 137, width=5),
        _box("%", 612, 137, width=5),
        _box("149.85", 640, 137, width=40),
        _box("Total in AED", 15, 175, width=90),
        _box("Total", 285, 175, width=35),
        _box("3.00", 345, 175, width=30),
        _box("2", 548, 175, width=5),
        _box(",", 554, 175, width=4),
        _box("997.00", 560, 175, width=40),
        _box("149.85", 640, 175, width=40),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert invoice.po == "00081234"
    assert invoice.currency == "AED"
    assert invoice.net == Decimal("2997.00")
    assert invoice.tax == Decimal("149.85")
    assert len(invoice.lines) == 1
    assert invoice.lines[0].sku == "SRK-SYNTH-01"
    assert invoice.lines[0].gtin == "00012345678905"
    assert invoice.lines[0].description == "Synthetic item"
    assert invoice.lines[0].qty == Decimal("3.00")
    assert invoice.lines[0].uom == "EA"
    assert invoice.lines[0].price == Decimal("999.00")
    assert invoice.lines[0].net_amount == Decimal("2997.00")
    assert invoice.lines[0].tax_amount == Decimal("149.85")


def test_handles_one_ocr_box_for_item_code_and_description_headers() -> None:
    text = "TAX INVOICE\nInvoice No: MERGED-001\n"
    boxes = [
        _box("S.No Item Code Description", 18, 100, width=210),
        _box("Uom", 285, 100, width=30),
        _box("Qty", 345, 100, width=25),
        _box("Price", 410, 100, width=45),
        _box("Value", 470, 100, width=40),
        *_table_row("SYN-MERGED-1", "Merged heading item", "EA", "2", "5.00", page=1, y=125),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert len(invoice.lines) == 1
    assert invoice.lines[0].sku == "SYN-MERGED-1"
    assert invoice.lines[0].description == "Merged heading item"


def test_extracts_name_only_line_without_treating_serial_number_as_sku() -> None:
    text = "TAX INVOICE\nInvoice No: NAME-ONLY-001\n"
    boxes = [
        _box("S.No", 18, 100, width=30),
        _box("Product Name", 100, 100, width=110),
        _box("Quantity", 345, 100, width=55),
        _box("Unit Price", 430, 100, width=65),
        _box("1", 18, 125, width=8),
        _box("Synthetic consulting service", 100, 125, width=190),
        _box("2", 345, 125, width=12),
        _box("12.50", 430, 125, width=40),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert len(invoice.lines) == 1
    assert invoice.lines[0].sku is None
    assert invoice.lines[0].gtin is None
    assert invoice.lines[0].description == "Synthetic consulting service"
    assert invoice.lines[0].qty == Decimal("2")
    assert invoice.lines[0].price == Decimal("12.50")

    validation = validate(
        invoice,
        {
            "version": "synthetic-v1",
            "sites": [],
            "routes": [],
            "orders": [],
            "items": [],
            "taxRules": [],
            "receipts": [],
        },
        Policy(),
        reviewed=True,
    )
    assert validation["ready"] is False
    assert any(
        issue["code"] == "ITEM" and issue["line"] == 1
        for issue in validation["issues"]
    )


def test_reads_aligned_compact_header_and_explicit_total_currency_and_tax() -> None:
    text = """TAX INVOICE
Net Amount 24.00
Tax (5%) 1.20
Total AED 25.20
"""
    boxes = [
        _box("Warehouse", 20, 40, width=60),
        _box("No.", 85, 40, width=20),
        _box("8", 112, 40, width=8),
        _box("No.", 400, 90, width=20),
        _box("SYN-00797", 490, 90, width=70),
        _box("Date", 400, 110, width=30),
        _box("03/09/2026", 490, 110, width=70),
        _box("PO", 400, 130, width=18),
        _box("No.", 420, 130, width=20),
        _box("14440000", 490, 130, width=60),
        _box("Due", 400, 150, width=25),
        _box("Date", 430, 150, width=30),
        _box("03/10/2026", 490, 150, width=70),
        _box("Receiver", 200, 300, width=55),
        _box("Date", 300, 300, width=30),
    ]

    invoice = Invoice.model_validate(extract_invoice(text, boxes))

    assert invoice.number == "SYN-00797"
    assert invoice.date == "2026-09-03"
    assert invoice.po == "14440000"
    assert invoice.currency == "AED"
    assert invoice.net == Decimal("24.00")
    assert invoice.tax == Decimal("1.20")


def test_accepts_description_column_before_item_code() -> None:
    boxes = [
        _box("Description", 40, 100, width=100),
        _box("SKU", 250, 100, width=35),
        _box("Quantity", 345, 100, width=55),
        _box("Unit Price", 430, 100, width=65),
        _box("Synthetic service", 40, 125, width=150),
        _box("SYN-1", 250, 125, width=45),
        _box("2", 345, 125, width=12),
        _box("4.00", 430, 125, width=35),
    ]

    invoice = Invoice.model_validate(extract_invoice("TAX INVOICE", boxes))

    assert len(invoice.lines) == 1
    assert invoice.lines[0].sku == "SYN-1"
    assert invoice.lines[0].description == "Synthetic service"
    assert invoice.lines[0].qty == Decimal("2")
    assert invoice.lines[0].price == Decimal("4.00")


def test_reads_split_number_ordinal_date_supplier_and_labeled_totals() -> None:
    boxes = [
        _box("Supplier:", 20, 20, width=50),
        _box("Tax", 400, 20, width=25),
        _box("Invoice", 430, 20, width=45),
        _box("Synthetic", 20, 36, width=60),
        _box("Trading", 84, 36, width=45),
        _box("LLC", 133, 36, width=25),
        _box("Tax", 400, 52, width=25),
        _box("Invoice", 430, 52, width=45),
        _box("Number", 480, 52, width=50),
        _box(":", 534, 52, width=5),
        _box("SYN-", 543, 52, width=35),
        _box("009", 580, 52, width=25),
        _box("Date", 400, 68, width=30),
        _box(":", 434, 68, width=5),
        _box("14th", 443, 68, width=28),
        _box("July", 475, 68, width=28),
        _box("2026", 507, 68, width=35),
        _box("SUBTOTAL", 400, 100, width=65),
        _box("AED", 470, 100, width=25),
        _box("120.00", 500, 100, width=45),
        _box("VAT", 400, 116, width=25),
        _box("-5%", 429, 116, width=25),
        _box("AED", 470, 116, width=25),
        _box("6.00", 500, 116, width=35),
    ]

    invoice = Invoice.model_validate(extract_invoice("TAX INVOICE", boxes))

    assert invoice.number == "SYN-009"
    assert invoice.date == "2026-07-14"
    assert invoice.supplier_name == "Synthetic Trading LLC"
    assert invoice.net == Decimal("120.00")
    assert invoice.tax == Decimal("6.00")


def test_next_label_after_invoice_number_label_is_not_the_number():
    import re

    from app.layout_extract import _unique_identifier

    pattern = re.compile(r"(?i)\b(?:invoice|inv|document)\s*(?:no\.?|number|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9./_-]{0,79})")
    assert _unique_identifier(["Tax Invoice No.  Tax Invoice Date  Currency"], (pattern,)) is None
    assert _unique_identifier(["Tax Invoice No. SYN-4401"], (pattern,)) == "SYN-4401"
