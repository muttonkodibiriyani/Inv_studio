"""Synthetic regressions for measured invoice column boundaries."""

import pytest

from app.docling_extract import extract_invoice_from_tables


def test_ocr_whitespace_does_not_create_extra_barcode_or_lose_vat_column():
    from app.docling_extract import _header_roles, _rows_from_words

    tokens = ["SN", " ", "Barcode", "/", "Part", "Description", "Qty", "Unit", "Price", "VAT", " ", "5", "%"]
    boxes = [word(token, 1, index * 22, 20) for index, token in enumerate(tokens)]
    roles = [role for role, _, _ in _header_roles(_rows_from_words(boxes)[0])]
    assert roles == ["serial", "part_code", "description", "qty", "price", "tax_amount"]


def test_logo_on_invoice_number_baseline_does_not_break_page_identity():
    from app.docling_extract import _measured_invoice_number, _rows_from_words

    boxes = [word("Synthetic Logo", 1, 8, 5), word("Invoice No:", 1, 150, 5),
             word("SYN-100", 1, 220, 5)]
    assert _measured_invoice_number(_rows_from_words(boxes), [320, 100]) == "SYN-100"
    boxes = [word("Original Invoice No: SYN-100", 1, 8, 5)]
    assert _measured_invoice_number(_rows_from_words(boxes), [320, 100]) is None


@pytest.mark.parametrize(("left", "expected", "split"), [(12, 16, True), (42, 16, False), (12, 15, False)])
def test_merged_serial_requires_column_crossing_and_literal_sequence(left, expected, split):
    from app.docling_extract import _column_bounds, _mapped_row

    roles = [("serial", 10, 20), ("part_code", 40, 80), ("description", 100, 160)]
    row = [word("16PR-001234567890", 1, left, 34, width=90-left)]
    values, mapped = _mapped_row(row, roles, _column_bounds(roles), expected)
    assert values[:2] == (["16", "PR-001234567890"] if split else [None, "16PR-001234567890"])
    assert bool(mapped.get("_repair")) == split


def word(text, page, left, top, *, width=18, size=(320, 100)):
    return {
        "text": text,
        "page": page,
        "box": [left, top, left + width, top + 6],
        "size": list(size),
    }


def measured_header(page=1, *, top=20, size=(320, 100)):
    return [
        word("SN", page, 8, top, width=12, size=size),
        word("Description", page, 38, top, width=42, size=size),
        word("Qty", page, 160, top, width=16, size=size),
        word("UOM", page, 188, top, width=18, size=size),
        word("Unit Price", page, 218, top, width=35, size=size),
        word("Net Amount", page, 270, top, width=42, size=size),
    ]


def measured_row(serial, description, page=1, *, top=34, size=(320, 100)):
    return [
        word(str(serial), page, 10, top, width=8, size=size),
        word(description, page, 40, top, width=105, size=size),
        word("2", page, 163, top, width=8, size=size),
        word("EA", page, 191, top, width=10, size=size),
        word("4.00", page, 224, top, width=20, size=size),
        word("8.00", page, 276, top, width=20, size=size),
    ]


def invoice_number(number, page, *, size=(320, 100)):
    return [word(f"Invoice No: {number}", page, 8, 5, width=90, size=size)]


def test_description_size_and_model_numbers_do_not_become_quantity():
    boxes = measured_header()
    boxes.extend([
        word("1", 1, 10, 34, width=8),
        word("Synthetic cleanser", 1, 40, 34, width=62),
        word("Model 125 ML", 1, 108, 34, width=37),
        word("7", 1, 163, 34, width=8),
        word("Nos", 1, 191, 34, width=13),
        word("12.50", 1, 224, 34, width=24),
        word("87.50", 1, 276, 34, width=24),
    ])

    result = extract_invoice_from_tables("Tax Invoice", [], boxes=boxes)

    assert result is not None
    assert result["lines"] == [{
        "description": "Synthetic cleanser Model 125 ML",
        "qty": "7",
        "uom": "Nos",
        "price": "12.50",
        "net_amount": "87.50",
        "page": 1,
        "evidence": "table page 1 row 2",
    }]


def test_polluted_numeric_cells_are_null_instead_of_using_first_number():
    header = [
        "Item Code", "Description", "Qty", "UOM", "Unit Price", "Net Amount",
        "VAT Amount",
    ]
    tables = [{
        "page": 1,
        "rows": [
            header,
            ["SYN-A", "Polluted quantity", "12 pieces", "EA", "4.00", "8.00", "0.40"],
            ["SYN-B", "Polluted price", "2", "EA", "4.00 each", "8.00", "0.40"],
            ["SYN-C", "Polluted amounts", "2", "EA", "4.00", "8.00 subtotal", "0.40 VAT"],
        ],
    }]

    result = extract_invoice_from_tables("Tax Invoice", tables)

    assert result is not None
    assert result["lines"] == [
        {
            "sku": "SYN-A",
            "description": "Polluted quantity",
            "uom": "EA",
            "price": "4.00",
            "net_amount": "8.00",
            "tax_amount": "0.40",
            "page": 1,
            "evidence": "table page 1 row 2",
        },
        {
            "sku": "SYN-C",
            "description": "Polluted amounts",
            "qty": "2",
            "uom": "EA",
            "price": "4.00",
            "page": 1,
            "evidence": "table page 1 row 4",
        },
    ]


