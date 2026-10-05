"""Lossless, source-grounded adapters for Docling document tables."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _page_size(document: Any, page: int) -> list[float] | None:
    pages = _value(document, "pages", {})
    item = pages.get(page) if hasattr(pages, "get") else None
    if item is None and hasattr(pages, "get"):
        item = pages.get(page - 1)
    size = _value(item, "size")
    width, height = _value(size, "width"), _value(size, "height")
    if width is None or height is None:
        return None
    return [float(width), float(height)]


def normalize_bbox(bbox: Any, size: list[float] | None) -> tuple[list[float] | None, str]:
    """Return a top-left bbox without guessing when the page height is absent."""
    if bbox is None:
        return None, "unknown"
    origin = str(_value(bbox, "coord_origin", "unknown"))
    values = [_value(bbox, key) for key in ("l", "t", "r", "b")]
    if any(value is None for value in values):
        return None, origin
    left, top, right, bottom = (float(value) for value in values)
    if "BOTTOMLEFT" in origin.upper():
        if not size:
            return None, origin
        top, bottom = size[1] - top, size[1] - bottom
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)], origin


def _word_boxes(parsed_pages: list[Any] | None) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    for page_index, page_item in enumerate(parsed_pages or [], 1):
        parsed = _value(page_item, "parsed_page")
        words = list(_value(parsed, "word_cells", []) or [])
        page_size = _value(page_item, "size")
        size = None
        if page_size is not None:
            size = [float(_value(page_size, "width")), float(_value(page_size, "height"))]
        for word in words:
            rect = _value(word, "rect")
            source_origin = str(_value(rect, "coord_origin", "unknown"))
            xs = [float(_value(rect, f"r_x{index}")) for index in range(4)]
            ys = [float(_value(rect, f"r_y{index}")) for index in range(4)]
            if "BOTTOMLEFT" in source_origin.upper():
                if not size:
                    continue
                ys = [size[1] - value for value in ys]
            text = str(_value(word, "text", "") or "").strip()
            if not text:
                continue
            boxes.append({
                "text": text,
                "page": page_index,
                "box": [min(xs), min(ys), max(xs), max(ys)],
                "size": size,
                "coordinate_system": "top-left",
                "source_coordinate_system": source_origin,
                "geometry": "word",
                "source_type": "TextCell",
                "estimated": False,
                "confidence": float(_value(word, "confidence", 1.0)),
                "from_ocr": bool(_value(word, "from_ocr", False)),
            })
    return boxes


def document_payload(
    document: Any, parsed_pages: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert a DoclingDocument to text, measured blocks, and lossless tables."""
    boxes = _word_boxes(parsed_pages)
    include_blocks = not boxes
    tables: list[dict[str, Any]] = []
    for item, _level in document.iterate_items():
        item_type = type(item).__name__
        text = str(_value(item, "text", "") or "")
        provenance = list(_value(item, "prov", []) or [])
        for prov in provenance:
            page = int(_value(prov, "page_no", 1))
            size = _page_size(document, page)
            box, source_origin = normalize_bbox(_value(prov, "bbox"), size)
            if include_blocks and text and box is not None:
                boxes.append({
                    "text": text,
                    "page": page,
                    "box": box,
                    "size": size,
                    "coordinate_system": "top-left",
                    "source_coordinate_system": source_origin,
                    "geometry": "block",
                    "source_type": item_type,
                    "estimated": False,
                })

        data = _value(item, "data")
        cells = list(_value(data, "table_cells", []) or [])
        if not cells:
            continue
        prov = provenance[0] if provenance else None
        page = int(_value(prov, "page_no", 1))
        size = _page_size(document, page)
        table_box, table_origin = normalize_bbox(_value(prov, "bbox"), size)
        row_count = int(_value(data, "num_rows", 0) or 0)
        column_count = int(_value(data, "num_cols", 0) or 0)
        rows: list[list[str | None]] = [[None] * column_count for _ in range(row_count)]
        public_cells: list[dict[str, Any]] = []
        for cell in cells:
            row = int(_value(cell, "start_row_offset_idx", 0) or 0)
            column = int(_value(cell, "start_col_offset_idx", 0) or 0)
            row_span = int(_value(cell, "row_span", 1) or 1)
            column_span = int(_value(cell, "col_span", 1) or 1)
            cell_text = str(_value(cell, "text", "") or "").strip()
            cell_box, cell_origin = normalize_bbox(_value(cell, "bbox"), size)
            for row_offset in range(row_span):
                for column_offset in range(column_span):
                    target_row, target_column = row + row_offset, column + column_offset
                    if target_row < row_count and target_column < column_count:
                        rows[target_row][target_column] = cell_text or None
            public_cells.append({
                "text": cell_text,
                "row": row,
                "row_span": row_span,
                "column": column,
                "column_span": column_span,
                "is_header": bool(_value(cell, "column_header", False)),
                "box": cell_box,
                "size": size,
                "coordinate_system": "top-left" if cell_box is not None else None,
                "source_coordinate_system": cell_origin,
            })
        tables.append({
            "page": page,
            "rows": rows,
            "cells": public_cells,
            "box": table_box,
            "size": size,
            "coordinate_system": "top-left" if table_box is not None else None,
            "source_coordinate_system": table_origin,
            "source": "docling",
        })
    tables.extend(_tables_from_measured_words(boxes))
    return document.export_to_markdown(), boxes, tables


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE))


