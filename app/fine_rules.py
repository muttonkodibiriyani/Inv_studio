"""ULTA fine-grained invoice-to-RMS matching rules.

Every decision cites the rule that made it (``R-0xx`` from 01_Rulebook and the
owner's POGRN rules, ``ALG-0xx`` from 01A_Algorithm_Rules). The engine reads
raw Item Master and PO/GRN rows through a source object and never writes
reference data. Values it cannot prove stay blank and go to the exception list;
nothing is inferred from a supplier's country or name suffix.
"""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


WAREHOUSE = "Warehouse (W)"
STORE = "Store (S)"
DERIVED_FROM_POGRN = "Derived from POGRN"
FROM_INVOICE = "Invoice"

# 03_Entity_Map: only the rows the business confirmed. RE2 is "To Validate" (V-001).
ENTITY_MAP = {
    "RA1": {"market": "Kuwait", "country": "KWT"},
    "RB2": {"market": "KSA", "country": "SAU"},
    "RA4": {"market": "UAE", "country": "ARE"},
}

# 05_Location_Master fallback rows (ALG-012/013, R-026).
LOCATION_PREFIXES = (("800", WAREHOUSE), ("380", STORE))

# Exception types whose presence still allows approval (warnings only).
NON_BLOCKING = {"Data Quality", "Entity Hint", "Description Check"}

TARGET_HEADER = ["Document", "Supplier Site", "Order No", "Location", "Location Type", "Document Date",
                 "Currency", "Gross Amount", "Tax Amount", "Net Amount", "Market", "Validation Status"]
TARGET_LINE = ["Transaction Number", "Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code", "VPN", "Brand",
               "Match Method", "Barcode Check", "VPN Check", "Description Check", "Source Row",
               "Validation Status", "POGRN RMS Order No", "POGRN Validation Status"]
MANDATORY_HEADER = ["Document", "Supplier Site", "Order No", "Location", "Location Type", "Document Date",
                    "Currency"]
MANDATORY_LINE = ["Item", "Unit Cost", "Quantity"]


# --------------------------------------------------------------------------- configuration


@dataclass
class RulesConfig:
    """Governed mapping tables. All of them are empty until the business supplies them."""

    # Exact POGRN location master: {location_id: {"type": ..., "market": ..., "name": ..., "country": ...}}
    location_master: dict = field(default_factory=dict)
    # Location to market, when supplied separately from the master.
    location_market: dict = field(default_factory=dict)
    # Approved supplier-site + market currency: {(supplier_site, market): currency}
    supplier_site_currency: dict = field(default_factory=dict)
    # Supplier sites with an approved USD exception (ALG-024).
    usd_exceptions: set = field(default_factory=set)
    entity_map: dict = field(default_factory=lambda: dict(ENTITY_MAP))
    qty_tolerance: Decimal = Decimal("0")
    value_tolerance: Decimal = Decimal("0")
    # Description acceptance threshold is open (V-009); description never auto-approves.
    description_threshold: float = 0.6
    version: str = "unconfigured"

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        master = {str(k).strip(): dict(v) for k, v in (data.get("location_master") or {}).items()}
        for location, row in master.items():
            if row.get("type") not in (None, "", WAREHOUSE, STORE):
                raise ValueError(f"Location {location}: type must be {STORE} or {WAREHOUSE}")
        currency = {}
        for row in data.get("supplier_site_currency") or []:
            site, market, code = (str(row.get(k) or "").strip() for k in ("supplier_site", "market", "currency"))
            if not (site and market and re.fullmatch(r"[A-Z]{3}", code)):
                raise ValueError("Supplier-site currency rows need supplier_site, market and a 3-letter currency")
            if currency.get((site, market), code) != code:
                raise ValueError(f"Supplier site {site} has two currencies for {market}")
            currency[(site, market)] = code
        tolerances = {}
        for key in ("qty_tolerance", "value_tolerance"):
            value = to_decimal(data.get(key, "0"))
            if value is None or value < 0:
                raise ValueError(f"{key} must be a non-negative number")
            tolerances[key] = value
        return cls(
            location_master=master,
            location_market={str(k).strip(): str(v).strip() for k, v in (data.get("location_market") or {}).items()},
            supplier_site_currency=currency,
            usd_exceptions={str(x).strip() for x in data.get("usd_exceptions") or []},
            entity_map={**ENTITY_MAP, **{str(k).upper(): dict(v) for k, v in (data.get("entity_map") or {}).items()}},
            version=str(data.get("version") or "unconfigured")[:80],
            **tolerances,
        )


# --------------------------------------------------------------------------- small helpers


