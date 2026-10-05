"""Reader for the columnar 'TAX INVOICE' layout (an accounting-package export).

The layout is recognised by its structure, never by a supplier name:

* a ``TAX INVOICE`` title followed by a ``# <document number>`` line;
* ``Date of Issuing:`` with a long-form date (``August 25, 2026``);
* ``Issued By:`` / ``Issued To:`` party columns;
* an item table headed ``# Description Brand Quantity UOM`` followed by the
  price, amount (excl. VAT), VAT %, VAT amount and amount (incl. VAT) columns,
  with the currency code repeated under the money columns;
* an optional ``Reference #:`` carrying the buyer's order number;
* ``Sub Total, CUR:``, ``Total VAT, CUR:`` and ``Total, CUR:`` lines.

Words are placed in columns by their x position against the table header, so
wrapped descriptions and brands are joined per line and lines continue across
page breaks. The barcode printed at the end of the description is moved to its
own field only when its GS1 check digit is valid. Printed values are copied as
printed; nothing is derived to make a line reconcile. ``reconciliation`` reports
the arithmetic checks so a reviewer sees which lines need attention.
"""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TITLE = re.compile(r"^TAX INVOICE(?:\s|$)")
NUMBER = re.compile(r"^#\s*([A-Z0-9][A-Z0-9./_-]{2,79})$")
# OCR readers may put logo text on the same row, to the right of the title and the date.
ISSUED = re.compile(r"^Date of Issuing\s*:\s*([A-Za-z]{3,9}\.?\s+\d{1,2}\s*,\s*\d{4})(?:\s|$)")
# Totals sit in the right-hand column; the amount in words may share their row on the left.
SUB_TOTAL = re.compile(r"(?:^|.*\s)Sub Total\s*,\s*([A-Z]{3})\s*:\s*([0-9][0-9,]*\.\d{2})$")
TOTAL_VAT = re.compile(r"(?:^|.*\s)Total VAT\s*,\s*([A-Z]{3})\s*:\s*([0-9][0-9,]*\.\d{2})$")
TOTAL = re.compile(r"(?:^|.*\s)(?<!Sub )Total\s*,\s*([A-Z]{3})\s*:\s*([0-9][0-9,]*\.\d{2})$")
# The buyer's order number; checked against the PO extract (RMS order numbers) before mapping to po.
REFERENCE = re.compile(r"^Reference #\s*:\s*(\d{4,20})(?:\s+Reference Date\s*:.*)?$")
TABLE_END = re.compile(r"^(TOTAL OF SUPPLY|Sub Total\s*,|Total VAT\s*,|Total\s*,|Terms and Conditions)")
PARTIES = re.compile(r"^Issued By\s*:.*\sIssued To\s*:")
PAGE_FOOTER = re.compile(r"^Made with \w+")
MONEY = re.compile(r"^-?[0-9][0-9,]*\.\d{2}$")
QTY = re.compile(r"^[0-9][0-9,]*(?:\.\d{1,4})?$")
RATE = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")
BARCODE = re.compile(r"(?:,\s*|\s|^)(\d{8,14})$")
# A short variant suffix may follow the barcode after a dash ("..., 5012345678900 - D").
BARCODE_SUFFIX = re.compile(r",\s*(\d{8,14})(\s+-\s+\S.{0,30})$")
MONTHS = {m: n for n, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
CENT = Decimal("0.01")


def _words(boxes):
    out = []
    for b in boxes or []:
        box, value = b.get("box"), str(b.get("text") or "").strip()
        if not value or not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            x0, top, x1, bottom = (float(v) for v in box)
            page = int(b.get("page") or 1)
        except (TypeError, ValueError):
            continue
        out.append({"text": value, "page": page, "x0": x0, "x1": x1, "top": top, "bottom": bottom})
    return out


def _rows(words):
    """Group words into visual rows per page, top to bottom, left to right."""
    rows = []
    for page in sorted({w["page"] for w in words}):
        current, mid = [], None
        for w in sorted((w for w in words if w["page"] == page), key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"])):
            centre = (w["top"] + w["bottom"]) / 2
            tolerance = max(2.0, (w["bottom"] - w["top"]) * 0.45)
            if current and abs(centre - mid) > tolerance:
                rows.append(sorted(current, key=lambda w: w["x0"]))
                current = []
            if not current:
                mid = centre
            current.append(w)
        if current:
            rows.append(sorted(current, key=lambda w: w["x0"]))
    return [_join_split_tokens(row) for row in rows]


def _join_split_tokens(row):
    """Rejoin tokens an OCR reader split off: 'Issuing' ':' and '1' ',' '234.50' or '1,' '234.50'.

    Only near neighbours join, and a thousands group joins only onto 1-3 digits; native words are unaffected.
    """
    out = []
    for w in row:
        prev = out[-1] if out else None
        gap = w["x0"] - prev["x1"] if prev else None
        height = w["bottom"] - w["top"]
        join = prev is not None and (
            (gap <= 0.8 * height and re.fullmatch(r"[:,]", w["text"]))
            or (gap <= 0.25 * height and re.fullmatch(r"[.)%]+", w["text"]))
            or (gap <= 0.8 * height and re.fullmatch(r"\d{1,3},", prev["text"])
                and re.fullmatch(r"\d{3}\.\d{2}", w["text"])))
        if join:
            out[-1] = {**prev, "text": prev["text"] + w["text"], "x1": max(prev["x1"], w["x1"]),
                       "bottom": max(prev["bottom"], w["bottom"])}
        else:
            out.append(w)
    return out


def _text(words):
    return " ".join(w["text"] for w in words)


def _decimal(value):
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _long_date(value):
    match = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*,\s*(\d{4})", value.strip())
    if not match or match.group(1)[:3].lower() not in MONTHS:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(1)[:3].lower()], int(match.group(2))).isoformat()
    except ValueError:
        return None


