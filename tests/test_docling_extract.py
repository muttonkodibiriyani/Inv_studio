from types import SimpleNamespace

from app.docling_extract import (
    document_payload,
    extract_invoice_from_tables,
    normalize_bbox,
)


def ns(**values):
    return SimpleNamespace(**values)


def test_bottom_left_bbox_uses_real_page_height():
    box = ns(l=10, t=90, r=40, b=70, coord_origin="BOTTOMLEFT")
    assert normalize_bbox(box, [100, 120]) == ([10, 30, 40, 50], "BOTTOMLEFT")
    assert normalize_bbox(box, None) == (None, "BOTTOMLEFT")


def test_document_payload_preserves_spans_cells_and_block_provenance():
    block = type("TextItem", (), {})()
    block.text = "Measured block"
    block.prov = [ns(page_no=1, bbox=ns(l=5, t=95, r=25, b=85, coord_origin="BOTTOMLEFT"))]
    table = type("TableItem", (), {})()
    table.text = ""
    table.prov = [ns(page_no=1, bbox=ns(l=0, t=80, r=100, b=20, coord_origin="BOTTOMLEFT"))]
    table.data = ns(
        num_rows=2,
        num_cols=2,
        table_cells=[
            ns(text="Header", start_row_offset_idx=0, start_col_offset_idx=0, row_span=1,
               col_span=2, column_header=True,
               bbox=ns(l=0, t=80, r=100, b=60, coord_origin="BOTTOMLEFT")),
            ns(text="Value", start_row_offset_idx=1, start_col_offset_idx=0, row_span=1,
               col_span=1, column_header=False,
               bbox=ns(l=0, t=60, r=50, b=20, coord_origin="BOTTOMLEFT")),
        ],
    )

    class Document:
        pages = {1: ns(size=ns(width=100, height=100))}

        @staticmethod
        def iterate_items():
            return iter(((block, 0), (table, 0)))

        @staticmethod
        def export_to_markdown():
            return "source markdown"

    text, boxes, tables = document_payload(Document())

    assert text == "source markdown"
    assert boxes[0] == {
        "text": "Measured block",
        "page": 1,
        "box": [5, 5, 25, 15],
        "size": [100.0, 100.0],
        "coordinate_system": "top-left",
        "source_coordinate_system": "BOTTOMLEFT",
        "geometry": "block",
        "source_type": "TextItem",
        "estimated": False,
    }
    assert tables[0]["rows"] == [["Header", "Header"], ["Value", None]]
    assert tables[0]["cells"][0]["column_span"] == 2
    assert tables[0]["cells"][1]["box"] == [0, 40, 50, 80]


def test_document_payload_prefers_measured_word_cells_over_blocks():
    block = type("TextItem", (), {})()
    block.text = "whole paragraph"
    block.prov = [ns(page_no=1, bbox=ns(l=0, t=90, r=100, b=70, coord_origin="BOTTOMLEFT"))]

    class Document:
        pages = {1: ns(size=ns(width=100, height=100))}

        @staticmethod
        def iterate_items():
            return iter(((block, 0),))

        @staticmethod
        def export_to_markdown():
            return "whole paragraph"

    rect = ns(
        r_x0=10, r_y0=80, r_x1=30, r_y1=80,
        r_x2=30, r_y2=70, r_x3=10, r_y3=70, coord_origin="BOTTOMLEFT",
    )
    parsed_pages = [ns(
        size=ns(width=100, height=100),
        parsed_page=ns(word_cells=[ns(text="word", rect=rect, confidence=0.98, from_ocr=False)]),
    )]

    _text, boxes, _tables = document_payload(Document(), parsed_pages)
    assert boxes == [{
        "text": "word", "page": 1, "box": [10, 20, 30, 30], "size": [100.0, 100.0],
        "coordinate_system": "top-left", "source_coordinate_system": "BOTTOMLEFT",
        "geometry": "word", "source_type": "TextCell", "estimated": False,
        "confidence": 0.98, "from_ocr": False,
    }]