def text(value):
    """Identifier as text, never a number (leading zeros survive)."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def to_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        result = Decimal(raw)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def fold(value):
    value = unicodedata.normalize("NFKD", text(value).casefold())
    return "".join(c for c in value if not unicodedata.combining(c))


def strip_ult(value):
    """R-004 / ALG-016: remove only the ``ULT_`` prefix; keep every other character."""
    value = text(value)
    return value[4:] if value[:4].upper() == "ULT_" else value


def normalize_barcode(value):
    """ALG-015: remove spaces, hyphens and formatting only; preserve leading zeros."""
    return re.sub(r"[\s\-./]", "", text(value))


# --------------------------------------------------------------------------- invoice scanning


def scan_pages(text_value="", boxes=(), page_count=None):
    """ALG-001: page-by-page text map of the whole document.

    Pages come from OCR/native boxes when they exist, else from form-feed
    separated text. ``page_count`` is the document's own page count when known.
    """
    pages = defaultdict(list)
    for box in boxes or ():
        value = text(box.get("text"))
        if not value:
            continue
        try:
            page = int(box.get("page", 1))
        except (TypeError, ValueError):
            continue
        top = 0.0
        position = box.get("box")
        if isinstance(position, (list, tuple)) and len(position) == 4:
            try:
                top = float(position[1])
            except (TypeError, ValueError):
                top = 0.0
        pages[page].append((top, value))
    page_text = {}
    if pages:
        for page, words in pages.items():
            rows, last = [], None
            for top, value in sorted(words, key=lambda x: x[0]):
                if last is not None and abs(top - last) <= 3 and rows:
                    rows[-1] += " " + value
                else:
                    rows.append(value)
                last = top
            page_text[page] = "\n".join(rows)
    elif text_value:
        for n, chunk in enumerate(str(text_value).split("\f"), 1):
            page_text[n] = chunk
    total = page_count or (max(page_text) if page_text else 0)
    readable = sorted(p for p, value in page_text.items() if value.strip())
    unreadable = [p for p in range(1, total + 1) if p not in readable]
    return {"page_count": total, "pages": {p: page_text.get(p, "") for p in range(1, total + 1)},
            "readable_pages": readable, "unreadable_pages": unreadable,
            "characters": {p: len(page_text.get(p, "")) for p in range(1, total + 1)}}


_LEGAL_SUFFIX = re.compile(
    r"(?i)\b(?:l\.?l\.?c|w\.?l\.?l|fz-?llc|fzco|fze|ltd|limited|inc|corp(?:oration)?|gmbh|s\.?a\.?r\.?l|"
    r"trading|est(?:ablishment)?|company|co\.)\b")
_SUPPLIER_LABEL = re.compile(
    r"(?i)^\s*(?:supplier|vendor|seller|sold\s+by|from|bill\s+from|beneficiary|company)(?:\s+name)?\s*[:\-]\s*(.{2,160})$")
_BUYER_LABEL = re.compile(r"(?i)\b(?:bill\s+to|ship\s+to|sold\s+to|buyer|customer|deliver\s+to|consignee)\b")


def supplier_candidates(scan, printed_name=None, buyer_name=None):
    """ALG-002: every supplier-name candidate across all pages, with page and region."""
    found = []

    def add(value, page, line_no, lines, how):
        value = " ".join(text(value).split()).strip(" :|-,")
        if len(value) < 2 or (buyer_name and fold(buyer_name) in fold(value)):
            return
        position = (line_no + 0.5) / max(1, len(lines))
        region = "top" if position < 1 / 3 else "bottom" if position > 2 / 3 else "body"
        key = fold(value)
        if not any(fold(c["name"]) == key for c in found):
            found.append({"name": value, "page": page, "line": line_no + 1, "region": region, "method": how})
        else:
            for c in found:
                if fold(c["name"]) == key:
                    c.setdefault("also_on", []).append({"page": page, "line": line_no + 1, "region": region})

    if printed_name:
        found.append({"name": text(printed_name), "page": None, "line": None, "region": "extracted",
                      "method": "extracted supplier field"})
    for page, page_value in scan["pages"].items():
        lines = [x for x in page_value.splitlines() if x.strip()]
        for n, line in enumerate(lines):
            label = _SUPPLIER_LABEL.match(line)
            if label:
                add(label.group(1), page, n, lines, "supplier label")
            elif _LEGAL_SUFFIX.search(line) and not _BUYER_LABEL.search(line) and len(line) <= 120:
                add(line, page, n, lines, "legal-entity name")
    return found


# --------------------------------------------------------------------------- identifiers


_LONG_NUMBER = re.compile(r"(?<![\w])(\d[\d\s\-]{6,18}\d)(?![\w])")


def barcode_candidates(line):
    """ALG-015: dedicated barcode/UPC column first, then long numeric strings in the description."""
    out = []
    if text(getattr(line, "gtin", None)):
        out.append({"value": normalize_barcode(strip_ult(line.gtin)), "origin": "barcode column"})
    for match in _LONG_NUMBER.finditer(text(getattr(line, "description", None))):
        value = normalize_barcode(match.group(1))
        if 8 <= len(value) <= 14 and value not in {c["value"] for c in out}:
            out.append({"value": value, "origin": "description"})
    return out


def vpn_candidates(line):
    """ALG-017 / R-002 / R-003: VPN column, else a leading six-digit number in the description.

    Other numbers in the description are kept as secondary candidates; they are
    tried only when they produce one unique master match (R-002).
    """
    out = []
    if text(getattr(line, "sku", None)):
        out.append({"value": text(line.sku), "origin": "VPN column", "primary": True})
        return out
    description = text(getattr(line, "description", None))
    leading = re.match(r"\s*(\d+)(?!\d)", description)
    if leading and len(leading.group(1)) == 6:
        out.append({"value": leading.group(1), "origin": "description start", "primary": True})
    for match in re.finditer(r"(?<!\d)(\d{6})(?!\d)", description):
        if match.group(1) not in {c["value"] for c in out}:
            out.append({"value": match.group(1), "origin": "description", "primary": False})
    return out


_UNITS = ((r"(\d)\s*(ml|millilit(?:er|re)s?)\b", r"\1ml"), (r"(\d)\s*(g|gm|gms|grams?)\b", r"\1g"),
          (r"(\d)\s*(kg|kilograms?)\b", r"\1kg"), (r"(\d)\s*(l|lit(?:er|re)s?)\b", r"\1l"),
          (r"(\d)\s*(oz|ounces?)\b", r"\1oz"), (r"(\d)\s*(pcs?|pieces?)\b", r"\1pc"))


def normalize_description(value, remove=()):
    """ALG-019: case-fold, unit and punctuation cleanup; drop extracted IDs; keep brand/size/shade tokens."""
    value = fold(value)
    for identifier in remove:
        if identifier:
            value = value.replace(fold(identifier), " ")
    for pattern, repl in _UNITS:
        value = re.sub(pattern, repl, value)
    value = re.sub(r"[^\w#]+", " ", value)
    return " ".join(value.split())


def description_score(invoice_description, row, remove=()):
    left = set(normalize_description(invoice_description, remove).split())
    best = 0.0
    for column in ("ITEM_DESC", "ITEM_DESC_SECONDARY", "SHORT_DESC"):
        right = set(normalize_description(row.get(column), remove).split())
        if left and right:
            best = max(best, len(left & right) / len(left | right))
    return round(best, 3)


def ebs_key(supplier_name):
    """ALG-006 (resolves R-019, see docs): first six characters of SUPPLIER_NAME, e.g. ABC001RA1KWD to ABC001."""
    name = re.sub(r"\s+", "", text(supplier_name)).upper()
    return name[:6] if len(name) >= 6 else None


def entity_hint(supplier_name, entity_map=ENTITY_MAP):
    """ALG-007 / R-008 / R-009: suffix after the six-character key as a hint only."""
    name = re.sub(r"\s+", "", text(supplier_name)).upper()
    suffix = name[6:]
    match = re.match(r"([A-Z]{2}\d)([A-Z]{3})?$", suffix)
    if not match:
        return {"suffix": suffix or None, "entity": None, "currency_hint": None, "mapped": None}
    entity, currency = match.groups()
    return {"suffix": suffix, "entity": entity, "currency_hint": currency, "mapped": entity_map.get(entity)}


def location_type(location, config=None):
    """ALG-012/013/014, R-026: exact master first; 800/380 prefix fallback kept as a cross-check."""
    location = text(location)
    prefix = next((kind for start, kind in LOCATION_PREFIXES if location.startswith(start)), None)
    master = (config.location_master.get(location) if config else None) or {}
    master_type = master.get("type") or None
    if master_type:
        conflict = prefix is not None and prefix != master_type
        return {"type": master_type, "source": "location master", "prefix_type": prefix, "conflict": conflict}
    return {"type": prefix, "source": "prefix fallback" if prefix else None, "prefix_type": prefix, "conflict": False}


def location_market(location, config):
    """R-027: market only from the accepted POGRN location through a governed mapping."""
    location = text(location)
    values = {v for v in ((config.location_master.get(location) or {}).get("market"),
                          config.location_market.get(location)) if v}
    count = len(values)
    return (values.pop() if count == 1 else None), count


def parse_printed_date(printed, parsed):
    """ALG-004: keep raw and parsed; n/n/yyyy with both parts <= 12 and different is ambiguous (V-008)."""
    raw = text(printed)
    match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", raw)
    ambiguous = bool(match and int(match.group(1)) <= 12 and int(match.group(2)) <= 12
                     and match.group(1).lstrip("0") != match.group(2).lstrip("0"))
    valid = None
    if parsed:
        try:
            valid = date.fromisoformat(parsed).isoformat()
        except ValueError:
            valid = None
    return {"raw": raw or None, "parsed": valid, "ambiguous": ambiguous}


# --------------------------------------------------------------------------- run state


class Run:
    def __init__(self, invoice, config, filename):
        self.invoice = invoice
        self.config = config
        self.filename = filename
        self.exceptions = []
        self.lineage = []

    def exception(self, kind, description, rule, line=None, evidence=None, proposed=None, owner="Accounts payable"):
        entry = {"Exception ID": f"EX-{len(self.exceptions) + 1:03d}", "Invoice Number": text(self.invoice.number),
                 "Line No.": line or "", "Exception Type": kind, "Description": description,
                 "Candidates / Evidence": evidence or "", "Proposed Resolution": proposed or "",
                 "Owner": owner, "Status": "Open", "Rule ID": rule, "blocking": kind not in NON_BLOCKING}
        self.exceptions.append(entry)
        return entry["Exception ID"]

    def trace(self, target, value, rule, source, original=None, reference=None, confidence="Exact", line=None):
        """R-018: original value, transformed value, rule ID, reference row and confidence."""
        if value in (None, ""):
            return
        self.lineage.append({"target": target, "line": line, "original": text(original if original is not None else value),
                             "value": text(value), "rule": rule, "source": source, "reference": reference or "",
                             "confidence": confidence})


# --------------------------------------------------------------------------- item matching


def _unique_parents(rows):
    return sorted({text(r.get("ITEM_PARENT")) for r in rows})


def _constrain(rows, sites):
    """ALG-018 / R-015: restrict to the matched supplier where possible."""
    if not sites:
        return rows, False
    narrowed = [r for r in rows if text(r.get("SUPPLIER")) in sites]
    return (narrowed, True) if narrowed else (rows, False)


def match_line(run, n, line, source, sites=None):
    """R-003/R-005/R-015, ALG-015..ALG-021, ALG-027, ALG-029: one invoice line to one ITEM_PARENT."""
    barcodes = barcode_candidates(line)
    vpns = vpn_candidates(line)
    result = {"line": n, "method": None, "rows": [], "barcode": None, "vpn": None, "parent": None,
              "checks": {"barcode": "Not available", "vpn": "Not available", "description": "Not available"},
              "status": "Exception", "description_score": None, "candidates": []}

    barcode_hits = {}
    for candidate in barcodes:
        rows = [r for r in source.items_by_barcode(candidate["value"]) if strip_ult(r.get("ITEM")) == candidate["value"]]
        if rows:
            barcode_hits[candidate["value"]] = (candidate, rows)
    vpn_hits = {}
    for candidate in vpns:
        rows = [r for r in source.items_by_vpn(candidate["value"]) if text(r.get("VPN")) == candidate["value"]]
        if rows:
            vpn_hits[candidate["value"]] = (candidate, rows)

    barcode_parents = {p for _, rows in barcode_hits.values() for p in _unique_parents(_constrain(rows, sites)[0])}
    primary_vpn = {v: hit for v, hit in vpn_hits.items() if hit[0]["primary"]}
    # R-002: a secondary number counts only when it alone yields one unique master match.
    secondary = {v: hit for v, hit in vpn_hits.items() if not hit[0]["primary"]}
    usable_vpn = primary_vpn or (secondary if len(secondary) == 1 else {})
    vpn_parents = {p for _, rows in usable_vpn.values() for p in _unique_parents(_constrain(rows, sites)[0])}
    if not primary_vpn and len(secondary) > 1:
        run.exception("Item Exception", "Several numbers in the description match different VPNs", "R-002", n,
                      evidence=", ".join(sorted(secondary)))

    parent = None
    if len(barcode_parents) == 1:
        parent, result["method"], rule = next(iter(barcode_parents)), "Barcode exact", "ALG-016"
    elif len(barcode_parents) > 1:
        run.exception("Item Exception", "Barcode matches more than one ITEM_PARENT", "ALG-016", n,
                      evidence=", ".join(sorted(barcode_parents)), owner="Item steward")
        result["candidates"] = sorted(barcode_parents)
        return result
    elif len(vpn_parents) == 1:
        parent, result["method"], rule = next(iter(vpn_parents)), "VPN exact", "ALG-018"
    elif len(vpn_parents) > 1:
        run.exception("Item Exception", "VPN matches more than one ITEM_PARENT", "ALG-018", n,
                      evidence=", ".join(sorted(vpn_parents)), owner="Item steward")
        result["candidates"] = sorted(vpn_parents)
        return result
    else:
        # R-005 third exact route: an internal item number on the invoice equals ITEM_PARENT.
        for value in [text(getattr(line, "item_id", None))] + [c["value"] for c in vpns]:
            if not value:
                continue
            rows = [r for r in source.items_by_parent(value) if text(r.get("ITEM_PARENT")) == value]
            rows = _constrain(rows, sites)[0]
            if rows:
                parent, result["method"], rule = value, "ITEM_PARENT exact", "R-005"
                break

    if parent is None:
        # ALG-019: description route produces review candidates only (R-005, V-009 open).
        removed = [c["value"] for c in barcodes + vpns]
        scored = []
        for row in source.items_by_description(text(line.description), sites):
            score = description_score(line.description, row, removed)
            if score > 0:
                scored.append((score, text(row.get("ITEM_PARENT"))))
        scored.sort(reverse=True)
        result["candidates"] = [f"{p} ({s:.2f})" for s, p in scored[:5]]
        result["method"] = "Description candidate" if scored else None
        reason = ("No exact barcode or VPN match; description candidates need manual approval"
                  if scored else "No barcode, VPN or description match in the Item Master")
        rules = "ALG-019" if scored else "R-003"
        result["exception"] = run.exception("Item Exception", reason, rules, n,
                                            evidence="; ".join(result["candidates"]), owner="Item steward")
        return result

    rows = _constrain(source.items_by_parent(parent), sites)[0]
    rows = [r for r in rows if text(r.get("ITEM_PARENT")) == parent] or \
        [r for _, hit in list(barcode_hits.values()) + list(usable_vpn.values()) for r in hit
         if text(r.get("ITEM_PARENT")) == parent]
    result.update(parent=parent, rows=rows)

    # ALG-020 triple assurance. Barcode and VPN are primary; description only supports.
    parent_barcodes = {strip_ult(r.get("ITEM")) for r in rows}
    parent_vpns = {text(r.get("VPN")) for r in rows}
    hit_barcode = next((c["value"] for c in barcodes if c["value"] in parent_barcodes), None)
    column_barcode = next((c["value"] for c in barcodes if c["origin"] == "barcode column"), None)
    if hit_barcode:
        result["checks"]["barcode"] = "Pass"
        result["barcode"] = hit_barcode
    elif column_barcode:
        result["checks"]["barcode"] = "Fail"
    hit_vpn = next((c["value"] for c in vpns if c["value"] in parent_vpns), None)
    column_vpn = next((c["value"] for c in vpns if c["origin"] == "VPN column"), None)
    if hit_vpn:
        result["checks"]["vpn"] = "Pass"
    elif column_vpn or (vpn_parents and parent not in vpn_parents):
        result["checks"]["vpn"] = "Fail"
    result["vpn"] = hit_vpn or (sorted(parent_vpns - {""})[0] if len(parent_vpns - {""}) == 1 else None)
    if text(line.description):
        score = max((description_score(line.description, r, [c["value"] for c in barcodes + vpns]) for r in rows),
                    default=0.0)
        result["description_score"] = score
        result["checks"]["description"] = "Consistent" if score >= run.config.description_threshold else "Weak"

    if "Fail" in (result["checks"]["barcode"], result["checks"]["vpn"]) or (
            barcode_parents and vpn_parents and not barcode_parents & vpn_parents):
        result["exception"] = run.exception(
            "Item Conflict", "Barcode and VPN identify different items, or an exact identifier disagrees",
            "ALG-020", n, evidence=f"barcode parents {sorted(barcode_parents)}; VPN parents {sorted(vpn_parents)}",
            owner="Item steward")
        return result
    if result["checks"]["description"] == "Weak":
        run.exception("Description Check", "Description is weakly consistent; exact identifier kept (ALG-020)",
                      "ALG-020", n, evidence=f"score {result['description_score']}")
    result["status"] = "Matched"
    run.trace("Item", parent, "ALG-021", "Item Master ITEM_PARENT", reference=_refs(rows), line=n,
              original=result["barcode"] or result["vpn"])
    result["rule"] = rule
    return result


def _refs(rows, limit=20):
    refs = [text(r.get("_ref")) for r in rows if r.get("_ref")]
    return ", ".join(refs[:limit]) + (f" (+{len(refs) - limit})" if len(refs) > limit else "")


# --------------------------------------------------------------------------- supplier


def resolve_supplier(run, source, matches, candidates):
    """R-006 / ALG-002 / ALG-005: supplier from the Item Master's valid item-supplier relationship."""
    by_name = []
    for candidate in candidates:
        by_name.extend(source.items_by_supplier_name(candidate["name"]))
    name_sites = {text(r.get("SUPPLIER")) for r in by_name if text(r.get("SUPPLIER"))}
    item_sites = None
    for m in matches:
        if m["status"] != "Matched":
            continue
        sites = {text(r.get("SUPPLIER")) for r in source.items_by_parent(m["parent"]) if text(r.get("SUPPLIER"))}
        item_sites = sites if item_sites is None else item_sites & sites
    if name_sites and item_sites is not None:
        sites = name_sites & item_sites
    else:
        sites = name_sites or item_sites or set()
    rows = [r for m in matches for r in source.items_by_parent(m["parent"] or "")] + by_name
    if len(sites) == 1:
        site = next(iter(sites))
        names = {text(r.get("SUPPLIER_NAME")) for r in rows if text(r.get("SUPPLIER")) == site and text(r.get("SUPPLIER_NAME"))}
        name = next(iter(names)) if len(names) == 1 else None
        run.trace("Supplier Site", site, "ALG-005", "Item Master SUPPLIER",
                  reference=_refs([r for r in rows if text(r.get("SUPPLIER")) == site]))
        if name is None:
            run.exception("Supplier Exception", "Supplier site has no single SUPPLIER_NAME in the Item Master",
                          "R-006", evidence=", ".join(sorted(names)), owner="Supplier operations")
        return site, name
    if not candidates and not sites:
        run.exception("Supplier Exception", "No supplier name found anywhere on the invoice", "ALG-002",
                      owner="Supplier operations")
    elif not sites:
        run.exception("Supplier Exception", "Supplier not found in the Item Master by name or matched items",
                      "R-006", evidence="; ".join(c["name"] for c in candidates)[:500], owner="Supplier operations")
    else:
        run.exception("Supplier Exception", "Several Item Master supplier sites remain; resolve with market/entity",
                      "R-006", evidence=", ".join(sorted(sites)), owner="Supplier operations")
    return None, None


