"""Conservative invoice extraction for readable documents without a template.

This module deliberately extracts only facts printed on the document.  It does
not perform reference matching, approve an invoice, or derive totals and codes
from business rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Iterable

from app.models import Invoice, Line


_STRONG_INVOICE = re.compile(
    r"(?im)(?:\b(?:tax|commercial|sales|debit)\s+invoice\b|\bcredit\s*invoice\b|^\s*invoice\s*$)"
)
_PURCHASE_ORDER_HEADING = re.compile(r"(?im)^\s*purchase\s+order(?:\s+no\b[^\n]*)?\s*$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_-]{0,79}$")
_PLAIN_NUMBER = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_GTIN = re.compile(r"(?i)\b(?:EAN|GTIN|UPC)\s*[:#-]?\s*([0-9][0-9 -]{6,22}[0-9])\b")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class _Word:
    text: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float
    page_height: float

    @property
    def x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


@dataclass(frozen=True)
class _Columns:
    item: float | None
    description: float
    uom: float | None
    qty: float
    price: float
    price_right: float
    serial: float | None = None
    net_bounds: tuple[float, float] | None = None
    tax_bounds: tuple[float, float] | None = None


@dataclass
class _DraftLine:
    sku: str | None
    description: str | None
    uom: str | None
    qty: Decimal
    price: Decimal
    evidence_parts: list[str]
    page: int
    last_y: float
    gtin: str | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None


def extract_invoice(text: str, boxes: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Extract visible invoice facts from OCR/native text and word boxes.

    A strong invoice title is required.  Conflicting labels and ambiguous dates
    are left unset, and line totals are never used to invent header totals.
    The result is shaped and validated by :class:`app.models.Invoice`.
    """

    source_text = text if isinstance(text, str) else ""
    words = _normalise_words(boxes or [])
    rows = _group_all_rows(words)
    reconstructed = "\n".join(_row_text(row) for _, row in rows)
    searchable = "\n".join(part for part in (source_text, reconstructed) if part)

    if not _has_invoice_heading(searchable):
        return None
    if _is_purchase_order_document(searchable):
        return None

    lines = [line.strip() for line in searchable.splitlines() if line.strip()]
    source_lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    number = _unique_identifier(
        lines,
        (
            re.compile(
                r"(?i)\b(?:invoice|inv|document)\s*(?:no\.?|number|#)\s*[:#-]?\s*"
                r"([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
        ),
    )
    labeled_number = _spatial_joined_identifier(
        rows, {"tax invoice number", "tax invoice no", "invoice number", "invoice no"}
    )
    if labeled_number is not None:
        number = labeled_number
    elif number is None:
        number = _spatial_invoice_number(rows)
    compact_header = _spatial_compact_header(rows)
    if number is None:
        number = compact_header.get("number")
    supplier_name = _unique_text(
        lines,
        (
            re.compile(r"(?i)\b(?:supplier(?:\s+name)?|seller\s+name)\s*:\s*(.{1,160})$"),
        ),
    )
    supplier_name = _spatial_party_name(rows, {"supplier", "supplier name"}) or supplier_name
    buyer_name = _spatial_buyer_name(rows) or _extract_buyer_name(lines)
    po = _unique_identifier(
        lines,
        (
            re.compile(
                r"(?i)\bref\s*:\s*lpo\s*[-:#]?\s*([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
            re.compile(
                r"(?i)\b(?:lpo|po|purchase\s+order)\s*(?:no\.?|number|#)?\s*[:#-]\s*"
                r"(?:lpo\s*[-:#]?\s*)?([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
            re.compile(
                r"(?i)\b(?:lpo|po|purchase\s+order)\s+(?:no\.?|number|#)\s*[:#-]?\s+"
                r"([A-Z0-9][A-Z0-9./_-]{0,79})"
            ),
        ),
    )
    if po is None:
        po = _spatial_labeled_identifier(rows, {"lpo no", "lpo number", "po no", "po number"})
    invoice_date = _extract_date(lines)
    if po is None:
        po = compact_header.get("po")
    if invoice_date is None:
        invoice_date = compact_header.get("date")
    if invoice_date is None:
        invoice_date = _spatial_labeled_date(rows)
    currency = _extract_currency(lines)
    net_patterns = (
            re.compile(
                r"(?i)^\s*net\s+total\s*:?\s*(?:[A-Z]{3}\s*)?"
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
            ),
            re.compile(
                r"(?i)^\s*net\s+amount\s*:?\s*(?:[A-Z]{3}\s*)?"
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
            ),
            re.compile(
                r"(?i)^\s*sub\s*total\s*:?\s*(?:[A-Z]{3}\s*)?"
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\b"
            ),
        )
    net = _extract_money(source_lines, net_patterns)
    if net is None:
        net = _extract_money(lines, net_patterns)
    tax_patterns = (
            re.compile(
                r"(?i)(?:^|\s)(?:tax\s+amount|vat\s+(?:amount|amt\.?))\s*:?\s*"
                r"(?:[A-Z]{3}\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\b"
            ),
            re.compile(
                r"(?i)^\s*tax\s*\(\s*[0-9]+(?:\.[0-9]+)?\s*%\s*\)\s*:?[ \t]*"
                r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
            ),
            re.compile(
                r"(?i)^\s*(?:vat|tax)\s*[-:]?\s*[0-9]+(?:\.[0-9]+)?\s*%?\s*"
                r"(?:[A-Z]{3}\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*$"
            ),
        )
    tax = _extract_money(source_lines, tax_patterns)
    if tax is None:
        tax = _extract_money(lines, tax_patterns)
    spatial_net, spatial_tax = _extract_spatial_totals(words)
    if net is None:
        net = spatial_net
    if tax is None:
        tax = spatial_tax
    extracted_lines = _extract_table_lines(words)

    invoice = Invoice(
        number=number,
        supplier_name=supplier_name,
        buyer_name=buyer_name,
        # These are internal/canonical fields and must come from reference review.
        seller=None,
        site=None,
        buyer=None,
        po=po,
        location=None,
        date=invoice_date,
        date_printed=_extract_printed_date(source_lines) or _extract_printed_date(lines),
        currency=currency,
        origin=None,
        market=None,
        taxCode=None,
        net=net,
        tax=tax,
        lines=extracted_lines,
    )
    if not any(
        (
            invoice.number,
            invoice.supplier_name,
            invoice.buyer_name,
            invoice.po,
            invoice.date,
            invoice.currency,
            invoice.net is not None,
            invoice.tax is not None,
            invoice.lines,
        )
    ):
        return None
    return invoice.model_dump(mode="json")


def _has_invoice_heading(text: str) -> bool:
    return bool(_STRONG_INVOICE.search(text))


def _is_purchase_order_document(text: str) -> bool:
    """Reject a purchase-order title that precedes any invoice title."""

    purchase = _PURCHASE_ORDER_HEADING.search(text)
    if purchase is None:
        return False
    invoice = _STRONG_INVOICE.search(text)
    return invoice is None or purchase.start() < invoice.start()


def _normalise_words(boxes: Iterable[dict[str, Any]]) -> list[_Word]:
    words: list[_Word] = []
    for raw in boxes:
        if not isinstance(raw, dict):
            continue
        value = raw.get("text")
        bounds = raw.get("box")
        size = raw.get("size")
        if not isinstance(value, str) or not value.strip():
            continue
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            continue
        try:
            page = int(raw.get("page", 1))
            x0, y0, x1, y1 = (float(part) for part in bounds)
            page_width, page_height = (float(part) for part in size)
        except (TypeError, ValueError, OverflowError):
            continue
        if not 1 <= page <= 20 or page_width <= 0 or page_height <= 0:
            continue
        if not all(map(_finite, (x0, y0, x1, y1, page_width, page_height))):
            continue
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        coordinate_system = str(raw.get("coordinate_system", "")).lower()
        if "bottom" in coordinate_system:
            y0, y1 = page_height - y1, page_height - y0
        words.append(
            _Word(
                text=" ".join(value.split()),
                page=page,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                page_width=page_width,
                page_height=page_height,
            )
        )
    return words


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _group_all_rows(words: list[_Word]) -> list[tuple[int, list[_Word]]]:
    grouped: list[tuple[int, list[_Word]]] = []
    for page in sorted({word.page for word in words}):
        for row in _group_page_rows([word for word in words if word.page == page]):
            grouped.append((page, row))
    return grouped


def _group_page_rows(words: list[_Word]) -> list[list[_Word]]:
    if not words:
        return []
    tolerance = max(3.0, median(word.height for word in words) * 0.6)
    rows: list[list[_Word]] = []
    centres: list[float] = []
    for word in sorted(words, key=lambda item: (item.y, item.x)):
        if rows and abs(word.y - centres[-1]) <= tolerance:
            rows[-1].append(word)
            centres[-1] = sum(item.y for item in rows[-1]) / len(rows[-1])
        else:
            rows.append([word])
            centres.append(word.y)
    return [sorted(row, key=lambda item: item.x) for row in rows]


def _row_text(row: list[_Word]) -> str:
    return " ".join(word.text for word in sorted(row, key=lambda item: item.x)).strip()


def _unique_identifier(
    lines: list[str], patterns: tuple[re.Pattern[str], ...]
) -> str | None:
    values: list[str] = []
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                value = match.group(1).strip().rstrip(".,:;")
                if _SAFE_IDENTIFIER.fullmatch(value):
                    values.append(value)
                break
    return _only_distinct(values)


def _spatial_invoice_number(rows: list[tuple[int, list[_Word]]]) -> str | None:
    """Read one prominent identifier printed beside an invoice heading."""

    values: list[str] = []
    for row_index, (page, row) in enumerate(rows):
        if not re.search(r"(?i)\b(?:tax|commercial|sales|debit|credit)\s*invoice\b", _row_text(row)):
            continue
        heading_words = [word for word in row if "invoice" in _normal(word.text) or _normal(word.text) == "tax"]
        if not heading_words:
            continue
        heading_right = max(word.x1 for word in heading_words)
        candidates: list[str] = []
        for word in row:
            candidate = word.text.strip().replace(" ", "")
            if word.x0 <= heading_right or not _SAFE_IDENTIFIER.fullmatch(candidate):
                continue
            if len(candidate) < 4 or not any(char.isdigit() for char in candidate):
                continue
            candidates.append(candidate)
        if not candidates and row_index:
            previous_page, previous = rows[row_index - 1]
            gap = _row_y(row) - _row_y(previous)
            heading_height = max(word.height for word in heading_words)
            if previous_page == page and 0 < gap <= max(100.0, heading_height * 6.0):
                for word in previous:
                    candidate = word.text.strip().replace(" ", "")
                    if word.x0 <= heading_right or not _SAFE_IDENTIFIER.fullmatch(candidate):
                        continue
                    if len(candidate) < 4 or not any(char.isdigit() for char in candidate):
                        continue
                    candidates.append(candidate)
        if len(set(candidates)) == 1:
            values.extend(candidates)
    return _only_distinct(values)


def _spatial_labeled_date(rows: list[tuple[int, list[_Word]]]) -> str | None:
    values: list[str] = []
    labels = {"date", "document date", "invoice date"}
    for _, row in rows:
        for start in range(len(row)):
            for width in (2, 1):
                end = start + width
                if end > len(row):
                    continue
                phrase = " ".join(_normal(word.text) for word in row[start:end])
                if phrase not in labels:
                    continue
                if phrase == "date" and start and _normal(row[start - 1].text) in {
                    "due", "run", "report",
                }:
                    continue
                label_right = max(word.x1 for word in row[start:end])
                for candidate_word in row[end:]:
                    if candidate_word.x0 < label_right:
                        continue
                    if candidate_word.x0 - label_right > row[0].page_width * 0.25:
                        break
                    parsed = _parse_date(candidate_word.text)
                    if parsed is not None:
                        values.append(parsed)
                    break
                break
    return _only_distinct(values)


def _spatial_labeled_identifier(
    rows: list[tuple[int, list[_Word]]], labels: set[str]
) -> str | None:
    """Read one identifier immediately to the right of an explicit label."""

    values: list[str] = []
    for _, row in rows:
        for start in range(len(row)):
            for width in (1, 2, 3):
                end = start + width
                if end > len(row):
                    continue
                label_words = row[start:end]
                if " ".join(_normal(word.text) for word in label_words) not in labels:
                    continue
                label_right = max(word.x1 for word in label_words)
                label_height = max(word.height for word in label_words)
                for candidate_word in row[end:]:
                    if candidate_word.x0 < label_right:
                        continue
                    if candidate_word.x0 - label_right > max(36.0, label_height * 5.0):
                        break
                    candidate = candidate_word.text.strip().replace(" ", "")
                    if _SAFE_IDENTIFIER.fullmatch(candidate) and any(
                        char.isdigit() for char in candidate
                    ):
                        values.append(candidate)
                    break
    return _only_distinct(values)


def _spatial_joined_identifier(
    rows: list[tuple[int, list[_Word]]], labels: set[str]
) -> str | None:
    """Join adjacent OCR tokens after one explicit identifier label.

    OCR commonly returns ``ABC-`` and ``01`` as separate words. Only tokens
    touching the label's value run are joined, and the result must still be a
    short safe identifier containing a digit.
    """

    values: list[str] = []
    for _, row in rows:
        for start in range(len(row)):
            for width in (3, 2):
                end = start + width
                if end > len(row):
                    continue
                label_words = row[start:end]
                if " ".join(_normal(word.text) for word in label_words) not in labels:
                    continue
                label_right = max(word.x1 for word in label_words)
                label_height = max(word.height for word in label_words)
                pieces: list[str] = []
                previous_right = label_right
                for word in row[end:]:
                    if word.x0 < label_right:
                        continue
                    token = word.text.strip().replace(" ", "")
                    if token in {":", "#", "-"} and not pieces:
                        previous_right = word.x1
                        continue
                    if word.x0 - previous_right > max(12.0, label_height * 1.5):
                        break
                    if not re.fullmatch(r"[A-Za-z0-9./_-]+", token):
                        break
                    pieces.append(token)
                    previous_right = word.x1
                candidate = "".join(pieces)
                if (
                    candidate
                    and len(candidate) <= 80
                    and _SAFE_IDENTIFIER.fullmatch(candidate)
                    and any(character.isdigit() for character in candidate)
                ):
                    values.append(candidate)
                break
    return _only_distinct(values)


def _spatial_party_name(
    rows: list[tuple[int, list[_Word]]], labels: set[str]
) -> str | None:
    """Read a party name beside or immediately below an explicit label."""

    values: list[str] = []
    for row_index, (page, row) in enumerate(rows):
        for start in range(len(row)):
            for width in (2, 1):
                end = start + width
                if end > len(row):
                    continue
                label_words = row[start:end]
                if " ".join(_normal(word.text) for word in label_words) not in labels:
                    continue
                label_right = max(word.x1 for word in label_words)
                same_row = [
                    word for word in row[end:]
                    if word.x0 >= label_right
                    and word.x0 - min(item.x0 for item in label_words)
                    <= row[0].page_width * 0.4
                ]
                candidate = _clean_party_name(_join_words(same_row))
                if candidate is None:
                    label_left = min(word.x0 for word in label_words)
                    label_y = _row_y(row)
                    label_height = max(word.height for word in label_words)
                    for next_page, next_row in rows[row_index + 1 :]:
                        if next_page != page:
                            continue
                        gap = _row_y(next_row) - label_y
                        if gap <= 0:
                            continue
                        if gap > max(50.0, label_height * 4.0):
                            break
                        nearby = [
                            word for word in next_row
                            if word.x0 >= label_left
                            and word.x0 - label_left <= row[0].page_width * 0.35
                        ]
                        candidate = _clean_party_name(_join_words(nearby))
                        if candidate is not None:
                            break
                if candidate is not None:
                    values.append(candidate)
                break
    return _only_distinct(values)


def _spatial_buyer_name(rows: list[tuple[int, list[_Word]]]) -> str | None:
    values: list[str] = []
    for row_index, (page, row) in enumerate(rows):
        label: tuple[int, int] | None = None
        for start in range(len(row)):
            for width in (2, 1):
                phrase = " ".join(
                    _normal(word.text) for word in row[start : start + width]
                )
                if phrase in {"bill to", "buyer"}:
                    label = start, start + width
                    break
            if label is not None:
                break
        if label is None:
            continue
        start, end = label
        label_left = row[start].x0
        right_boundary = row[0].page_width
        for position in range(end, len(row)):
            phrase = " ".join(
                _normal(word.text) for word in row[position : position + 2]
            )
            if phrase in {"ship to", "deliver to"}:
                right_boundary = row[position].x0
                break
        label_y = _row_y(row)
        label_height = max(word.height for word in row[start:end])
        for next_page, candidate_row in (item for item in rows[row_index + 1 :] if item[0] == page):
            gap = _row_y(candidate_row) - label_y
            if gap <= 0:
                continue
            if gap > max(90.0, label_height * 8.0):
                break
            candidate_words = [
                word for word in candidate_row
                if word.x >= label_left and word.x < right_boundary
            ]
            candidate = _clean_party_name(_join_words(candidate_words))
            if candidate is not None:
                values.append(candidate)
                break
    return _only_distinct(values)


def _spatial_compact_header(rows: list[tuple[int, list[_Word]]]) -> dict[str, str]:
    """Read an explicit aligned No./Date/PO header block.

    Bare ``No.`` and ``Date`` labels are too broad on their own.  They are
    accepted only when all three invoice labels share the same right-side
    column on one page and each has one visible value immediately to its right.
    """

    labels = {"no": "number", "date": "date", "po no": "po", "po number": "po"}
    records: list[tuple[int, str, float, float, str]] = []
    for page, row in rows:
        for start in range(len(row)):
            for width in (2, 1):
                end = start + width
                if end > len(row):
                    continue
                phrase = " ".join(_normal(word.text) for word in row[start:end])
                field = labels.get(phrase)
                if field is None:
                    continue
                if field == "date" and start and _normal(row[start - 1].text) == "due":
                    continue
                if field == "number" and start and _normal(row[start - 1].text) in {
                    "po", "lpo", "warehouse",
                }:
                    continue
                label_words = row[start:end]
                label_right = max(word.x1 for word in label_words)
                page_width = row[0].page_width
                candidate = next(
                    (
                        word.text.strip()
                        for word in row[end:]
                        if word.x0 >= label_right
                        and word.x0 - label_right <= page_width * 0.22
                    ),
                    None,
                )
                if candidate is None:
                    continue
                if field == "date":
                    parsed = _parse_compact_header_date(candidate)
                else:
                    compact = candidate.replace(" ", "")
                    parsed = (
                        compact
                        if _SAFE_IDENTIFIER.fullmatch(compact)
                        and any(character.isdigit() for character in compact)
                        else None
                    )
                if parsed is not None:
                    records.append((page, field, label_words[0].x, page_width, parsed))
                break

    valid: list[dict[str, str]] = []
    for page, _field, anchor, page_width, _value in records:
        nearby = [
            record
            for record in records
            if record[0] == page
            and abs(record[2] - anchor) <= max(18.0, page_width * 0.04)
        ]
        values = {
            field: {value for _page, candidate_field, _x, _width, value in nearby
                    if candidate_field == field}
            for field in {"number", "date", "po"}
        }
        if all(len(field_values) == 1 for field_values in values.values()):
            valid.append({field: next(iter(field_values)) for field, field_values in values.items()})
    distinct = {tuple(sorted(candidate.items())) for candidate in valid}
    return dict(next(iter(distinct))) if len(distinct) == 1 else {}


def _parse_compact_header_date(value: str) -> str | None:
    parsed = _parse_date(value)
    if parsed is not None:
        return parsed
    day_first = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value.strip())
    if day_first is None:
        return None
    day, month, year = map(int, day_first.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _unique_text(lines: list[str], patterns: tuple[re.Pattern[str], ...]) -> str | None:
    values: list[str] = []
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            value = " ".join(match.group(1).split()).strip(" :")
            if value and not _looks_dangerous(value):
                values.append(value[:160])
            break
    return _only_distinct(values)


def _extract_buyer_name(lines: list[str]) -> str | None:
    values: list[str] = []
    label = re.compile(r"(?i)^\s*(?:bill\s+to|buyer)\s*:?[ \t]*(.*)$")
    for index, line in enumerate(lines):
        match = label.match(line)
        if match is None:
            continue
        candidate = match.group(1)
        if not candidate and index + 1 < len(lines):
            candidate = lines[index + 1]
        cleaned = _clean_party_name(candidate)
        if cleaned is not None:
            values.append(cleaned)
    return _only_distinct(values)


def _clean_party_name(value: Any) -> str | None:
    candidate = " ".join(str(value or "").split()).strip(" :|-")
    while re.match(r"(?i)^(?:bill\s+to|buyer)\b", candidate):
        candidate = re.sub(
            r"(?i)^(?:bill\s+to|buyer)\b\s*:?[ \t|,-]*", "", candidate, count=1
        )
    candidate = re.split(
        r"(?i)\b(?:ship\s+to|deliver\s+to|p\.?\s*o\.?\s*box|address|tel(?:ephone)?|trn)\b",
        candidate,
        maxsplit=1,
    )[0].strip(" :|-,")
    if (
        not 2 <= len(candidate) <= 160
        or _looks_dangerous(candidate)
        or not any(character.isalpha() for character in candidate)
        or _normal(candidate) in {"bill to", "buyer", "ship to", "deliver to"}
    ):
        return None
    return candidate


def _only_distinct(values: Iterable[str]) -> str | None:
    distinct: dict[str, str] = {}
    for value in values:
        distinct.setdefault(value.casefold(), value)
    if len(distinct) != 1:
        return None
    return next(iter(distinct.values()))


def _extract_printed_date(lines: list[str]) -> str | None:
    """Preserve a labelled source date even when its locale is ambiguous."""
    date_pattern = r"(?:\d{1,4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,4}|\d{1,2}[- /][A-Za-z]{3,9}[- /]\d{4})"
    label = re.compile(r"(?i)\b(?:invoice|document)\s+date\s*:?\s*(" + date_pattern + r")(?!\d)")
    values = []
    for i, line in enumerate(lines):
        match = label.search(line)
        if match:
            values.append(match.group(1).strip())
        elif re.fullmatch(r"(?i)\s*(?:(?:tax\s+)?invoice\s+|document\s+)?date\s*:?\s*", line) and i + 1 < len(lines):
            if re.fullmatch(date_pattern, lines[i + 1].strip()):
                values.append(lines[i + 1].strip())
        else:
            bare = re.fullmatch(r"(?i)\s*date\s*:?\s*(" + date_pattern + r")\s*", line)
            if bare:
                values.append(bare.group(1).strip())
    normalized = {}
    for value in values:
        normalized.setdefault(re.sub(r"\s*([-/.])\s*", r"\1", value).casefold(), value)
    return next(iter(normalized.values())) if len(normalized) == 1 else None


def _extract_date(lines: list[str]) -> str | None:
    priorities = (
        re.compile(r"(?i)\bdocument\s+date\s*:?\s*([0-9A-Z./ -]{6,24})"),
        re.compile(r"(?i)\binvoice\s+date\s*:?\s*([0-9A-Z./ -]{6,24})"),
        re.compile(r"(?i)^\s*date\s*:?\s*([0-9A-Z./ -]{6,24})\s*$"),
        re.compile(r"(?i)(?<!report\srun\s)(?<!run\s)\bdate\s*:?\s*([0-9A-Z./ -]{6,24})"),
    )
    for pattern in priorities:
        values: list[str] = []
        for line in lines:
            match = pattern.search(line)
            if match:
                parsed = _parse_date(match.group(1).strip())
                if parsed:
                    values.append(parsed)
        if values:
            return _only_distinct(values)
    return None


def _parse_date(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value.strip()).rstrip(".,")
    cleaned = re.sub(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", cleaned)
    month_name = re.fullmatch(r"(\d{1,2})[- /.]([A-Za-z]{3,9})[- /.](\d{4})", cleaned)
    if month_name:
        try:
            parsed = date.fromisoformat(
                f"{int(month_name.group(3)):04d}-"
                f"{_month_number(month_name.group(2)):02d}-{int(month_name.group(1)):02d}"
            )
            return parsed.isoformat()
        except (ValueError, TypeError):
            return None
    iso = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", cleaned)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None
    numeric = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", cleaned)
    if numeric:
        day, month, year = map(int, numeric.groups())
        if day <= 12 < month:
            day, month = month, day
        elif day <= 12 and day != month:
            return None
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _month_number(value: str) -> int:
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "sept": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    key = value.casefold()[:4] if value.casefold().startswith("sept") else value.casefold()[:3]
    if key not in months:
        raise ValueError("unknown month")
    return months[key]


def _extract_currency(lines: list[str]) -> str | None:
    values: list[str] = []
    patterns = (
        re.compile(r"(?i)\bcurrency\s*:\s*([A-Z]{3})\b"),
        re.compile(r"(?i)\btotal\s+in\s+([A-Z]{3})\b"),
        re.compile(r"(?i)^\s*total\s+([A-Z]{3})\s+[0-9][0-9,.]*\s*$"),
        re.compile(r"(?i)\bamount\s+(AED|USD|EUR|GBP|KWD|SAR|QAR|BHD|OMR)\b"),
    )
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                candidate = match.group(1).upper()
                if _CURRENCY.fullmatch(candidate):
                    values.append(candidate)
                break
    return _only_distinct(values)


def _extract_money(lines: list[str], patterns: tuple[re.Pattern[str], ...]) -> Decimal | None:
    values: list[Decimal] = []
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            parsed = _parse_decimal(match.group(1))
            if parsed is not None:
                values.append(parsed)
            break
    distinct = {value for value in values}
    if len(distinct) != 1:
        return None
    return next(iter(distinct))


def _parse_decimal(value: str) -> Decimal | None:
    cleaned = value.strip().replace(",", "")
    if not _PLAIN_NUMBER.fullmatch(cleaned):
        return None
    try:
        result = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not result.is_finite() or result < 0 or len(result.as_tuple().digits) > 24:
        return None
    return result


def _extract_spatial_totals(words: list[_Word]) -> tuple[Decimal | None, Decimal | None]:
    """Read printed invoice net and tax from an explicit currency total row."""

    nets: list[Decimal] = []
    taxes: list[Decimal] = []
    for page in sorted({word.page for word in words}):
        page_words = [word for word in words if word.page == page]
        rows = _group_page_rows(page_words)
        if not rows:
            continue
        for row in rows:
            normal = _normal(_row_text(row))
            numbers = [
                value
                for word in row
                if (value := _parse_decimal(word.text)) is not None
            ]
            if not numbers:
                continue
            if re.search(r"\b(?:sub\s*total|subtotal|net amount)\b", normal):
                nets.append(numbers[-1])
            if re.match(r"^(?:vat|tax)\b", normal):
                taxes.append(numbers[-1])
        typical_height = median(word.height for word in page_words)
        index = 0
        while index < len(rows):
            columns, header_end = _find_header(rows, index, typical_height)
            if columns is None:
                break
            for row in rows[header_end + 1 :]:
                normal = _normal(_row_text(row))
                if not re.search(r"\btotal\s+in\s+[a-z]{3}\b", normal):
                    continue
                net = _decimal_in_bounds(row, columns.net_bounds)
                tax = _decimal_in_bounds(row, columns.tax_bounds)
                if net is not None:
                    nets.append(net)
                if tax is not None:
                    taxes.append(tax)
                break
            index = header_end + 1

    def only(values: list[Decimal]) -> Decimal | None:
        distinct = set(values)
        return next(iter(distinct)) if len(distinct) == 1 else None

    return only(nets), only(taxes)


def _extract_table_lines(words: list[_Word]) -> list[Line]:
    result: list[Line] = []
    for page in sorted({word.page for word in words}):
        page_words = [word for word in words if word.page == page]
        rows = _group_page_rows(page_words)
        if not rows:
            continue
        typical_height = median(word.height for word in page_words)
        index = 0
        while index < len(rows):
            columns, header_end = _find_header(rows, index, typical_height)
            if columns is None:
                break
            index = header_end + 1
            draft: _DraftLine | None = None
            pending: list[list[_Word]] = []
            while index < len(rows):
                row = rows[index]
                row_text = _row_text(row)
                normal = _normal(row_text)
                if _is_totals_or_footer(normal):
                    break
                next_columns, _ = _find_header(rows, index, typical_height)
                if next_columns is not None:
                    break
                parts = _row_parts(row, columns)
                qty = _first_decimal(parts["qty"])
                price = _first_decimal(parts["price"])
                sku = _select_sku(parts["item"])
                description = _clean_description_words(parts["description"])
                gtin = _extract_gtin_words(parts["description"])
                if (
                    qty is not None
                    and price is not None
                    and (sku is not None or gtin is not None or description is not None)
                ):
                    if draft is not None:
                        result.append(_finish_line(draft))
                    draft = _DraftLine(
                        sku=sku,
                        description=description,
                        uom=_clean_uom(_join_words(parts["uom"])),
                        qty=qty,
                        price=price,
                        evidence_parts=[row_text],
                        page=page,
                        last_y=_row_y(row),
                        gtin=gtin,
                        net_amount=_decimal_in_bounds(row, columns.net_bounds),
                        tax_amount=_decimal_in_bounds(row, columns.tax_bounds),
                    )
                    for prior in pending:
                        _merge_continuation(draft, prior, columns, prepend=True)
                    pending.clear()
                elif draft is not None and _is_near_row(draft, row, typical_height):
                    _merge_continuation(draft, row, columns, prepend=False)
                elif _has_continuation_content(parts):
                    pending.append(row)
                    if len(pending) > 2:
                        pending.pop(0)
                index += 1
            if draft is not None:
                result.append(_finish_line(draft))
            if index == header_end + 1:
                index += 1
    if len(result) > 1000:
        raise ValueError("Invoice contains more than the supported 1,000 line items")
    return result


def _find_header(
    rows: list[list[_Word]], start: int, typical_height: float
) -> tuple[_Columns | None, int]:
    for index in range(start, len(rows)):
        anchors = _header_anchors(rows[index])
        end = index
        if not {"description", "qty", "price"}.issubset(anchors) and index + 1 < len(rows):
            gap = abs(_row_y(rows[index + 1]) - _row_y(rows[index]))
            if gap <= max(8.0, typical_height * 2.0):
                combined = dict(anchors)
                combined.update(
                    {key: value for key, value in _header_anchors(rows[index + 1]).items() if key not in combined}
                )
                if len(combined) > len(anchors):
                    anchors = combined
                    end = index + 1
        if all(key in anchors for key in ("description", "qty", "price")):
            item = anchors.get("item")
            description = anchors["description"]
            uom = anchors.get("uom")
            qty = anchors["qty"]
            price = anchors["price"]
            serial = anchors.get("serial")
            identity_columns = [value for value in (serial, item, description, uom) if value is not None]
            if (
                not description < qty < price
                or any(value >= qty for value in identity_columns)
                or len(set((*identity_columns, qty, price))) != len(identity_columns) + 2
            ):
                continue
            later = sorted(
                word.x
                for word in rows[index] + (rows[end] if end != index else [])
                if word.x > price + 4 and _normal(word.text) in _AFTER_PRICE_HEADERS
            )
            page_width = rows[index][0].page_width
            price_right = (price + later[0]) / 2 if later else page_width
            net_bounds, tax_bounds = _financial_column_bounds(
                rows, index, end, price, typical_height
            )
            return _Columns(
                item=item,
                description=description,
                uom=uom,
                qty=qty,
                price=price,
                price_right=price_right,
                serial=serial,
                net_bounds=net_bounds,
                tax_bounds=tax_bounds,
            ), end
    return None, start


_AFTER_PRICE_HEADERS = {
    "amount",
    "amt",
    "disc",
    "discount",
    "gross",
    "net",
    "rate",
    "subtotal",
    "tax",
    "total",
    "value",
    "vat",
}


def _header_anchors(row: list[_Word]) -> dict[str, float]:
    anchors: dict[str, float] = {}
    repeated_spanned_headers: list[_Word] = []
    for word in row:
        value = _normal(word.text)
        compact = re.sub(r"[^a-z0-9]", "", word.text.casefold())
        if all(part in compact for part in ("itemcode", "description", "uom", "qty")):
            repeated_spanned_headers.append(word)
        serial_part = next(
            (part for part in ("serialnumber", "serialno", "srno", "sno") if part in compact),
            None,
        )
        item_part = next(
            (part for part in ("itemnumber", "itemcode", "itemno") if part in compact), None
        )
        description_part = next(
            (
                part
                for part in (
                    "productdescription",
                    "itemdescription",
                    "servicedescription",
                    "description",
                    "productname",
                    "itemname",
                    "particulars",
                    "desc",
                )
                if part in compact
            ),
            None,
        )
        uom_part = next(
            (part for part in ("unitofmeasure", "measureunit", "uom") if part in compact),
            None,
        )
        qty_part = next(
            (part for part in ("quantity", "qty") if part in compact),
            None,
        )
        price_part = next(
            (part for part in ("sellingprice", "unitprice", "priceperunit", "price", "rate") if part in compact),
            None,
        )
        if serial_part:
            anchors.setdefault("serial", _substring_x(word, compact, serial_part))
        elif value in {"s no", "sno", "sr", "sr no", "serial", "serial no", "line no", "line number"}:
            anchors.setdefault("serial", word.x)
        if item_part:
            anchors.setdefault("item", _substring_x(word, compact, item_part))
        elif re.fullmatch(r"item\s*(?:no|number|code)?|sku|product\s*code", value):
            anchors.setdefault("item", word.x)
        if description_part:
            anchors.setdefault("description", _substring_x(word, compact, description_part))
        elif value.startswith("desc") or value in {
            "name",
            "item name",
            "product name",
            "service description",
            "item description",
            "product description",
            "particulars",
        }:
            anchors.setdefault("description", word.x)
        if uom_part:
            anchors.setdefault("uom", _substring_x(word, compact, uom_part))
        elif value in {"uom", "u m", "unit", "unit of measure"}:
            anchors.setdefault("uom", word.x)
        if qty_part:
            anchors.setdefault("qty", _substring_x(word, compact, qty_part))
        elif value in {"qty", "quantity"}:
            anchors.setdefault("qty", word.x)
        if price_part:
            anchors.setdefault("price", _substring_x(word, compact, price_part))
        elif value in {"price", "unit price", "unitprice", "rate"}:
            anchors.setdefault("price", word.x)

    if len(repeated_spanned_headers) >= 3:
        # Some Docling scan tables repeat one merged header string in each
        # covered physical cell. The measured cell centres still identify the
        # UOM and quantity columns; the first wide cell retains item/description.
        repeated_spanned_headers.sort(key=lambda word: word.x)
        anchors["uom"] = repeated_spanned_headers[1].x
        anchors["qty"] = repeated_spanned_headers[2].x

    ordered = sorted(row, key=lambda word: word.x)
    phrases = {
        "product name": "description",
        "item name": "description",
        "product description": "description",
        "item description": "description",
        "service description": "description",
        "unit price": "price",
        "s no": "serial",
        "sr no": "serial",
        "line no": "serial",
        "line number": "serial",
    }
    for first, second in zip(ordered, ordered[1:]):
        phrase = f"{_normal(first.text)} {_normal(second.text)}"
        role = phrases.get(phrase)
        if role is None:
            continue
        centre = (first.x + second.x) / 2
        anchors.setdefault(role, centre)
        if role == "price" and anchors.get("uom") == first.x:
            anchors.pop("uom")
    return anchors


def _substring_x(word: _Word, compact: str, part: str) -> float:
    start = compact.find(part)
    if start < 0 or not compact:
        return word.x
    centre = start + len(part) / 2
    return word.x0 + (word.x1 - word.x0) * centre / len(compact)


def _financial_column_bounds(
    rows: list[list[_Word]], index: int, end: int, price: float, typical_height: float
) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    header_rows = list(rows[index : end + 1])
    if index > 0 and abs(_row_y(rows[index]) - _row_y(rows[index - 1])) <= max(
        12.0, typical_height * 2.0
    ):
        header_rows.insert(0, rows[index - 1])

    main_row = rows[end]
    labels: list[tuple[str, float]] = []
    repeated_financial = sorted(
        (
            word for word in main_row
            if all(
                part in re.sub(r"[^a-z0-9]", "", word.text.casefold())
                for part in ("value", "disc", "subtotal")
            )
        ),
        key=lambda word: word.x,
    )
    if len(repeated_financial) >= 3:
        labels.extend(("other", word.x) for word in repeated_financial[:2])
        labels.append(("net", repeated_financial[2].x))
        labels.extend(("other", word.x) for word in repeated_financial[3:])
    repeated_vat_total = sorted(
        (
            word for word in main_row
            if all(
                part in re.sub(r"[^a-z0-9]", "", word.text.casefold())
                for part in ("amt", "total")
            )
        ),
        key=lambda word: word.x,
    )
    if len(repeated_vat_total) >= 2:
        labels.append(("tax", repeated_vat_total[0].x))
        labels.extend(("other", word.x) for word in repeated_vat_total[1:])
    consumed: set[int] = set()
    for position, word in enumerate(main_row):
        value = _normal(word.text)
        following = _normal(main_row[position + 1].text) if position + 1 < len(main_row) else ""
        if value == "sub" and following == "total":
            labels.append(("net", (word.x + main_row[position + 1].x) / 2))
            consumed.update((position, position + 1))
        elif value in {"subtotal", "sub total", "net amount", "taxable value"}:
            labels.append(("net", word.x))
            consumed.add(position)

    upper_words = [word for row in header_rows[:-1] for word in row]
    for position, word in enumerate(main_row):
        if position in consumed or word.x <= price + 4:
            continue
        value = _normal(word.text)
        kind = "other"
        if value in {"amt", "amount"} and any(
            _normal(upper.text) in {"vat", "tax"}
            and abs(upper.x - word.x) <= max(30.0, word.height * 2.5)
            for upper in upper_words
        ):
            kind = "tax"
        elif value in {"vat amount", "vat amt", "tax amount", "tax amt"}:
            kind = "tax"
        elif value not in _AFTER_PRICE_HEADERS:
            continue
        labels.append((kind, word.x))

    # Preserve the unit-price boundary even when its label is split across rows.
    centres = sorted({price, *(centre for _, centre in labels)})
    if len(centres) < 2:
        return None, None

    def bounds(kind: str) -> tuple[float, float] | None:
        targets = [centre for label, centre in labels if label == kind]
        if len(targets) != 1:
            return None
        target = targets[0]
        position = centres.index(target)
        left = (centres[position - 1] + target) / 2 if position else 0.0
        right = (
            (target + centres[position + 1]) / 2
            if position + 1 < len(centres)
            else main_row[0].page_width
        )
        return left, right

    return bounds("net"), bounds("tax")


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _row_y(row: list[_Word]) -> float:
    return sum(word.y for word in row) / len(row)


def _row_parts(row: list[_Word], columns: _Columns) -> dict[str, list[_Word]]:
    parts = {"item": [], "description": [], "uom": [], "qty": [], "price": []}
    anchors: list[tuple[str, float]] = []
    if columns.serial is not None:
        anchors.append(("serial", columns.serial))
    if columns.item is not None:
        anchors.append(("item", columns.item))
    anchors.append(("description", columns.description))
    if columns.uom is not None:
        anchors.append(("uom", columns.uom))
    anchors.extend((("qty", columns.qty), ("price", columns.price)))
    anchors.sort(key=lambda anchor: anchor[1])
    boundaries: list[float] = []
    for (left_role, left), (_right_role, right) in zip(anchors, anchors[1:]):
        if left_role == "serial" and _right_role == "item":
            # A serial column is normally narrow.  Keep its numeric row marker
            # out of the SKU while allowing a left-aligned item code beneath a
            # merged "S.No Item Code" heading.
            boundaries.append(left + (right - left) * 0.25)
        elif left_role == "description" and columns.uom is not None:
            boundaries.append(right - max(3.0, (right - left) * 0.08))
        else:
            boundaries.append((left + right) / 2)
    for word in row:
        if word.x >= columns.price_right:
            continue
        column = len(anchors) - 1
        for position, boundary in enumerate(boundaries):
            if word.x < boundary:
                column = position
                break
        role = anchors[column][0]
        if role in parts:
            parts[role].append(word)
    return parts


def _join_words(words: list[_Word]) -> str:
    if not words:
        return ""
    tolerance = max(2.0, median(word.height for word in words) * 0.45)
    lines: list[list[_Word]] = []
    centres: list[float] = []
    for word in sorted(words, key=lambda item: (item.y, item.x)):
        if lines and abs(word.y - centres[-1]) <= tolerance:
            lines[-1].append(word)
            centres[-1] = sum(item.y for item in lines[-1]) / len(lines[-1])
        else:
            lines.append([word])
            centres.append(word.y)
    return " ".join(
        word.text for line in lines for word in sorted(line, key=lambda item: item.x)
    ).strip()


def _first_decimal(words: list[_Word]) -> Decimal | None:
    if any(_normal(word.text) in {"", "comma", "dot"} and not word.text.isalnum() for word in words):
        joined = "".join(word.text.strip() for word in sorted(words, key=lambda item: item.x))
        return _parse_decimal(joined)
    values = [_parse_decimal(word.text) for word in words]
    valid = [value for value in values if value is not None]
    if len(valid) != 1:
        return None
    return valid[0]


def _decimal_in_bounds(
    row: list[_Word], bounds: tuple[float, float] | None
) -> Decimal | None:
    if bounds is None:
        return None
    left, right = bounds
    return _first_decimal([word for word in row if left <= word.x < right])


def _select_sku(words: list[_Word]) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for word in words:
        value = word.text.strip().replace(" ", "")
        if not _SAFE_IDENTIFIER.fullmatch(value):
            continue
        normal = _normal(value)
        if normal in {"item", "no", "number", "s no", "serial"}:
            continue
        score = (3 if any(character.isalpha() for character in value) else 1) + (
            1 if any(character in "-_/" for character in value) else 0
        )
        candidates.append((score, len(value), value))
    if not candidates:
        return None
    alpha_parts = [
        word.text.strip().replace(" ", "")
        for word in sorted(words, key=lambda item: (item.y, item.x))
        if _SAFE_IDENTIFIER.fullmatch(word.text.strip().replace(" ", ""))
        and any(character.isalpha() for character in word.text)
    ]
    joined = "".join(alpha_parts)
    if len(alpha_parts) > 1 and _SAFE_IDENTIFIER.fullmatch(joined):
        return joined
    return max(candidates)[2]


def _clean_description(value: str) -> str | None:
    if not value:
        return None
    cleaned = _GTIN.sub("", value)
    cleaned = " ".join(cleaned.split()).strip(" -,:;")
    if not cleaned or _looks_dangerous(cleaned):
        return None
    return cleaned


def _extract_gtin(value: str) -> str | None:
    match = _GTIN.search(value)
    return match.group(1).replace(" ", "") if match else None


def _extract_gtin_words(words: list[_Word]) -> str | None:
    joined = _join_words(words)
    labeled = _extract_gtin(joined)
    if labeled:
        return labeled
    if not any(_normal(word.text) in {"ean", "gtin", "upc"} for word in words):
        return None
    values = {
        word.text.strip().replace(" ", "")
        for word in words
        if re.fullmatch(r"[0-9]{8,18}", word.text.strip().replace(" ", ""))
    }
    return next(iter(values)) if len(values) == 1 else None


def _clean_description_words(words: list[_Word]) -> str | None:
    gtin = _extract_gtin_words(words)
    kept: list[_Word] = []
    for word in words:
        value = word.text.strip()
        normal = _normal(value)
        compact = value.replace(" ", "")
        if normal in {"ean", "gtin", "upc"} or value in {":", "#"}:
            continue
        if gtin and compact == gtin:
            continue
        kept.append(word)
    return _clean_description(_join_words(kept))


def _clean_uom(value: str) -> str | None:
    cleaned = " ".join(value.split()).strip(" :")
    if not cleaned or len(cleaned) > 30 or _looks_dangerous(cleaned):
        return None
    return cleaned


def _looks_dangerous(value: str) -> bool:
    return value.lstrip().startswith(("=", "+", "-", "@")) or any(ord(char) < 32 for char in value)


def _is_totals_or_footer(normal: str) -> bool:
    return bool(
        re.search(
            r"\b(?:net amount|tax amount|vat amount|total amount|sub total|subtotal|continued|page total)\b",
            normal,
        )
    )


def _has_continuation_content(parts: dict[str, list[_Word]]) -> bool:
    return bool(parts["item"] or parts["description"])


def _is_near_row(draft: _DraftLine, row: list[_Word], typical_height: float) -> bool:
    # Continuation lines immediately follow their priced row.  A wider limit is
    # still bounded enough to exclude page totals and footers, which are stopped
    # before this check.
    return bool(row) and 0 <= _row_y(row) - draft.last_y <= max(12.0, typical_height * 2.2)


def _merge_continuation(
    draft: _DraftLine, row: list[_Word], columns: _Columns, *, prepend: bool
) -> None:
    parts = _row_parts(row, columns)
    row_text = _row_text(row)
    item = _select_sku(parts["item"])
    description_text = _join_words(parts["description"])
    gtin_match = _GTIN.search(description_text) or _GTIN.search(row_text)
    if gtin_match and draft.gtin is None:
        draft.gtin = gtin_match.group(1).replace(" ", "")
    description = _clean_description(description_text)
    if item and len(item) > 1:
        if draft.sku is None:
            draft.sku = item
        else:
            draft.sku = item + draft.sku if prepend else draft.sku + item
    if description:
        if draft.description:
            draft.description = (
                f"{description} {draft.description}" if prepend else f"{draft.description} {description}"
            )
        else:
            draft.description = description
    if row_text:
        if prepend:
            draft.evidence_parts.insert(0, row_text)
        else:
            draft.evidence_parts.append(row_text)
    draft.last_y = _row_y(row)


def _finish_line(draft: _DraftLine) -> Line:
    evidence = " | ".join(dict.fromkeys(part for part in draft.evidence_parts if part))[:500]
    return Line(
        sku=draft.sku,
        gtin=draft.gtin,
        description=draft.description,
        qty=draft.qty,
        uom=draft.uom,
        price=draft.price,
        net_amount=draft.net_amount,
        tax_amount=draft.tax_amount,
        evidence=evidence or None,
        page=draft.page,
    )