def test_semantic_tables_handle_repeated_headers_and_page_continuation():
    header = [
        "Product Reference", "Product Description", "Barcode", "Qty (In PCE)",
        "UOM", "Selling Price", "Taxable Value", "Tax Amount",
    ]
    first = ["ITEM-1", "First product", "0123456789012", "2", "EA", "1.250", "2.500", "0.125"]
    second = ["ITEM-2", "Second product", "0987654321098", "3", "PCS", "2.000", "6.000", "0.300"]
    tables = [
        {"page": 1, "rows": [header, first]},
        {"page": 2, "rows": [header, second, ["Net amount", None, None, None, None, None, "8.500", None]]},
        {"page": 2, "rows": [["VAT amount", "0.425"]]},
    ]

    result = extract_invoice_from_tables(
        "Invoice No: INV-100\nInvoice Date: 04/10/2026\nPurchase Order: PO-9\nCurrency: AED",
        tables,
    )

    assert result is not None
    assert result["number"] == "INV-100"
    assert result["po"] == "PO-9"
    assert result.get("date") is None
    assert result["date_printed"] == "04/10/2026"
    assert result["currency"] == "AED"
    assert result["net"] == "8.500"
    assert result["tax"] == "0.425"
    assert result["lines"] == [
        {
            "sku": "ITEM-1", "description": "First product", "gtin": "0123456789012",
            "qty": "2", "uom": "EA", "price": "1.250", "net_amount": "2.500",
            "tax_amount": "0.125", "page": 1, "evidence": "table page 1 row 2",
        },
        {
            "sku": "ITEM-2", "description": "Second product", "gtin": "0987654321098",
            "qty": "3", "uom": "PCS", "price": "2.000", "net_amount": "6.000",
            "tax_amount": "0.300", "page": 2, "evidence": "table page 2 row 2",
        },
    ]


def test_explicit_repeated_header_table_beats_captions_and_ranked_currency_totals():
    document_header = [
        "Tax Invoice No.", "Tax Invoice Date", "Currency", "Customer PO No",
    ]
    document_values = ["INV-200", "05.10.2026", "AED", "PO-200"]
    line_header = ["Product Reference", "Product Description", "Qty", "Selling Price"]
    line = ["SKU-200", "Visible product", "2", "4.50"]
    tables = [
        {"page": 1, "rows": [document_header, document_values]},
        {"page": 1, "rows": [line_header, line]},
        {"page": 2, "rows": [document_header, document_values]},
        {"page": 2, "rows": [
            [None, "Net Amount", "9.00"],
            [None, "VAT Amount", "0.45"],
            [None, "VAT Amount in AED", "0.00"],
        ]},
        {"page": 2, "rows": [["Invoice", "To Ship To"], ["Currency", "DUE"]]},
    ]

    result = extract_invoice_from_tables(
        "Invoice\nTo Ship To\nPurchase Order BOX: 999\nCurrency DUE",
        tables,
    )

    assert result is not None
    assert {key: result.get(key) for key in ("number", "date", "currency", "po", "net", "tax")} == {
        "number": "INV-200",
        "date": "2026-10-05",
        "currency": "AED",
        "po": "PO-200",
        "net": "9.00",
        "tax": "0.45",
    }


def test_conflicting_repeated_table_headers_are_left_unset():
    line_table = {
        "page": 1,
        "rows": [
            ["Product Reference", "Product Description", "Qty", "Selling Price"],
            ["SKU-1", "Visible product", "1", "2.00"],
        ],
    }
    tables = [
        {"page": 1, "rows": [["Tax Invoice No.", "Currency"], ["INV-1", "AED"]]},
        line_table,
        {"page": 2, "rows": [["Tax Invoice No.", "Currency"], ["INV-2", "AED"]]},
    ]

    result = extract_invoice_from_tables("Tax Invoice", tables)

    assert result is not None
    assert result.get("number") is None
    assert result["currency"] == "AED"


def test_table_without_semantic_identity_is_not_invented():
    tables = [{"page": 1, "rows": [["Quantity", "Unit price"], ["4", "9.99"]]}]
    assert extract_invoice_from_tables("", tables) is None


def test_name_only_table_preserves_description_and_never_uses_serial_as_sku():
    tables = [{
        "page": 1,
        "rows": [
            ["S.No", "Product Name", "Quantity", "Unit Price"],
            ["1", "Synthetic consulting service", "3", "8.00"],
        ],
    }]

    result = extract_invoice_from_tables("Tax Invoice", tables)

    assert result is not None
    assert result["lines"] == [{
        "description": "Synthetic consulting service",
        "qty": "3",
        "price": "8.00",
        "page": 1,
        "evidence": "table page 1 row 2",
    }]


def test_measured_rows_replace_overlapping_structural_rows_on_same_page():
    structural = {
        "page": 1,
        "source": "docling",
        "rows": [
            ["Item Code", "Description", "Quantity", "Unit Price"],
            ["SYN-1\ncontinued", "Synthetic service", "2", "4.00"],
        ],
    }
    measured = {
        "page": 1,
        "source": "docling_word_cells",
        "rows": [
            ["sku", "description", "qty", "price"],
            ["SYN-1", "Synthetic service", "2", "4.00"],
        ],
    }

    result = extract_invoice_from_tables("Tax Invoice", [structural, measured])

    assert result is not None
    assert len(result["lines"]) == 1
    assert result["lines"][0]["sku"] == "SYN-1"