# --------------------------------------------------------------------------- POGRN


def _group_rows(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(text(row.get("EBS_SUPPLIER_CODE")).upper(), text(row.get("RMS_ORDER_NO")), text(row.get("LOCATION")))].append(row)
    return groups


def evaluate_pogrn(run, rows, ebs, invoice_qty, invoice_value, invoice_currency, supplier_site, name_raw,
                   name_master, supplier_value):
    """R-020..R-030, ALG-008..ALG-014: candidate orders aggregated by (EBS, RMS order, location)."""
    config = run.config
    groups = _group_rows(rows)
    locations_per_order = defaultdict(set)
    for (_, order, location) in groups:
        locations_per_order[order].add(location)
    evaluated = []
    for (code, order, location), group in sorted(groups.items()):
        reasons = []
        counted = [r for r in group if to_decimal(r.get("QTY_RECEIVED")) is not None]
        qty = sum((to_decimal(r.get("QTY_RECEIVED")) for r in counted), Decimal(0)) if counted else None
        costs = [to_decimal(r.get("TOTAL COST")) for r in counted]
        cost = sum(costs, Decimal(0)) if counted and None not in costs else None
        currencies = {text(r.get("CURRENCY_CODE")).upper() for r in group if text(r.get("CURRENCY_CODE"))}
        supplier_ok = bool(ebs) and code == ebs.upper()
        order_ok = bool(order)
        if not supplier_ok:
            reasons.append("EBS supplier code differs")
        if not order_ok:
            reasons.append("RMS_ORDER_NO blank")
        # R-025: exact location of the order, never combined across locations.
        location_ok = bool(location) and len(locations_per_order[order]) == 1
        if not location:
            reasons.append("Location ID blank")
        elif len(locations_per_order[order]) > 1:
            reasons.append("Order has several locations; no approved multi-location rule")
        kind = location_type(location, config) if location else {"type": None, "conflict": False}
        type_ok = bool(kind["type"]) and not kind["conflict"]
        if location and not kind["type"]:
            reasons.append("Location type unresolved (no master row and no 800/380 prefix)")
        if kind.get("conflict"):
            reasons.append(f"Location master type {kind['type']} disagrees with prefix {kind['prefix_type']}")
        market, market_count = location_market(location, config) if location else (None, 0)
        market_ok = market is not None
        if not market_ok:
            reasons.append("Market unresolved: no approved location-market mapping" if market_count == 0
                           else "Location maps to several markets")
        # R-028: QTY_RECEIVED at the same aggregation level, tolerance default 0.
        qty_variance = invoice_qty - qty if invoice_qty is not None and qty is not None else None
        qty_ok = qty_variance is not None and abs(qty_variance) <= config.qty_tolerance
        if qty is None:
            reasons.append("No QTY_RECEIVED on this order/location")
        elif invoice_qty is None:
            reasons.append("Invoice quantity unavailable")
        elif not qty_ok:
            reasons.append(f"Quantity variance {qty_variance}")
        # R-029: currency must agree first, then pre-tax value over the same rows.
        currency_ok = bool(invoice_currency) and currencies == {invoice_currency.upper()}
        value_variance = invoice_value - cost if invoice_value is not None and cost is not None else None
        value_ok = currency_ok and value_variance is not None and abs(value_variance) <= config.value_tolerance
        if not currency_ok:
            reasons.append(f"Currency differs or unknown (invoice {invoice_currency or 'blank'}, POGRN "
                           f"{'/'.join(sorted(currencies)) or 'blank'})")
        elif cost is None:
            reasons.append("TOTAL COST missing on counted rows")
        elif invoice_value is None:
            reasons.append("Invoice value before tax unavailable")
        elif not value_ok:
            reasons.append(f"Value variance {value_variance}")
        passed = all((supplier_ok, order_ok, location_ok, type_ok, market_ok, qty_ok, value_ok))
        receipt_dates = sorted({text(r.get("RECEIPT_DATE"))[:10] for r in group if text(r.get("RECEIPT_DATE"))})
        evaluated.append({
            "Invoice Number": text(run.invoice.number), "Supplier Name Raw": name_raw or "",
            "Item Master Supplier Name": name_master or "", "Supplier Value": supplier_value or "",
            "EBS Supplier Code 6D": ebs or "", "Invoice Quantity": invoice_qty, "Invoice Value Before Tax": invoice_value,
            "POGRN Quantity": qty, "POGRN Value Before Tax": cost,
            "Qty Match": "Pass" if qty_ok else "Fail", "Pre-Tax Value Match": "Pass" if value_ok else "Fail",
            "POGRN RMS Order No": order, "Derived PO Number": "", "PO Source": "",
            "POGRN Row Reference": _refs(group), "Validation Status": "Pass" if passed else "Fail",
            "Exception Reason": "; ".join(reasons),
            "POGRN Location ID": location, "Location Type": kind["type"] or "", "Market": market or "",
            "Aggregated QTY_RECEIVED": qty, "Quantity Variance": qty_variance, "Quantity Result": "Pass" if qty_ok else "Fail",
            "Aggregated TOTAL COST": cost, "Value Variance": value_variance, "Value Result": "Pass" if value_ok else "Fail",
            "Receipt Dates": ", ".join(receipt_dates), "Supplier Check": "Pass" if supplier_ok else "Fail",
            "Location Check": "Pass" if location_ok and type_ok else "Fail", "Market Check": "Pass" if market_ok else "Fail",
            "Candidate Pass Count": None, "_passed": passed, "_location_type": kind, "_market": market,
            "_rows": group, "_core": supplier_ok and order_ok and qty_ok and value_ok,
        })
    passing = [g for g in evaluated if g["_passed"]]
    for g in evaluated:
        g["Candidate Pass Count"] = len({x["POGRN RMS Order No"] for x in passing})
    return evaluated, passing