def _role(header: Any) -> str | None:
    value = _normalized(header)
    packed = value.replace(" ", "")
    checks = (
        ("serial", ("serialnumber", "serialno", "srno", "sr")),
        ("gtin", ("barcode", "gtin", "ean", "upc")),
        ("description", (
            "productdescription", "itemdescription", "description", "productname",
            "itemname", "servicedescription", "particulars", "name",
        )),
        ("sku", ("productreference", "itemcode", "itemnumber", "sku", "article")),
        ("qty", ("quantity", "qtyinpce", "qtypce", "qty")),
        ("uom", ("unitofmeasure", "uom", "measureunit")),
        ("tax_amount", ("taxamount", "vatamount", "taxamt", "vatamt")),
        ("tax_rate", ("taxrate", "vatrate", "taxpercent")),
        ("net_amount", ("taxablevalue", "netamount", "lineamount", "totalprice", "subtotal")),
        ("gross_amount", ("grossamount", "amountincludingtax")),
        ("price", ("sellingprice", "unitprice", "priceperunit", "price")),
    )
    for role, aliases in checks:
        if any(alias in packed for alias in aliases):
            return role
    return None


def _exact_header_role(header: Any) -> str | None:
    packed = _normalized(header).replace(" ", "")
    aliases = {
        "serial": {"sr", "srno", "serialno", "serialnumber"},
        "gtin": {"barcode", "gtin", "ean", "upc"},
        "description": {
            "description", "productdescription", "itemdescription", "productname",
            "itemname", "servicedescription", "particulars", "name",
        },
        "size": {"size"},
        "sku": {"productreference", "itemcode", "itemnumber", "sku", "article"},
        "qty": {"qty", "quantity", "qtyinpce", "qtypce"},
        "uom": {"uom", "unitofmeasure", "measureunit"},
        "price": {"price", "sellingprice", "unitprice", "priceperunit"},
        "net_amount": {
            "amount", "taxablevalue", "netamount", "lineamount", "totalprice", "subtotal",
        },
        "tax_rate": {"taxrate", "vatrate", "taxpercent"},
        "tax_amount": {"taxamount", "vatamount", "taxamt", "vatamt"},
        "gross_amount": {"grossamount", "amountincludingtax", "total"},
    }
    return next((role for role, values in aliases.items() if packed in values), None)