def test_incomplete_measured_rows_do_not_drop_structural_rows():
    structural = {
        "page": 1,
        "source": "docling",
        "rows": [
            ["Item Code", "Description", "Quantity", "Unit Price"],
            ["SYN-1", "First synthetic service", "2", "4.00"],
            ["SYN-2", "Second synthetic service", "1", "7.00"],
        ],
    }
    measured = {
        "page": 1,
        "source": "docling_word_cells",
        "rows": [
            ["sku", "description", "qty", "price"],
            ["SYN-1", "First synthetic service", "2", "4.00"],
        ],
    }

    result = extract_invoice_from_tables("Tax Invoice", [structural, measured])

    assert result is not None
    assert [line["sku"] for line in result["lines"]] == ["SYN-1", "SYN-2"]


def test_explicit_total_line_count_can_prove_measured_rows_complete():
    structural = {
        "page": 1,
        "source": "docling",
        "rows": [
            ["Item Code", "Description", "Quantity", "Unit Price"],
            ["SYN-2", "Second synthetic service", "99", "7.00"],
        ],
    }
    measured = {
        "page": 1,
        "source": "docling_word_cells",
        "rows": [
            ["sku", "description", "qty", "price"],
            ["SYN-1", "First synthetic service", "2", "4.00"],
            ["SYN-2", "Second synthetic service", "1", "7.00"],
        ],
    }

    result = extract_invoice_from_tables(
        "Tax Invoice\nTotal Lines: 2 Total Qty: 3",
        [structural, measured],
    )

    assert result is not None
    assert [line["sku"] for line in result["lines"]] == ["SYN-1", "SYN-2"]


def test_merged_serial_item_description_header_maps_visible_columns():
    tables = [{
        "page": 1,
        "source": "docling",
        "rows": [
            ["Bill To", "Synthetic Buyer Company P.O.Box: 100", None, None, None, None, None, None, None],
            [
                "S.No Item Code Description", "S.No Item Code Description", None,
                "Uom", "Qty", "Unit Price", "Sub Total", "VAT Amt", "Total",
            ],
            ["1", "SYN-1", "Synthetic service", "EA", "2", "4.00", "8.00", "0.40", "8.40"],
        ],
    }]

    result = extract_invoice_from_tables("Tax Invoice", tables)

    assert result is not None
    assert result["buyer_name"] == "Synthetic Buyer Company"
    assert result["lines"] == [{
        "sku": "SYN-1",
        "description": "Synthetic service",
        "uom": "EA",
        "qty": "2",
        "price": "4.00",
        "net_amount": "8.00",
        "tax_amount": "0.40",
        "page": 1,
        "evidence": "table page 1 row 3",
    }]


def test_measured_words_recover_borderless_semantic_table():
    header = [
        ("Product", 10), ("Reference", 20), ("Product", 40), ("Description", 52),
        ("Barcode", 72), ("Qty", 88), ("Selling", 100), ("Price", 110),
        ("Taxable", 125), ("Value", 136), ("Tax", 148), ("Rate", 155),
        ("Tax", 168), ("Amount", 177),
    ]
    data = [
        ("SKU-1", 15), ("Visible", 45), ("item", 55), ("0123456789012", 72),
        ("2", 88), ("1.250", 106), ("2.500", 131), ("5.00", 152), ("0.125", 173),
    ]
    boxes = []
    for text, x in header:
        boxes.append({"text": text, "page": 1, "box": [x - 3, 10, x + 3, 16], "size": [200, 100]})
    for text, x in data:
        boxes.append({"text": text, "page": 1, "box": [x - 3, 22, x + 3, 28], "size": [200, 100]})

    result = extract_invoice_from_tables("Tax Invoice", [], boxes=boxes)

    assert result is not None
    assert result["lines"] == [{
        "sku": "SKU-1", "description": "Visible item", "gtin": "0123456789012",
        "qty": "2", "price": "1.250", "net_amount": "2.500", "tax_amount": "0.125",
        "page": 1, "evidence": "table page 1 row 2",
    }]


