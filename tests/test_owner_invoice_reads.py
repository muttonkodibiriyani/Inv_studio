"""Synthetic reads for layouts seen on a scanned supplier invoice: run-on OCR headers and printed dates."""
from app.layout_extract import _extract_printed_date


def test_ordinal_date_after_a_mid_line_date_label_is_kept_as_printed():
    lines = ["Tax Invoice  Order Number: SYN-7  Date: 3rd March 2031  Invoice Number: SYN-7"]

    assert _extract_printed_date(lines) == "3rd March 2031"


def test_full_month_and_ordinal_forms_are_printed_dates():
    assert _extract_printed_date(["Invoice Date: 21st March 2031"]) == "21st March 2031"
    assert _extract_printed_date(["Date: 2 March, 2031"]) == "2 March, 2031"
    assert _extract_printed_date(["Document Date: 03/04/2031"]) == "03/04/2031"


def test_other_dates_in_a_run_on_line_are_not_the_invoice_date():
    assert _extract_printed_date(["Ship Date: 2nd June 2031"]) is None
    assert _extract_printed_date(["Payment Due Date: 5th May 2031"]) is None
    assert _extract_printed_date(["Due Date: 5th May 2031  Date: 1st May 2031"]) == "1st May 2031"


def test_two_different_labelled_dates_stay_unresolved():
    assert _extract_printed_date(["Date: 1st May 2031", "Invoice Date: 2nd May 2031"]) is None


# A synthetic scanned table: 13 rows printed 13.5 px apart with 16 px tall words, on a page tilted so the
# right end of each row sits 7 px lower than its left end (the quantity/price/amount columns drift into the
# next row's band). One right-aligned quantity ends just past its column boundary.
from decimal import Decimal  # noqa: E402

from app.docling_extract import _rows_from_words, _tables_from_measured_words  # noqa: E402

SLOPE = 0.009


def _word(text, x0, y, width=None, page=1):
    width = width or 8 * len(text)
    tilt = SLOPE * (x0 + width / 2)
    return {"text": text, "page": page, "box": [x0, y + tilt - 8, x0 + width, y + tilt + 8],
            "size": [1200, 2000], "geometry": "word"}


def _tilted_table():
    words = [_word("DESCRIPTION", 160, 600), _word("SKU", 480, 600, width=60), _word("SIZE", 580, 600),
             _word("QUANTITY", 660, 600), _word("UNIT", 760, 600), _word("PRICE", 800, 600),
             _word("AMOUNT", 850, 600, width=100)]
    lines = []
    for n in range(13):
        y = 620 + 13.5 * n
        qty, price = 3 + n % 4, Decimal("10.25") + n
        amount = qty * price
        lines.append((f"SYN-{100 + n}", qty, price, amount))
        words += [_word("Synthetic", 160, y), _word(f"product{n}", 240, y), _word(f"SYN-{100 + n}", 480, y),
                  _word("30ml", 580, y),
                  # the quantity of row 3 is printed a little right, past the quantity/price boundary
                  _word(str(qty), 746 if n == 3 else 736, y, width=8),
                  _word("AED", 760, y), _word(str(price), 805, y), _word("AED", 850, y), _word(str(amount), 910, y)]
    words += [_word("SUBTOTAL", 760, 820), _word(str(sum(x[3] for x in lines)), 910, 820)]
    return words, lines


def test_rows_of_a_tilted_closely_spaced_table_stay_separate():
    words, lines = _tilted_table()
    rows = _rows_from_words(words)
    skus = [[w["text"] for w in row if w["text"].startswith("SYN-")] for row in rows]
    assert [s for s in skus if s] == [[line[0]] for line in lines]


def test_tilted_scanned_table_reads_every_printed_line():
    words, lines = _tilted_table()
    tables = _tables_from_measured_words(words)

    assert len(tables) == 1
    header, *rows = tables[0]["rows"]
    column = {role: header.index(role) for role in ("sku", "qty", "price", "net_amount")}
    assert [row[column["sku"]] for row in rows] == [line[0] for line in lines]
    for row, (_sku, qty, price, amount) in zip(rows, lines):
        assert row[column["qty"]] == str(qty)
        assert row[column["price"]] == f"AED {price}"
        assert row[column["net_amount"]] == f"AED {amount}"


def test_a_straight_page_groups_rows_as_before():
    words = [{"text": t, "page": 1, "box": [x, y - 8, x + 20, y + 8]}
             for n in range(30) for t, x, y in ((f"r{n}", 100, 100 + 20 * n), (str(n), 300, 101 + 20 * n))]
    assert [[w["text"] for w in row] for row in _rows_from_words(words)] == [[f"r{n}", str(n)] for n in range(30)]


# A synthetic two-page scan: the invoice, then a packing list for the same goods. The packing list prints
# its own 'Supplier:' label beside its 'Packing List:' title and repeats the goods in a priced table.
from app.layout_extract import extract_invoice  # noqa: E402


def _line(texts, y, page, x=100):
    words = []
    for text in texts.split():
        words.append({"text": text, "page": page, "box": [x, y - 8, x + 8 * len(text), y + 8],
                      "size": [1200, 2000], "geometry": "word"})
        x += 8 * len(text) + 8
    return words


def _invoice_with_packing_list(supplier_on_invoice=False):
    words = _line("TAX INVOICE", 100, 1, x=500)
    if supplier_on_invoice:
        words += _line("Supplier: Synthetic Goods Trading", 150, 1)
    words += _line("Invoice Number: SYN-41", 180, 1) + _line("Bill To: Example Retail Store", 210, 1)
    words += _line("Packing List", 100, 2, x=500)
    words += _line("Supplier: Business Park   Packing List: PL-9", 150, 2)
    words += [w for row in (("DESCRIPTION", 160), ("QUANTITY", 660), ("PRICE", 800)) for w in _line(row[0], 300, 2, x=row[1])]
    words += [w for row in (("Synthetic item", 160), ("4", 680), ("12.50", 805)) for w in _line(row[0], 330, 2, x=row[1])]
    text = "\n".join(" ".join(w["text"] for w in words if w["box"][1] == y - 8 and w["page"] == p)
                     for p, y in ((1, 100), (1, 150), (1, 180), (1, 210), (2, 100), (2, 150), (2, 300), (2, 330)))
    return text, words


def test_a_packing_list_page_never_supplies_the_supplier_name():
    text, words = _invoice_with_packing_list()
    invoice = extract_invoice(text, words)

    assert invoice is not None
    assert invoice["supplier_name"] is None
    assert invoice["buyer_name"] == "Example Retail Store"


def test_the_invoice_page_supplier_label_is_still_read_beside_a_packing_list():
    text, words = _invoice_with_packing_list(supplier_on_invoice=True)

    assert extract_invoice(text, words)["supplier_name"] == "Synthetic Goods Trading"


def test_a_packing_list_page_contributes_no_invoice_lines():
    _text, words = _invoice_with_packing_list()
    packing = [w for w in words if w["page"] == 2]
    assert _tables_from_measured_words(packing) == []
    retitled = [dict(w, text="Delivery") if w["text"] == "Packing" else w for w in packing]
    assert len(_tables_from_measured_words(retitled)) == 1


def test_a_thousands_comma_read_as_a_point_is_still_an_amount():
    from app.docling_extract import _decimal

    assert _decimal("AED 1.234.56") == "1234.56"
    assert _decimal("12.345.678.90") == "12345678.90"
    assert _decimal("1.234") == "1.234"
    assert _decimal("1.23.45") is None and _decimal("1.234.5") is None and _decimal("1.234.567") is None