def resolve_po(run, source, ebs, invoice_qty, invoice_value, supplier_site, name_raw, name_master):
    """ALG-008..ALG-011, R-020..R-030: validate the printed PO or derive one from POGRN."""
    invoice = run.invoice
    printed = text(invoice.po)
    currency = text(invoice.currency) or None
    outcome = {"order": None, "source": None, "group": None, "validation": [], "status": "Missing PO"}
    common = (ebs, invoice_qty, invoice_value, currency, supplier_site, name_raw, name_master, supplier_site)
    if printed:
        rows = [r for r in source.pogrn_by_order(printed) if text(r.get("RMS_ORDER_NO")) == printed]
        evaluated, passing = evaluate_pogrn(run, rows, *common)
        outcome["validation"].extend(evaluated)
        if len(passing) == 1:
            passing[0].update({"Derived PO Number": printed, "PO Source": FROM_INVOICE})
            run.trace("Order No", printed, "ALG-008", "Invoice PO validated against POGRN",
                      reference=passing[0]["POGRN Row Reference"])
            outcome.update(order=printed, source=FROM_INVOICE, group=passing[0], status="Approved")
            return outcome
    if not ebs:
        run.exception("Missing PO", "No EBS supplier code, so POGRN cannot be searched", "R-019",
                      owner="Supplier operations")
        if printed:
            outcome.update(order=printed, source=FROM_INVOICE, status="Printed PO not validated")
        return outcome
    rows = [r for r in source.pogrn_by_ebs(ebs) if text(r.get("EBS_SUPPLIER_CODE")).upper() == ebs.upper()]
    if not rows:
        run.exception("Missing PO", f"No POGRN rows for EBS supplier code {ebs}", "R-020", owner="Buyer")
        if printed:
            outcome.update(order=printed, source=FROM_INVOICE, status="Printed PO not validated")
        return outcome
    evaluated, passing = evaluate_pogrn(run, rows, *common)
    seen = {(g["POGRN RMS Order No"], g["POGRN Location ID"]) for g in outcome["validation"]}
    outcome["validation"].extend(g for g in evaluated if (g["POGRN RMS Order No"], g["POGRN Location ID"]) not in seen)
    orders = {g["POGRN RMS Order No"] for g in passing}
    if len(orders) == 1 and len(passing) == 1:
        group = passing[0]
        derived = group["POGRN RMS Order No"]
        group.update({"Derived PO Number": derived, "PO Source": DERIVED_FROM_POGRN})
        if printed and printed != derived:
            # R-024: never overwrite an invoice-supplied PO without exception approval.
            run.exception("PO Conflict", f"Invoice PO {printed} differs from the POGRN-derived order {derived}",
                          "R-024", evidence=group["POGRN Row Reference"], owner="Buyer")
            outcome.update(order=printed, source=FROM_INVOICE, group=group, status="PO Conflict")
            return outcome
        run.trace("Order No", derived, "ALG-010", "POGRN RMS_ORDER_NO", reference=group["POGRN Row Reference"],
                  confidence=DERIVED_FROM_POGRN)
        outcome.update(order=derived, source=DERIVED_FROM_POGRN, group=group, status="Approved")
        return outcome
    if len(orders) > 1:
        run.exception("Ambiguous PO", "More than one RMS order passes every check", "R-030",
                      evidence=", ".join(sorted(orders)), owner="Buyer")
        outcome["status"] = "Ambiguous PO"
    else:
        core = [g for g in evaluated if g["_core"]]
        detail = "; ".join(f"{g['POGRN RMS Order No']}@{g['POGRN Location ID']}: {g['Exception Reason']}" for g in core[:5])
        run.exception("Missing PO", "No POGRN order passes every check (R-030)" +
                      (" - supplier, quantity and value agree but mapping checks fail" if core else ""),
                      "R-030", evidence=detail, owner="Buyer",
                      proposed="Supply the POGRN location-market mapping (V-007)" if core else None)
        outcome["status"] = "Missing PO"
        outcome["candidates"] = core
    if printed:
        outcome.update(order=printed, source=FROM_INVOICE)
    return outcome