def gs1_valid(code):
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def _header_row(row):
    names = [w["text"] for w in row]
    if not all(n in names for n in ("Description", "Brand", "Quantity", "UOM")):
        return None
    pick = {w["text"]: w for w in row}
    heights = sorted(w["bottom"] - w["top"] for w in row)
    return {"description": pick["Description"]["x0"], "brand": pick["Brand"]["x0"],
            "quantity": pick["Quantity"]["x0"], "uom": pick["UOM"]["x0"],
            # Column slack follows the text size, so point (PDF) and pixel (OCR) coordinates both work.
            "slack": max(3.0, heights[len(heights) // 2] * 0.4)}


def detect(text, boxes):
    """True only when every structural marker of the layout is present."""
    lines = [_text(r) for r in _rows(_words(boxes))]
    if not lines:
        return False
    has = lambda pattern: any(pattern.match(line) for line in lines)  # noqa: E731
    return (has(TITLE) and has(NUMBER) and has(ISSUED) and has(SUB_TOTAL) and has(TOTAL_VAT) and has(TOTAL)
            and any(_header_row(r) for r in _rows(_words(boxes)))
            and any(PARTIES.match(line) for line in lines))


def _one(lines, pattern):
    found = {pattern.match(line).groups() for line in lines if pattern.match(line)}
    return found.pop() if len(found) == 1 else None


def _party_names(rows):
    """Names on the row under 'Issued By:  Issued To:', split at the 'Issued To:' column."""
    for i, row in enumerate(rows[:-1]):
        if not PARTIES.match(_text(row)):
            continue
        split = next(w["x0"] for w, nxt in zip(row, row[1:]) if w["text"] == "Issued" and nxt["text"].startswith("To")
                     and w is not row[0])
        below = rows[i + 1]
        if below[0]["page"] != row[0]["page"]:
            return None, None
        left = _text([w for w in below if w["x1"] <= split + 1]) or None
        right = _text([w for w in below if w["x0"] >= split - 1]) or None
        return left, right
    return None, None


def _tail(words):
    """qty UOM price amount VAT% VAT-amount total, exactly as printed, or None."""
    t = [w["text"] for w in words]
    if (len(t) == 7 and QTY.match(t[0]) and re.fullmatch(r"[A-Za-z][A-Za-z.]{0,11}", t[1])
            and MONEY.match(t[2]) and MONEY.match(t[3]) and RATE.match(t[4]) and MONEY.match(t[5])
            and MONEY.match(t[6])):
        return dict(zip(("qty", "uom", "price", "net_amount", "rate", "tax_amount", "total"),
                        [_decimal(t[0]), t[1], *[_decimal(v) for v in t[2:]]]))
    return None


def _split_barcode(description):
    match = BARCODE.search(description)
    if match and gs1_valid(match.group(1)):
        return description[:match.start()].rstrip(" ,") or None, match.group(1)
    match = BARCODE_SUFFIX.search(description)
    if match and gs1_valid(match.group(1)):
        return (description[:match.start()].rstrip(" ,") + match.group(2)).strip() or None, match.group(1)
    return description, None


def extract(text, boxes):
    """Return (invoice dict, reconciliation) for this layout, or None when it does not apply."""
    if not detect(text, boxes):
        return None
    rows = _rows(_words(boxes))
    lines = [_text(r) for r in rows]
    columns, drafts, in_table, finished = None, [], False, False
    for row in rows:
        line = _text(row)
        header = _header_row(row)
        if header:
            columns, in_table = header, False
            continue
        if columns is None or finished:
            continue
        if not in_table:
            # Body starts below the currency row; a page without its own header continues the table.
            codes = [w for w in row if re.fullmatch(r"[A-Z]{3}", w["text"])]
            if len(codes) >= 2 and all(w in codes or w["text"] == "%" for w in row):
                in_table = True
                continue
            if drafts and row[0]["page"] != drafts[-1]["page"] and not header:
                in_table = True
            else:
                continue
        if PAGE_FOOTER.match(line):
            in_table = False
            continue
        if TABLE_END.match(line):
            finished = True
            continue
        slack = columns["slack"]
        left = columns["description"] - slack
        number = row[0]["text"] if re.fullmatch(r"\d{1,4}", row[0]["text"]) and row[0]["x1"] <= left + 2 * slack else None
        body = row[1:] if number else row
        description = [w for w in body if w["x0"] < columns["brand"] - slack]
        brand = [w for w in body if columns["brand"] - slack <= w["x0"] < columns["quantity"] - slack]
        tail = [w for w in body if w["x0"] >= columns["quantity"] - slack]
        if number:
            drafts.append({"no": int(number), "page": row[0]["page"], "description": [_text(description)],
                           "brand": [_text(brand)], "tail": _tail(tail), "raw_tail": _text(tail)})
        elif drafts:
            if tail:
                # A continuation row never carries amounts; keep it visible instead of guessing.
                drafts[-1]["stray"] = drafts[-1].get("stray", []) + [_text(tail)]
            drafts[-1]["description"].append(_text(description))
            drafts[-1]["brand"].append(_text(brand))
    sub_total, total_vat, total = _one(lines, SUB_TOTAL), _one(lines, TOTAL_VAT), _one(lines, TOTAL)
    currencies = {x[0] for x in (sub_total, total_vat, total) if x}
    issued = _one(lines, ISSUED)
    supplier, buyer = _party_names(rows)
    out_lines = []
    for d in drafts:
        description, gtin = _split_barcode(" ".join(p for p in d["description"] if p).strip())
        tail = d["tail"] or {}
        out_lines.append({
            "gtin": gtin, "description": description, "qty": tail.get("qty"), "uom": tail.get("uom"),
            "price": tail.get("price"), "net_amount": tail.get("net_amount"), "tax_amount": tail.get("tax_amount"),
            "page": d["page"],
            "evidence": f"line {d['no']}: {d['raw_tail']}"[:300],
        })
    invoice = {
        "number": (_one(lines, NUMBER) or [None])[0],
        "po": (_one(lines, REFERENCE) or [None])[0],
        "supplier_name": supplier, "buyer_name": buyer,
        "date_printed": issued[0] if issued else None, "date": _long_date(issued[0]) if issued else None,
        "currency": currencies.pop() if len(currencies) == 1 else None,
        "net": _decimal(sub_total[1]) if sub_total else None,
        "tax": _decimal(total_vat[1]) if total_vat else None,
        "lines": out_lines,
    }
    return invoice, reconcile(drafts, invoice, _decimal(total[1]) if total else None)


def reconcile(drafts, invoice, total):
    """Arithmetic checks on printed values only; nothing is corrected."""
    checks = []
    for d, line in zip(drafts, invoice["lines"]):
        t = d["tail"]
        ok = bool(t) and not d.get("stray") and line["description"] is not None
        if t:
            ok = ok and (t["qty"] * t["price"]).quantize(CENT, ROUND_HALF_UP) == t["net_amount"]
            ok = ok and (t["net_amount"] * t["rate"] / 100).quantize(CENT, ROUND_HALF_UP) == t["tax_amount"]
            ok = ok and t["net_amount"] + t["tax_amount"] == t["total"]
        checks.append({"line": d["no"], "reconciled": ok, "gtin": line["gtin"] is not None})
    amounts = [line["net_amount"] for line in invoice["lines"]]
    taxes = [line["tax_amount"] for line in invoice["lines"]]
    numbers = [d["no"] for d in drafts]
    net, tax = invoice["net"], invoice["tax"]
    return {
        "lines": checks,
        "line_numbers_contiguous": numbers == list(range(1, len(numbers) + 1)),
        "lines_sum_to_net": bool(amounts) and None not in amounts and net is not None and sum(amounts) == net,
        "line_vat_sums_to_vat": bool(taxes) and None not in taxes and tax is not None and sum(taxes) == tax,
        "net_plus_vat_is_total": None not in (net, tax, total) and net + tax == total,
    }