def test_headerless_next_page_continues_only_same_invoice_size_and_serial():
    page_size = (320, 100)
    boxes = invoice_number("SYN-100", 1, size=page_size)
    boxes.extend(measured_header(size=page_size))
    boxes.extend(measured_row(1, "First synthetic item", size=page_size))
    boxes.extend(invoice_number("SYN-100", 2, size=page_size))
    boxes.extend(measured_row(2, "Second synthetic item", page=2, top=20, size=page_size))

    result = extract_invoice_from_tables("Tax Invoice", [], boxes=boxes)

    assert result is not None
    assert [(line["description"], line["page"]) for line in result["lines"]] == [
        ("First synthetic item", 1),
        ("Second synthetic item", 2),
    ]


@pytest.mark.parametrize(
    ("second_number", "second_size", "second_serial"),
    [
        ("SYN-OTHER", (320, 100), 2),
        ("SYN-100", (330, 100), 2),
        ("SYN-100", (320, 100), 1),
    ],
)
def test_headerless_next_page_rejects_wrong_invoice_size_or_restarted_serial(
    second_number,
    second_size,
    second_serial,
):
    first_size = (320, 100)
    boxes = invoice_number("SYN-100", 1, size=first_size)
    boxes.extend(measured_header(size=first_size))
    boxes.extend(measured_row(1, "First synthetic item", size=first_size))
    boxes.extend(invoice_number(second_number, 2, size=second_size))
    boxes.extend(measured_row(
        second_serial,
        "Unrelated second-page item",
        page=2,
        top=20,
        size=second_size,
    ))

    result = extract_invoice_from_tables("Tax Invoice", [], boxes=boxes)

    assert result is not None
    assert [line["description"] for line in result["lines"]] == ["First synthetic item"]


def test_zero_price_and_fully_discounted_rows_are_retained_as_printed():
    tables = [{
        "page": 1,
        "rows": [
            ["Item Code", "Description", "Qty", "UOM", "Unit Price", "Net Amount", "VAT Amount"],
            ["FREE-1", "Complimentary sample", "1", "EA", "0.00", "0.00", "0.00"],
            ["DISC-1", "Fully discounted item", "1", "EA", "12.50", "0.00", "0.00"],
        ],
    }]

    result = extract_invoice_from_tables("Tax Invoice", tables)

    assert result is not None
    assert [
        (line["sku"], line["price"], line["net_amount"], line["tax_amount"])
        for line in result["lines"]
    ] == [
        ("FREE-1", "0.00", "0.00", "0.00"),
        ("DISC-1", "12.50", "0.00", "0.00"),
    ]


@pytest.mark.parametrize("flattened", [False, True])
def test_description_metadata_is_separated_and_not_joined_to_product_name(flattened):
    tables = [{
        "page": 1,
        "rows": [
            ["Barcode / Part Number", "Description", "Qty", "UOM", "Unit Price", "Net Amount"],
            [
                "PR-001234567890-OP",
                "Synthetic Face Cream Model 50 ml\n"
                "SKU: SYN-CREAM-50\n"
                "COO: Exampleland\n"
                "HS Code: 33040000",
                "3",
                "Nos",
                "9.00",
                "27.00",
            ],
        ],
    }]
    if flattened:
        tables[0]["rows"][1][1] = tables[0]["rows"][1][1].replace("\n", " ")

    result = extract_invoice_from_tables("Tax Invoice", tables)

    assert result is not None
    assert result["lines"][0] == {
        "description": "Synthetic Face Cream Model 50 ml",
        "sku": "SYN-CREAM-50",
        "qty": "3",
        "uom": "Nos",
        "price": "9.00",
        "net_amount": "27.00",
        "page": 1,
        "evidence": "table page 1 row 2",
    }


def test_compound_barcode_part_column_only_maps_pure_digits_to_gtin():
    tables = [{
        "page": 1,
        "rows": [
            ["Barcode / Part Number", "Description", "Qty", "Unit Price"],
            ["001234567890", "Numeric identifier item", "1", "3.00"],
            ["PR-001234567890-OP", "Mixed part item", "1", "4.00"],
        ],
    }]

    result = extract_invoice_from_tables("Tax Invoice", tables)

    assert result is not None
    assert result["lines"][0]["gtin"] == "001234567890"
    assert "sku" not in result["lines"][0]
    assert result["lines"][1]["sku"] == "PR-001234567890-OP"
    assert "gtin" not in result["lines"][1]