# --------------------------------------------------------------------------- currency


def resolve_currency(run, supplier_site, market, hint):
    """R-012/R-013/R-014, ALG-023/024/025: configured supplier-site + market currency only."""
    config = run.config
    printed = text(run.invoice.currency).upper() or None
    if not (supplier_site and market):
        run.exception("Currency Mapping", "Currency waits for a resolved supplier site and receiving market",
                      "ALG-023", proposed="Supply the supplier-site-market-currency list (V-010)", owner="Finance")
        return None
    configured = config.supplier_site_currency.get((supplier_site, market))
    if configured is None:
        # R-013 / ALG-025: a cross-border pair is accepted only when configured.
        kind = "Cross-border" if hint and hint.get("mapped") and hint["mapped"].get("market") != market else "Currency Mapping"
        run.exception(kind, f"No approved currency for supplier site {supplier_site} in {market}",
                      "ALG-025" if kind == "Cross-border" else "R-012", owner="Finance",
                      proposed="Supply the supplier-site-market-currency list (V-010)")
        return None
    if configured == "USD" and supplier_site not in config.usd_exceptions:
        run.exception("USD Review", "USD is configured without an approved supplier-site USD exception",
                      "ALG-024", owner="Finance")
        return None
    if printed == "USD" and configured != "USD":
        run.exception("USD Review", "Invoice is in USD; the MVP routes USD to manual review", "R-014", owner="Finance")
        return None
    if printed and printed != configured:
        run.exception("Currency Mapping", f"Invoice currency {printed} differs from configured {configured}",
                      "R-012", owner="Finance")
        return None
    run.trace("Currency", configured, "ALG-023", "Supplier-site-market currency map",
              reference=f"{supplier_site}|{market}|{config.version}")
    return configured


