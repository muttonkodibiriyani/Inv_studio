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
        ("description", ("productdescription", "itemdescription", "description", "productname")),
        ("sku", ("productreference", "itemcode", "itemnumber", "sku", "article")),
        ("qty", ("quantity", "qtyinpce", "qtypce", "qty")),
        ("uom", ("unitofmeasure", "uom", "measureunit")),
        ("tax_amount", ("taxamount", "vatamount")),
        ("tax_rate", ("taxrate", "vatrate", "taxpercent")),
        ("net_amount", ("taxablevalue", "netamount", "lineamount", "totalprice")),
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
        "description": {"description", "productdescription", "itemdescription", "productname"},
        "sku": {"productreference", "itemcode", "itemnumber", "sku", "article"},
        "qty": {"qty", "quantity", "qtyinpce", "qtypce"},
        "uom": {"uom", "unitofmeasure", "measureunit"},
        "price": {"price", "sellingprice", "unitprice", "priceperunit"},
        "net_amount": {"taxablevalue", "netamount", "lineamount", "totalprice"},
        "tax_rate": {"taxrate", "vatrate", "taxpercent"},
        "tax_amount": {"taxamount", "vatamount"},
        "gross_amount": {"grossamount", "amountincludingtax"},
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
            required = {"sku", "description", "qty", "price"}
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
                if (
                    any(mapped.get(role) for role in ("sku", "gtin", "description"))
                    and _decimal(mapped.get("qty")) is not None
                    and _decimal(mapped.get("price")) is not None
                ):
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


def _labelled_value(text: str, labels: str) -> str | None:
    match = re.search(rf"(?im)\b(?:{labels})\b\s*(?:no\.?|number|#)?\s*[:\-]?\s*([^\n|]{{1,60}})", text)
    return match.group(1).strip() if match else None


def extract_invoice_from_tables(
    text: str,
    tables: list[dict[str, Any]] | None,
    boxes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Extract only values explicitly represented by table columns or labels."""
    tables = list(tables or [])
    if boxes and not any(table.get("source") == "docling_word_cells" for table in tables):
        tables.extend(_tables_from_measured_words(boxes))
    if not tables:
        return None
    invoice: dict[str, Any] = {"lines": []}
    active_roles: dict[int, str] | None = None
    active_width: int | None = None
    seen_line_sources: dict[tuple[Any, ...], set[str]] = {}
    for table in sorted(tables, key=lambda value: (
        int(value.get("page", 1)), value.get("source") != "docling_word_cells",
    )):
        table_source = str(table.get("source") or "unknown")
        rows = _table_rows(table)
        fixed_values = {
            key: str(value) for key, value in (table.get("fixed_values") or {}).items()
            if key in {"uom"} and str(value).strip()
        }
        roles: dict[int, str] | None = None
        for row_index, row in enumerate(rows):
            row_roles = {index: role for index, cell in enumerate(row) if (role := _role(cell))}
            if len(set(row_roles.values())) >= 2:
                roles = row_roles
                active_roles, active_width = roles, len(row)
                continue
            if _read_totals_row(invoice, row):
                continue
            current = roles or (active_roles if active_width == len(row) else None)
            if not current:
                continue
            if len(set(row_roles.values())) >= 2:  # repeated header
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
            measure = any(values.get(key) is not None for key in ("qty", "price", "net_amount"))
            if identity and measure:
                for key, value in fixed_values.items():
                    values.setdefault(key, value)
                values["page"] = int(table.get("page", 1))
                values["evidence"] = f"table page {values['page']} row {row_index + 1}"
                fingerprint = tuple(values.get(key) for key in (
                    "page", "sku", "qty", "price", "net_amount", "tax_amount",
                ))
                sources = seen_line_sources.setdefault(fingerprint, set())
                if sources and table_source not in sources:
                    continue
                sources.add(table_source)
                invoice["lines"].append(values)

    number = _labelled_value(text, r"invoice")
    po = _labelled_value(text, r"purchase\s+order|p\.?\s*o\.?")
    date = _labelled_value(text, r"invoice\s+date|date")
    currency = _labelled_value(text, r"currency")
    if number:
        invoice["number"] = number
    if po:
        invoice["po"] = po
    if date:
        date_match = re.search(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", date)
        if date_match:
            invoice["date"] = date_match.group(0)
    if currency:
        code = re.search(r"\b[A-Z]{3}\b", currency.upper())
        if code:
            invoice["currency"] = code.group(0)
    return invoice if invoice["lines"] else None


def _read_totals_row(invoice: dict[str, Any], row: list[str | None]) -> bool:
    label = _normalized(" ".join(str(cell or "") for cell in row))
    values = [_decimal(cell) for cell in reversed(row)]
    value = next((item for item in values if item is not None), None)
    if value is None:
        return False
    if any(term in label for term in ("vat amount", "tax amount", "total vat", "total tax")):
        invoice["tax"] = value
        return True
    elif any(term in label for term in ("net amount", "taxable total", "total excl", "subtotal")):
        invoice["net"] = value
        return True
    return False