def test_measured_size_column_does_not_contaminate_quantity_and_keeps_partial_line():
    header = [
        ("Description", 15), ("SKU", 65), ("Size", 95),
        ("Quantity", 125), ("Unit Price", 160), ("Amount", 195),
    ]
    data = [
        ("Synthetic item", 15), ("SYN-1", 65), ("125ML", 95),
        ("unreadable", 125), ("4.00", 160), ("12.00", 195),
    ]
    boxes = [
        {"text": value, "page": 1, "box": [x - 4, y, x + 4, y + 6], "size": [220, 100]}
        for y, row in ((10, header), (22, data))
        for value, x in row
    ]

    result = extract_invoice_from_tables("Tax Invoice", [], boxes=boxes)

    assert result is not None
    assert result["lines"] == [{
        "description": "Synthetic item", "sku": "SYN-1", "price": "4.00",
        "net_amount": "12.00", "page": 1, "evidence": "table page 1 row 2",
    }]


def _two_amount_boxes(amounts, net_amounts=("50.00", "60.00"), taxes=("2.25", "2.50")):
    """A borderless table printing a pre-discount net and, after it, the amount charged."""
    header = [
        ("SN", 6), ("Item", 16), ("Code", 24), ("Description", 50), ("Qty", 88), ("UOM", 98),
        ("Unit", 108), ("Price", 116), ("Net", 128), ("Amount", 137), ("VAT", 150), ("5%", 157),
    ]
    if amounts is not None:
        header += [("Amount", 172), ("AED", 181)]
    boxes = [{"text": text, "page": 1, "box": [x - 3, 10, x + 3, 16], "size": [200, 100]} for text, x in header]
    rows = [
        [("1", 6), ("SKU-1", 20), ("First", 45), ("item", 55), ("2", 88), ("Nos", 98), ("25.00", 112),
         (net_amounts[0], 132), (taxes[0], 153)],
        [("2", 6), ("SKU-2", 20), ("Second", 45), ("item", 55), ("3", 88), ("Nos", 98), ("20.00", 112),
         (net_amounts[1], 132), (taxes[1], 153)],
    ]
    for index, row in enumerate(rows):
        if amounts is not None:
            row.append((amounts[index], 176))
        top = 22 + index * 10
        boxes += [{"text": text, "page": 1, "box": [x - 3, top, x + 3, top + 6], "size": [200, 100]}
                  for text, x in row]
    return boxes


def test_second_amount_column_is_taken_when_it_reconciles_with_the_totals():
    # Net Amount is the pre-discount value; Amount AED sums to the printed Net Total and
    # carries the 5% VAT, so it is the line net. Nothing else on the row moves.
    text = "Tax Invoice\nTotal 110.00\nDiscount 15.00\nNet Total 95.00\nVAT 5% 4.75\nGross Total 99.75\n"
    result = extract_invoice_from_tables(text, [], boxes=_two_amount_boxes(("45.00", "50.00")))

    assert result is not None
    assert [line["net_amount"] for line in result["lines"]] == ["45.00", "50.00"]
    assert [line["tax_amount"] for line in result["lines"]] == ["2.25", "2.50"]
    assert [line["price"] for line in result["lines"]] == ["25.00", "20.00"]
    assert result["net"] == "95.00"
    assert all("_other_amount" not in line for line in result["lines"])


def test_second_amount_column_is_ignored_when_it_does_not_reconcile():
    # The printed Net Total is the sum of the Net Amount column, so that column stays.
    text = "Tax Invoice\nNet Total 110.00\nVAT 5% 5.50\nGross Total 115.50\n"
    boxes = _two_amount_boxes(("45.00", "50.00"), taxes=("2.50", "3.00"))
    result = extract_invoice_from_tables(text, [], boxes=boxes)

    assert result is not None
    assert [line["net_amount"] for line in result["lines"]] == ["50.00", "60.00"]
    assert result["net"] == "110.00"


def test_second_amount_column_is_ignored_when_line_vat_does_not_follow_it():
    # The sums agree by coincidence but the VAT is charged on the Net Amount column.
    text = "Tax Invoice\nNet Total 110.00\nVAT 5% 5.50\n"
    boxes = _two_amount_boxes(("40.00", "70.00"), taxes=("2.50", "3.00"))
    result = extract_invoice_from_tables(text, [], boxes=boxes)

    assert result is not None
    assert [line["net_amount"] for line in result["lines"]] == ["50.00", "60.00"]


def test_single_amount_column_is_unchanged_by_the_reconciliation_rule():
    text = "Tax Invoice\nNet Total 110.00\nVAT 5% 5.50\n"
    result = extract_invoice_from_tables(text, [], boxes=_two_amount_boxes(None, taxes=("2.50", "3.00")))

    assert result is not None
    assert [line["net_amount"] for line in result["lines"]] == ["50.00", "60.00"]
    assert all("_other_amount" not in line for line in result["lines"])