# --------------------------------------------------------------------------- invoice run


def _line_value(invoice):
    if invoice.net is not None:
        return invoice.net, "printed net"
    nets = [l.net_amount for l in invoice.lines]
    if nets and None not in nets:
        return sum(nets, Decimal(0)), "sum of printed line amounts"
    return None, None


def run_invoice(invoice, source, config=None, filename="", text_value="", boxes=(), page_count=None,
                transaction=1, seen_documents=None):
    """Run the whole rule chain for one invoice (ALG-028: every invoice, every rule)."""
    config = config or RulesConfig()
    run = Run(invoice, config, filename)
    scan = scan_pages(text_value, boxes, page_count)
    if scan["unreadable_pages"]:
        run.exception("OCR Review", f"Pages without readable text: {scan['unreadable_pages']}", "ALG-001",
                      owner="Accounts payable")
    if not invoice.lines:
        run.exception("Unreadable Invoice", "No invoice lines were read; route to OCR/manual review", "R-001")

    # Header facts straight from the invoice (ALG-003 / ALG-004).
    document = text(invoice.number)
    if not document:
        run.exception("Header Exception", "Document (invoice number) is blank", "ALG-003")
    elif seen_documents is not None and document in seen_documents:
        run.exception("Header Exception", "Document number repeats in this batch", "ALG-003")
    else:
        run.trace("Document", document, "ALG-003", "Invoice Document", original=invoice.number)
    dates = parse_printed_date(invoice.date_printed, invoice.date)
    document_date = None
    if not dates["parsed"]:
        run.exception("Date Review", "Document Date is missing or invalid", "ALG-004", evidence=dates["raw"] or "")
    elif dates["ambiguous"]:
        run.exception("Date Review", f"Printed date {dates['raw']} is ambiguous day/month (V-008)", "ALG-004",
                      evidence=f"parsed {dates['parsed']}")
    else:
        document_date = dates["parsed"]
        run.trace("Document Date", document_date, "ALG-004", "Invoice Document Date", original=dates["raw"])

    # Supplier discovery over the whole document, then items, then supplier linkage.
    candidates = supplier_candidates(scan, invoice.supplier_name, invoice.buyer_name)
    first_names = {r_site for c in candidates for r_site in
                   (text(r.get("SUPPLIER")) for r in source.items_by_supplier_name(c["name"])) if r_site}
    # A probe pass finds the supplier from unambiguous lines; the recorded pass then
    # constrains every line by that supplier (ALG-018 / R-015).
    probe = Run(invoice, config, filename)
    probe_matches = [match_line(probe, n, line, source, first_names or None) for n, line in enumerate(invoice.lines, 1)]
    probe_site, _ = resolve_supplier(probe, source, probe_matches, candidates)
    sites = {probe_site} if probe_site else (first_names or None)
    matches = [match_line(run, n, line, source, sites) for n, line in enumerate(invoice.lines, 1)]
    supplier_site, master_name = resolve_supplier(run, source, matches, candidates)
    if supplier_site:
        # Re-check items under the resolved supplier where the first pass was unconstrained.
        for m in matches:
            if m["status"] == "Matched" and supplier_site not in {text(r.get("SUPPLIER")) for r in m["rows"]}:
                run.exception("Item Exception", "Matched item has no relationship with the resolved supplier site",
                              "R-015", m["line"], evidence=m["parent"], owner="Item steward")
                m["status"] = "Exception"
            elif m["status"] == "Matched":
                m["rows"] = [r for r in m["rows"] if text(r.get("SUPPLIER")) == supplier_site]
    key = ebs_key(master_name)
    if master_name and not key:
        run.exception("Supplier Exception", "SUPPLIER_NAME is shorter than six characters", "R-019",
                      evidence=master_name, owner="Supplier operations")
    if key:
        run.trace("EBS Supplier Code 6D", key, "ALG-006", "Item Master SUPPLIER_NAME", original=master_name)
    hint = entity_hint(master_name or "", config.entity_map) if master_name else None
    if hint and hint["suffix"] and not hint["mapped"]:
        run.exception("Entity Hint", f"Supplier suffix {hint['suffix']} has no confirmed entity mapping (V-001)",
                      "R-009", owner="Business")

    # PO and receiving location.
    invoice_qty = sum((l.qty for l in invoice.lines if l.qty is not None), Decimal(0)) \
        if invoice.lines and all(l.qty is not None for l in invoice.lines) else None
    invoice_value, value_source = _line_value(invoice)
    raw_name = candidates[0]["name"] if candidates else None
    po = resolve_po(run, source, key, invoice_qty, invoice_value, supplier_site, raw_name, master_name)
    group = po["group"] if po["status"] in ("Approved", "PO Conflict") else None
    location = loc_type = market = None
    if group:
        location = group["POGRN Location ID"]
        loc_type = group["_location_type"]["type"]
        market = group["_market"]
        run.trace("Location", location, "ALG-011", "Accepted POGRN LOCATION", reference=group["POGRN Row Reference"])
        run.trace("Location Type", loc_type, "ALG-014" if group["_location_type"]["source"] == "location master"
                  else ("ALG-012" if loc_type == WAREHOUSE else "ALG-013"), group["_location_type"]["source"],
                  original=location)
        run.trace("Market", market, "R-027", "Location-market mapping", original=location)
    else:
        run.exception("Location ID", "Location waits for an accepted POGRN order (never from Item Master)",
                      "ALG-011", owner="Buyer")
    if hint and hint["mapped"] and market and hint["mapped"]["market"] != market:
        run.exception("Market Mapping", f"Supplier suffix suggests {hint['mapped']['market']} but the receiving "
                      f"location is {market}", "R-027", owner="Business")
    currency = resolve_currency(run, supplier_site, market, hint) if group else None
    if not group:
        run.exception("Currency Mapping", "Currency waits for the receiving market of an accepted order",
                      "ALG-023", owner="Finance")
    if po["order"] and po["source"] == FROM_INVOICE and po["status"] not in ("Approved",):
        run.trace("Order No", po["order"], "R-024", "Invoice PO kept (not validated)", confidence="Unvalidated")

    # Line outputs (ALG-021/022/026/027).
    tax_code = text(invoice.taxCode)
    lines_out = []
    for line, m in zip(invoice.lines, matches):
        n = m["line"]
        brand = ""
        if m["status"] == "Matched":
            brands = {text(r.get("UDA_LV_1_VALUE")) for r in m["rows"]} - {""}
            if len(brands) == 1:
                brand = next(iter(brands))
                run.trace("Brand", brand, "ALG-022", "Item Master UDA_LV_1_VALUE", reference=_refs(m["rows"]), line=n)
            else:
                run.exception("Data Quality", "Brand (UDA_LV_1_VALUE) missing or not unique for the matched item",
                              "ALG-022", n, evidence=", ".join(sorted(brands)))
        upc = m["barcode"] or next((c["value"] for c in barcode_candidates(line) if c["origin"] == "barcode column"), "")
        if upc:
            run.trace("UPC", upc, "ALG-027", "Invoice barcode", original=line.gtin or line.description, line=n)
        line_ok = m["status"] == "Matched" and line.qty is not None and line.price is not None
        if line.qty is None or line.price is None:
            run.exception("Line Exception", "Quantity or unit cost missing on the invoice line", "ALG-026", n)
        elif line.net_amount is not None:
            # 02A Unit Cost: line/value reconciliation at the printed line amount's precision.
            computed = (line.qty * line.price).quantize(line.net_amount, rounding=ROUND_HALF_UP)
            if computed != line.net_amount:
                line_ok = False
                run.exception("Line Exception", f"Quantity x unit cost {computed} differs from line amount "
                              f"{line.net_amount}", "02A-Unit Cost", n)
        for f, v in (("Unit Cost", line.price), ("Quantity", line.qty)):
            run.trace(f, v, "ALG-026", "Invoice line", line=n)
        if tax_code:
            run.trace("Unit Tax Code", tax_code, "R-016", "Reviewed invoice tax code", line=n)
        lines_out.append({
            "Transaction Number": transaction, "Item": m["parent"] if m["status"] == "Matched" else "",
            "UPC": upc if m["status"] == "Matched" or not m["parent"] else upc, "Unit Cost": line.price,
            "Quantity": line.qty, "Unit Tax Code": tax_code, "VPN": m["vpn"] or "", "Brand": brand,
            "Match Method": m["method"] or "Unmatched", "Barcode Check": m["checks"]["barcode"],
            "VPN Check": m["checks"]["vpn"], "Description Check": m["checks"]["description"],
            "Source Row": f"page {line.page}" if line.page else f"line {n}",
            "Validation Status": "Matched" if line_ok else "Exception",
            "POGRN RMS Order No": po["order"] or "", "POGRN Validation Status": po["status"],
            "_match": m, "_line": line,
        })
    if not tax_code and invoice.lines:
        run.exception("Tax Code", "Unit Tax Code is blank; no approved tax mapping in the ULTA sources", "R-016",
                      owner="Tax reviewer")

    header = {
        "Document": document, "Supplier Site": supplier_site or "", "Order No": po["order"] or "",
        "Location": location or "", "Location Type": loc_type or "", "Document Date": document_date or "",
        "Currency": currency or "", "Gross Amount": (invoice.net + invoice.tax) if invoice.net is not None and invoice.tax is not None else None,
        "Tax Amount": invoice.tax, "Net Amount": invoice.net, "Market": market or "",
    }
    # R-016 / R-018 / ALG-030: approval needs every mandatory field, a lineage record for it and no blocker.
    traced = {(x["target"], x["line"]) for x in run.lineage}
    for name in MANDATORY_HEADER:
        if header[name] and (name, None) not in traced:
            run.exception("Lineage", f"{name} has no lineage record", "R-018")
    for row in lines_out:
        n = row["_match"]["line"]
        if row["Item"] and ("Item", n) not in traced:
            run.exception("Lineage", "Item has no lineage record", "R-018", n)
    missing = [name for name in MANDATORY_HEADER if not header[name]]
    blocking = [e for e in run.exceptions if e["blocking"]]
    line_missing = any(not row[c] and row[c] != 0 for row in lines_out for c in MANDATORY_LINE)
    if not missing and not blocking and not line_missing and lines_out and tax_code:
        status = "Approved"
    elif any(e["Exception Type"] in ("Unreadable Invoice",) for e in blocking):
        status = "Blocked"
    else:
        status = "Review"
    header["Validation Status"] = status
    workbench = [_workbench_row(run, header, row, key, hint, po, invoice_value) for row in lines_out]
    return {
        "status": status, "filename": filename, "transaction": transaction, "header": header,
        "lines": [{k: v for k, v in row.items() if not k.startswith("_")} for row in lines_out],
        "workbench": workbench, "exceptions": run.exceptions, "lineage": run.lineage,
        "pogrn_validation": [{k: v for k, v in g.items() if not k.startswith("_")} for g in po["validation"]],
        "raw_invoice": raw_invoice_rows(invoice, filename, barcode_candidates, vpn_candidates),
        "tax": tax_rows(invoice, tax_code), "scan": {k: v for k, v in scan.items() if k != "pages"},
        "supplier_candidates": candidates, "missing_mandatory": missing,
        "po": {"order": po["order"], "source": po["source"], "status": po["status"], "value_source": value_source},
        "ebs_supplier_code": key, "entity_hint": hint, "config_version": config.version,
    }