def _rows_from_words(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not words:
        return []
    typical = median(max(1.0, float(word["box"][3]) - float(word["box"][1])) for word in words)
    tolerance = max(3.0, typical * 0.6)
    rows: list[list[dict[str, Any]]] = []
    centres: list[float] = []
    for word in sorted(words, key=lambda value: (
        (float(value["box"][1]) + float(value["box"][3])) / 2,
        float(value["box"][0]),
    )):
        centre = (float(word["box"][1]) + float(word["box"][3])) / 2
        if rows and abs(centre - centres[-1]) <= tolerance:
            rows[-1].append(word)
            centres[-1] = sum(
                (float(item["box"][1]) + float(item["box"][3])) / 2 for item in rows[-1]
            ) / len(rows[-1])
        else:
            rows.append([word])
            centres.append(centre)
    return [sorted(row, key=lambda value: float(value["box"][0])) for row in rows]


def _header_roles(row: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    found: dict[str, tuple[int, float, float]] = {}
    for start in range(len(row)):
        for width in (1, 2, 3):
            cells = row[start : start + width]
            if len(cells) != width:
                continue
            role = _exact_header_role(" ".join(str(cell["text"]) for cell in cells))
            if role is None:
                continue
            if role not in found or width > found[role][0]:
                found[role] = (
                    width,
                    min(float(cell["box"][0]) for cell in cells),
                    max(float(cell["box"][2]) for cell in cells),
                )
    return sorted(
        ((role, value[1], value[2]) for role, value in found.items()),
        key=lambda value: value[1],
    )


def _tables_from_measured_words(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover missed borderless tables from real Docling word cells.

    Every value is assigned by its measured x coordinate beneath an explicit
    semantic header. No block is split and no coordinate is synthesized.
    """
    result: list[dict[str, Any]] = []
    for page in sorted({int(box.get("page", 1)) for box in boxes}):
        page_words = [box for box in boxes if int(box.get("page", 1)) == page]
        rows = _rows_from_words(page_words)
        index = 0
        while index < len(rows):
            roles = _header_roles(rows[index])
            required = {"description", "qty", "price"}
            if not required.issubset({role for role, _left, _right in roles}):
                index += 1
                continue
            boundaries = []
            for left_role, right_role in zip(roles, roles[1:]):
                gap = max(0.0, right_role[1] - left_role[2])
                header_width = max(1.0, left_role[2] - left_role[1])
                if left_role[0] == "sku":
                    # Product references are compact and left aligned, while
                    # descriptions can start well before their long heading.
                    boundary = left_role[2] + min(gap / 2, max(4.0, header_width * 0.15))
                else:
                    boundary = left_role[2] + gap / 2
                boundaries.append(boundary)
            bounds = [float("-inf"), *boundaries, float("inf")]
            table_rows: list[list[str | None]] = [[role for role, _left, _right in roles]]
            header_text = _normalized(" ".join(str(word["text"]) for word in rows[index]))
            unit_match = re.search(r"\bqty\s+in\s+([a-z0-9]{1,10})\b", header_text)
            fixed_values = {"uom": unit_match.group(1).upper()} if unit_match else {}
            index += 1
            while index < len(rows):
                row = rows[index]
                normal = _normalized(" ".join(str(word["text"]) for word in row))
                if any(label in normal for label in (
                    "total qty", "total price", "net amount", "total in", "amount in words",
                )):
                    break
                if required.issubset({role for role, _left, _right in _header_roles(row)}):
                    break
                values: list[str | None] = []
                for column in range(len(roles)):
                    words = [word for word in row if bounds[column] <= (
                        (float(word["box"][0]) + float(word["box"][2])) / 2
                    ) < bounds[column + 1]]
                    value = " ".join(str(word["text"]).strip() for word in words).strip()
                    values.append(value or None)
                mapped = {roles[column][0]: values[column] for column in range(len(roles))}
                identity = any(
                    mapped.get(role) for role in ("sku", "gtin", "description")
                )
                price = _decimal(mapped.get("price"))
                has_quantity_or_amount = (
                    _decimal(mapped.get("qty")) is not None
                    or _decimal(mapped.get("net_amount")) is not None
                )
                if identity and price is not None and has_quantity_or_amount:
                    table_rows.append(values)
                index += 1
            if len(table_rows) > 1:
                result.append({
                    "page": page,
                    "rows": table_rows,
                    "cells": [],
                    "box": None,
                    "size": page_words[0].get("size") if page_words else None,
                    "coordinate_system": "top-left",
                    "source": "docling_word_cells",
                    "reconstruction": "measured_column_alignment",
                    "fixed_values": fixed_values,
                })
            if index < len(rows):
                index += 1
    return result


def _decimal(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    match = re.search(r"[-+]?\d[\d, ]*(?:\.\d+)?", text)
    if not match:
        return None
    number = match.group(0).replace(",", "").replace(" ", "")
    try:
        parsed = Decimal(number)
    except InvalidOperation:
        return None
    if negative:
        parsed = -abs(parsed)
    return format(parsed, "f")


def _gtin(value: Any) -> str | None:
    raw = str(value or "").strip()
    matches = []
    for match in re.finditer(r"(?<!\d)(?:\d[ -]?){7,17}\d(?!\d)", raw):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) in {8, 12, 13, 14}:
            matches.append(digits)
    distinct = list(dict.fromkeys(matches))
    if len(distinct) == 1:
        return distinct[0]
    return raw or None


def _table_rows(table: dict[str, Any]) -> list[list[str | None]]:
    rows = table.get("rows")
    if isinstance(rows, list):
        return [[None if cell is None else str(cell) for cell in row] for row in rows]
    cells = table.get("cells") or []
    row_count = max((int(cell.get("row", 0)) + int(cell.get("row_span", 1)) for cell in cells), default=0)
    col_count = max((int(cell.get("column", 0)) + int(cell.get("column_span", 1)) for cell in cells), default=0)
    result: list[list[str | None]] = [[None] * col_count for _ in range(row_count)]
    for cell in cells:
        row, col = int(cell.get("row", 0)), int(cell.get("column", 0))
        for row_offset in range(max(1, int(cell.get("row_span", 1)))):
            for col_offset in range(max(1, int(cell.get("column_span", 1)))):
                result[row + row_offset][col + col_offset] = str(cell.get("text") or "") or None
    return result


def _semantic_row_roles(row: list[str | None]) -> dict[int, str]:
    roles = {index: role for index, cell in enumerate(row) if (role := _role(cell))}
    merged = [
        index
        for index, cell in enumerate(row)
        if all(marker in _normalized(cell).replace(" ", "") for marker in (
            "itemcode", "description",
        ))
        and any(marker in _normalized(cell).replace(" ", "") for marker in (
            "sno", "srno", "serial",
        ))
    ]
    if merged:
        # TableFormer can merge the first three visible headings into a cell
        # spanning the serial and item-code columns, leaving the immediately
        # following description column blank. Preserve their printed order.
        start = merged[0]
        if start + 2 < len(row):
            roles[start] = "serial"
            roles[start + 1] = "sku"
            roles[start + 2] = "description"
    return roles


def _is_line_header(roles: dict[int, str]) -> bool:
    values = set(roles.values())
    return {"qty", "price"}.issubset(values) and bool(
        values & {"sku", "gtin", "description"}
    )


_HEADER_LABELS = {
    "tax invoice no": "number",
    "tax invoice number": "number",
    "invoice no": "number",
    "invoice number": "number",
    "tax invoice date": "date",
    "invoice date": "date",
    "document date": "date",
    "customer po no": "po",
    "customer po number": "po",
    "purchase order no": "po",
    "purchase order number": "po",
    "po no": "po",
    "po number": "po",
    "currency": "currency",
}


def _date_value(value: Any) -> str | None:
    raw = str(value or "").strip()
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        return raw
    day_first = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if day_first:
        day, month, year = map(int, day_first.groups())
        try:
            from datetime import date

            return date(year, month, day).isoformat()
        except ValueError:
            return None
    month_name = re.fullmatch(r"(\d{1,2})[- ]([A-Za-z]{3,9})[- ](\d{4})", raw)
    if month_name:
        from datetime import datetime

        for pattern in ("%d-%b-%Y", "%d-%B-%Y", "%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(raw.title(), pattern).date().isoformat()
            except ValueError:
                continue
    # Slash dates are locale-ambiguous. Preserve the visible value rather than
    # silently swapping its month and day.
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
        return raw
    return None


def _header_value(field: str, value: Any) -> str | None:
    raw = " ".join(str(value or "").split()).strip()
    if not raw or len(raw) > 80:
        return None
    if field == "date":
        return _date_value(raw)
    if field == "currency":
        return raw.upper() if re.fullmatch(r"[A-Za-z]{3}", raw) else None
    if field in {"number", "po"} and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_-]{0,79}", raw):
        return raw
    return None


def _table_header_values(tables: list[dict[str, Any]]) -> dict[str, str]:
    """Read repeated label/value header tables and reject conflicting values."""
    candidates: dict[str, set[str]] = {field: set() for field in {"number", "date", "po", "currency"}}
    for table in sorted(tables, key=lambda value: int(value.get("page", 1))):
        rows = _table_rows(table)
        for row in rows:
            if not any("document date" in _normalized(cell) for cell in row):
                continue
            visible_dates = {
                parsed
                for cell in row
                for raw_date in re.findall(
                    r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{4}|"
                    r"\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{4})\b",
                    str(cell or ""),
                )
                if (parsed := _date_value(raw_date)) is not None
            }
            if len(visible_dates) == 1:
                candidates["date"].update(visible_dates)
        for row_index, row in enumerate(rows[:-1]):
            mapped = {
                column: _HEADER_LABELS[_normalized(cell)]
                for column, cell in enumerate(row)
                if _normalized(cell) in _HEADER_LABELS
            }
            # A genuine invoice header table carries several independent
            # labels. Requiring two prevents captions or footer prose from
            # becoming header values.
            if len(set(mapped.values())) < 2:
                continue
            values = rows[row_index + 1]
            for column, field in mapped.items():
                if column >= len(values):
                    continue
                parsed = _header_value(field, values[column])
                if parsed is not None:
                    candidates[field].add(parsed)
    return {
        field: next(iter(values))
        for field, values in candidates.items()
        if len(values) == 1
    }


def _strict_text_headers(text: str) -> dict[str, str]:
    patterns = {
        "number": re.compile(
            r"(?im)^\s*(?:tax\s+)?invoice\s+(?:no\.?|number|#)\s*[:\-]\s*"
            r"([A-Za-z0-9][A-Za-z0-9./_-]{0,79})\s*$"
        ),
        "po": re.compile(
            r"(?im)^\s*(?:customer\s+)?(?:purchase\s+order|p\.?\s*o\.?)\s*"
            r"(?:(?:no\.?|number|#)\s*)?[:\-]\s*"
            r"([A-Za-z0-9][A-Za-z0-9./_-]{0,79})\s*$"
        ),
        "date": re.compile(
            r"(?im)^\s*(?:tax\s+)?invoice\s+date\s*[:\-]\s*"
            r"(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4})\s*$"
        ),
        "currency": re.compile(r"(?im)^\s*currency\s*[:\-]\s*([A-Za-z]{3})\s*$"),
    }
    result = {}
    for field, pattern in patterns.items():
        values = {
            parsed
            for match in pattern.finditer(text)
            if (parsed := _header_value(field, match.group(1))) is not None
        }
        if len(values) == 1:
            result[field] = next(iter(values))
    return result


def extract_invoice_from_tables(
    text: str,
    tables: list[dict[str, Any]] | None,
    boxes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Extract only values explicitly represented by table columns or labels."""
    tables = list(tables or [])
    has_native_tables = any(table.get("source") == "pdfplumber" for table in tables)
    if (
        boxes
        and not has_native_tables
        and not any(table.get("source") == "docling_word_cells" for table in tables)
    ):
        tables.extend(_tables_from_measured_words(boxes))
    if not tables:
        return None
    invoice: dict[str, Any] = {"lines": []}
    total_candidates: dict[str, list[tuple[int, str]]] = {"net": [], "tax": []}
    active_roles: dict[int, str] | None = None
    active_width: int | None = None
    line_candidates: list[tuple[str, dict[str, Any]]] = []
    for table in sorted(tables, key=lambda value: (
        int(value.get("page", 1)), value.get("source") != "docling_word_cells",
    )):
        table_source = str(table.get("source") or "unknown")
        rows = _table_rows(table)
        fixed_values = {
            key: str(value) for key, value in (table.get("fixed_values") or {}).items()
            if key in {"uom"} and str(value).strip()
        }
        printed_units = {
            match.group(1).upper()
            for row in rows
            for cell in row
            if (match := re.search(
                r"\bqty\s+in\s+([a-z0-9]{1,10})\b", _normalized(cell)
            )) is not None
        }
        if len(printed_units) == 1:
            fixed_values.setdefault("uom", next(iter(printed_units)))
        roles: dict[int, str] | None = None
        for row_index, row in enumerate(rows):
            row_roles = _semantic_row_roles(row)
            if _is_line_header(row_roles):
                roles = row_roles
                active_roles, active_width = roles, len(row)
                continue
            total = _totals_row(row)
            if total is not None:
                field, priority, value = total
                total_candidates[field].append((priority, value))
                continue
            current = roles or (active_roles if active_width == len(row) else None)
            if not current:
                continue
            if _is_line_header(row_roles):  # repeated header
                continue
            values: dict[str, Any] = {}
            for column, role in current.items():
                if column >= len(row):
                    continue
                if role not in {
                    "sku", "gtin", "description", "qty", "uom", "price",
                    "net_amount", "tax_amount",
                }:
                    continue
                raw = row[column]
                if role in {"qty", "price", "net_amount", "tax_amount"}:
                    parsed = _decimal(raw)
                    if parsed is not None:
                        values[role] = parsed
                elif role == "gtin":
                    parsed = _gtin(raw)
                    if parsed is not None:
                        values[role] = parsed
                elif str(raw or "").strip():
                    values[role] = str(raw).strip()
            identity = any(values.get(key) for key in ("sku", "gtin", "description"))
            priced_quantity = values.get("qty") is not None and values.get("price") is not None
            priced_amount = (
                values.get("price") is not None and values.get("net_amount") is not None
            )
            page = int(table.get("page", 1))
            if any(_normalized(cell) in {"total", "grand total"} for cell in row):
                if values.get("net_amount") is not None:
                    total_candidates["net"].append((70, values["net_amount"]))
                if values.get("tax_amount") is not None:
                    total_candidates["tax"].append((70, values["tax_amount"]))
                continue
            if identity and (priced_quantity or priced_amount):
                for key, value in fixed_values.items():
                    values.setdefault(key, value)
                values["page"] = page
                values["evidence"] = f"table page {values['page']} row {row_index + 1}"
                line_candidates.append((table_source, values))

    measured_total = sum(
        source == "docling_word_cells" for source, _values in line_candidates
    )
    printed_total = _printed_line_count(text)
    measured_matches_printed_total = (
        printed_total is not None and measured_total == printed_total
    )
    for page in sorted({values["page"] for _source, values in line_candidates}):
        measured = [
            values for source, values in line_candidates
            if values["page"] == page and source == "docling_word_cells"
        ]
        structural = [
            values for source, values in line_candidates
            if values["page"] == page and source != "docling_word_cells"
        ]
        measured_complete = bool(measured) and (
            measured_matches_printed_total
            or all(
                any(_lines_equivalent(structural_line, measured_line) for measured_line in measured)
                for structural_line in structural
            )
        )
        if measured_complete:
            # Use real measured words when they cover every structural row;
            # this removes duplicate rows caused by cell spans/split headers.
            invoice["lines"].extend(measured)
        else:
            # Preserve every TableFormer/native structural row. Measured rows
            # may add visible rows it missed, but never replace unmatched rows.
            invoice["lines"].extend(structural)
            invoice["lines"].extend(
                values for values in measured
                if not any(_lines_equivalent(values, existing) for existing in structural)
            )

    for field, candidates in total_candidates.items():
        if not candidates:
            continue
        best_priority = max(priority for priority, _value in candidates)
        values = {value for priority, value in candidates if priority == best_priority}
        if len(values) == 1:
            invoice[field] = next(iter(values))

    headers = _table_header_values(tables)
    text_headers = _strict_text_headers(text)
    for field in ("number", "date", "po", "currency"):
        if field in headers:
            invoice[field] = headers[field]
        elif field in text_headers:
            invoice[field] = text_headers[field]
    buyer_name = _table_buyer_name(text, tables, boxes)
    if buyer_name is not None:
        invoice["buyer_name"] = buyer_name
    return invoice if invoice["lines"] else None


def _lines_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _decimal(left.get("qty")) != _decimal(right.get("qty")):
        return False
    if _decimal(left.get("price")) != _decimal(right.get("price")):
        return False
    for field in ("sku", "gtin", "description"):
        left_value = re.sub(r"[^a-z0-9]+", "", str(left.get(field) or "").casefold())
        right_value = re.sub(r"[^a-z0-9]+", "", str(right.get(field) or "").casefold())
        if left_value and left_value == right_value:
            return True
    return False


def _printed_line_count(text: str) -> int | None:
    values = {
        int(match.group(1))
        for match in re.finditer(
            r"(?i)\btotal\s+lines\s*:?\s*(\d{1,4})\b",
            text,
        )
        if 0 < int(match.group(1)) <= 1000
    }
    return next(iter(values)) if len(values) == 1 else None


def _table_buyer_name(
    text: str,
    tables: list[dict[str, Any]],
    boxes: list[dict[str, Any]] | None,
) -> str | None:
    from app.layout_extract import (
        _clean_party_name,
        _extract_buyer_name,
        _group_all_rows,
        _normalise_words,
        _spatial_buyer_name,
    )

    spatial_candidate = _spatial_buyer_name(
        _group_all_rows(_normalise_words(boxes or []))
    )
    if spatial_candidate is not None:
        return spatial_candidate

    candidates: list[str] = []
    text_candidate = _extract_buyer_name(
        [line.strip() for line in str(text or "").splitlines() if line.strip()]
    )
    if text_candidate is not None:
        candidates.append(text_candidate)
    for table in tables:
        for row in _table_rows(table):
            for index, cell in enumerate(row):
                label = _normalized(cell)
                raw_candidate = None
                if label in {"bill to", "buyer"}:
                    raw_candidate = next(
                        (value for value in row[index + 1 :] if str(value or "").strip()),
                        None,
                    )
                elif label.startswith("bill to "):
                    raw_candidate = re.sub(r"(?i)^\s*bill\s+to\s*:?[ \t]*", "", str(cell))
                elif label.startswith("buyer "):
                    raw_candidate = re.sub(r"(?i)^\s*buyer\s*:?[ \t]*", "", str(cell))
                cleaned = _clean_party_name(raw_candidate)
                if cleaned is not None:
                    candidates.append(cleaned)
    normalized: dict[str, str] = {}
    for candidate in candidates:
        normalized.setdefault(_normalized(candidate), candidate)
    return next(iter(normalized.values())) if len(normalized) == 1 else None


def _totals_row(row: list[str | None]) -> tuple[str, int, str] | None:
    labels = {
        "vat amount": ("tax", 100),
        "tax amount": ("tax", 100),
        "total vat": ("tax", 90),
        "total tax": ("tax", 90),
        "vat amount in aed": ("tax", 10),
        "tax amount in aed": ("tax", 10),
        "net amount": ("net", 100),
        "taxable total": ("net", 90),
        "total excl": ("net", 90),
        "subtotal": ("net", 80),
    }
    for index, cell in enumerate(row):
        label = _normalized(cell)
        if label not in labels:
            continue
        value = next(
            (
                parsed
                for candidate in reversed(row[index + 1 :])
                if (parsed := _decimal(candidate)) is not None
            ),
            None,
        )
        if value is not None:
            field, priority = labels[label]
            return field, priority, value
    return None