def test_docling_flattened_metadata_matches_measured_row_without_duplicates():
    boxes = measured_header()
    boxes.extend(measured_row(1, "Synthetic lotion"))
    boxes.append(word("SKU: SYN-A", 1, 40, 43, width=100))
    tables = [{"source": "docling", "page": 1, "rows": [
        ["SN", "Description", "Qty", "UOM", "Unit Price", "Net Amount", "VAT5%"],
        ["1", "Synthetic lotion SKU: SYN-A COO: Exampleland HS Code: 33040000",
         "2", "EA", "4.00", "8.00", "0.40"],
    ]}]
    result = extract_invoice_from_tables("Tax Invoice", tables, boxes)
    assert len(result["lines"]) == 1
    assert result["lines"][0]["description"] == "Synthetic lotion"
    assert result["lines"][0]["sku"] == "SYN-A"
    assert result["lines"][0]["tax_amount"] == "0.40"


def test_incidental_pdfplumber_footer_table_does_not_block_word_recovery():
    footer = {
        "page": 1,
        "source": "pdfplumber",
        "rows": [
            ["Created By", "Approved By", "Printed By"],
            ["Synthetic User", "Synthetic Reviewer", "Synthetic Printer"],
        ],
    }
    boxes = measured_header()
    boxes.extend(measured_row(1, "Recovered synthetic item"))

    result = extract_invoice_from_tables("Tax Invoice", [footer], boxes=boxes)

    assert result is not None
    assert result["lines"] == [{
        "description": "Recovered synthetic item",
        "qty": "2",
        "uom": "EA",
        "price": "4.00",
        "net_amount": "8.00",
        "page": 1,
        "evidence": "table page 1 row 2",
    }]


def test_right_aligned_barcode_left_of_its_heading_is_not_description():
    from app.docling_extract import _column_bounds, _mapped_row

    roles = [("description", 40, 90), ("gtin", 200, 240), ("qty", 260, 275)]
    row = [word("Synthetic serum 30ml", 1, 40, 34, width=80),
           word("4000000000012", 1, 150, 34, width=80), word("3", 1, 265, 34, width=6)]
    values, _mapped = _mapped_row(row, roles, _column_bounds(roles))
    assert values == ["Synthetic serum 30ml", "4000000000012", "3"]


def test_size_digits_misread_as_letters_are_restored_in_description():
    from app.docling_extract import _column_bounds, _mapped_row

    roles = [("description", 40, 90), ("qty", 200, 215)]
    row = [word("SYN SERUM 3OML", 1, 40, 34, width=60), word("10mI PS", 1, 104, 34, width=30)]
    values, _mapped = _mapped_row(row, roles, _column_bounds(roles))
    assert values[0] == "SYN SERUM 30ML 10ml PS"


@pytest.mark.parametrize(("raw", "expected"), [("1 , 234.56", "1234.56"), ("2, 345.67", "2345.67"), ("12 ,5", None)])
def test_ocr_split_thousands_separator(raw, expected):
    from app.docling_extract import _decimal

    assert _decimal(raw) == expected


def test_measured_label_row_supplies_header_values_and_tolerates_label_typo():
    boxes = measured_header(top=40, size=(320, 120)) + measured_row(1, "Synthetic item", top=54, size=(320, 120))
    size = (320, 120)
    labels = [("Tax Invoice No.", 8, 50), ("Tax Inyoice Date", 80, 55), ("Sales Order", 160, 40), ("Currency", 230, 30)]
    values = [("SYN-4401", 10), ("25.06.2026", 84), ("SO-1", 162), ("AED", 232)]
    for text, left, width in labels:
        boxes.append(word(text, 1, left, 8, width=width, size=size))
    for text, left in values:
        boxes.append(word(text, 1, left, 18, width=30, size=size))
    text = "Net Amount  8.00\nVAT Amount  0.40\nVAT Amount in AED  0.00\n"
    invoice = extract_invoice_from_tables(text, [], boxes)
    assert (invoice["number"], invoice["date"], invoice["currency"]) == ("SYN-4401", "2026-06-25", "AED")
    assert (invoice["net"], invoice["tax"]) == ("8.00", "0.40")


def test_unit_heading_with_ocr_dropped_letter_still_supplies_uom():
    size = (320, 100)
    boxes = [word("Description", 1, 38, 20, width=42), word("Qty.", 1, 160, 20, width=12),
             word("(n", 1, 175, 20, width=8), word("PCE)", 1, 186, 20, width=14),
             word("Unit Price", 1, 218, 20, width=35), word("Net Amount", 1, 270, 20, width=42),
             word("Synthetic item", 1, 40, 34, width=60), word("2", 1, 175, 34, width=8),
             word("4.00", 1, 224, 34, width=20), word("8.00", 1, 276, 34, width=20)]
    invoice = extract_invoice_from_tables("", [], boxes)
    assert invoice["lines"][0]["uom"] == "PCE"