def _workbench_row(run, header, row, key, hint, po, invoice_value):
    m = row["_match"]
    exception = next((e["Exception ID"] for e in run.exceptions if e["Line No."] == m["line"] and e["blocking"]), "")
    group = po.get("group") or {}
    return {
        "Invoice Number": header["Document"], "Line No.": m["line"], "Supplier Code": header["Supplier Site"],
        "Entity Suffix": (hint or {}).get("entity") or "", "Market": header["Market"],
        "Location Code": header["Location"], "Location Type": header["Location Type"], "VPN": row["VPN"],
        "Barcode": row["UPC"], "ITEM_PARENT": m["parent"] or "",
        "ITEM": ", ".join(sorted({text(r.get("ITEM")) for r in m["rows"]})) if m["rows"] else "",
        "Brand": row["Brand"], "Currency": header["Currency"], "Match Method": row["Match Method"],
        "Rule ID": m.get("rule") or "", "Confidence": "Exact" if m["status"] == "Matched" else "Candidate",
        "Validation Status": row["Validation Status"], "Exception ID": exception,
        "Invoice Value Before Tax": invoice_value, "POGRN Quantity": group.get("POGRN Quantity"),
        "POGRN Value Before Tax": group.get("POGRN Value Before Tax"), "PO Source": po["source"] or "",
        "POGRN RMS Order No": po["order"] or "",
    }


def raw_invoice_rows(invoice, filename, barcodes=barcode_candidates, vpns=vpn_candidates):
    """R-001 / 06_Raw_Invoice: raw values, unchanged."""
    rows = []
    for n, line in enumerate(invoice.lines, 1):
        rows.append({"Invoice File": filename, "Invoice Number": invoice.number or "", "Line No.": n,
                     "Supplier Name": invoice.supplier_name or "", "Description Raw": line.description or "",
                     "Candidate VPN": ", ".join(c["value"] for c in vpns(line)),
                     "Candidate Barcode": ", ".join(c["value"] for c in barcodes(line)),
                     "Quantity": line.qty, "Unit Cost": line.price, "Line Amount": line.net_amount,
                     "Currency": invoice.currency or "", "Source Row": f"page {line.page}" if line.page else ""})
    return rows


def tax_rows(invoice, tax_code):
    """11_Output_Tax: printed tax facts per line; tax code only when reviewed on the invoice."""
    rows = []
    for n, line in enumerate(invoice.lines, 1):
        taxable = line.net_amount if line.net_amount is not None else (
            line.qty * line.price if line.qty is not None and line.price is not None else None)
        rate = (line.tax_amount / taxable).quantize(Decimal("0.0001")) if line.tax_amount is not None and taxable else None
        rows.append({"Invoice Number": invoice.number or "", "Line No.": n, "Tax Code": tax_code, "Tax Rate": rate,
                     "Taxable Amount": taxable, "Tax Amount": line.tax_amount, "Country Code": "",
                     "Source Row": f"page {line.page}" if line.page else f"line {n}"})
    return rows


def run_batch(entries, source, config=None):
    """ALG-028: every invoice runs the full chain; one failure never stops the batch."""
    results, seen = [], set()
    for transaction, entry in enumerate(entries, 1):
        try:
            result = run_invoice(entry["invoice"], source, config, entry.get("filename", ""), entry.get("text", ""),
                                 entry.get("boxes", ()), entry.get("page_count"), transaction, seen)
        except Exception as error:  # isolated per invoice; the error is the evidence
            result = {"status": "Blocked", "filename": entry.get("filename", ""), "transaction": transaction,
                      "header": {"Document": text(entry["invoice"].number), "Validation Status": "Blocked"},
                      "lines": [], "workbench": [], "lineage": [], "pogrn_validation": [], "raw_invoice": [],
                      "tax": [], "exceptions": [{"Exception ID": "EX-001", "Invoice Number": text(entry["invoice"].number),
                                                 "Line No.": "", "Exception Type": "Processing", "Description": str(error)[:300],
                                                 "Candidates / Evidence": "", "Proposed Resolution": "", "Owner": "Support",
                                                 "Status": "Open", "Rule ID": "ALG-028", "blocking": True}]}
        if text(entry["invoice"].number):
            seen.add(text(entry["invoice"].number))
        results.append(result)
    return results


# --------------------------------------------------------------------------- feedback (R-017)


FEEDBACK_COLUMNS = ["Feedback ID", "Date", "Invoice / Line", "Original Suggestion", "User Correction", "Reason",
                    "Evidence", "Rule Candidate", "Approver", "Decision", "Implemented Version"]


def feedback_entry(log, invoice_line, original, correction, reason, evidence, rule_candidate="", today=None):
    """R-017: a correction is logged as Proposed. It never changes a rule until it is reviewed."""
    if not text(correction) or not text(evidence):
        raise ValueError("A correction needs the corrected value and its evidence")
    entry = {"Feedback ID": f"FB-{len(log) + 1:04d}", "Date": (today or date.today()).isoformat(),
             "Invoice / Line": text(invoice_line), "Original Suggestion": text(original),
             "User Correction": text(correction), "Reason": text(reason), "Evidence": text(evidence),
             "Rule Candidate": text(rule_candidate), "Approver": "", "Decision": "Proposed", "Implemented Version": ""}
    return [*log, entry]


def decide_feedback(log, feedback_id, approver, decision, version=""):
    """R-017: only a named approver can accept or reject; acceptance records the rule version."""
    if decision not in ("Approved", "Rejected") or not text(approver):
        raise ValueError("Decision must be Approved or Rejected by a named approver")
    out, found = [], False
    for entry in log:
        if entry["Feedback ID"] == feedback_id:
            if entry["Decision"] != "Proposed":
                raise ValueError("Feedback has already been decided")
            entry = {**entry, "Approver": text(approver), "Decision": decision,
                     "Implemented Version": text(version) if decision == "Approved" else ""}
            found = True
        out.append(entry)
    if not found:
        raise ValueError("Unknown feedback ID")
    return out


class RowsSource:
    """In-memory Item Master / POGRN rows (tests and small uploads)."""

    def __init__(self, items=(), pogrn=()):
        self.items = [dict(r) for r in items]
        self.pogrn = [dict(r) for r in pogrn]

    def items_by_barcode(self, value):
        return [r for r in self.items if strip_ult(r.get("ITEM")) == value]

    def items_by_vpn(self, value):
        return [r for r in self.items if text(r.get("VPN")) == value]

    def items_by_parent(self, value):
        return [r for r in self.items if text(r.get("ITEM_PARENT")) == value]

    def items_by_supplier_name(self, value):
        key = re.sub(r"[^\w]", "", fold(value))
        return [r for r in self.items if key and re.sub(r"[^\w]", "", fold(r.get("SUPPLIER_NAME"))) == key]

    def items_by_description(self, value, sites=None):
        tokens = set(normalize_description(value).split())
        return [r for r in self.items if (not sites or text(r.get("SUPPLIER")) in sites) and tokens &
                set(normalize_description(" ".join(text(r.get(c)) for c in ("ITEM_DESC", "ITEM_DESC_SECONDARY", "SHORT_DESC"))).split())]

    def pogrn_by_ebs(self, value):
        return [r for r in self.pogrn if text(r.get("EBS_SUPPLIER_CODE")).upper() == value.upper()]

    def pogrn_by_order(self, value):
        return [r for r in self.pogrn if text(r.get("RMS_ORDER_NO")) == value]
