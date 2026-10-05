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
from datetime import date, datetime
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

# 03_Mandatory_Checklist: engine exception kinds to the owner's Failure Status and check id.
# The engine's own rule id stays in "Rule ID".
FAILURE_STATUS = {
    "OCR Review": ("OCR Exception", "C-01"), "Unreadable Invoice": ("OCR Exception", "C-01"),
    "Header Exception": ("Header Exception", "C-02"), "Date Review": ("Header Exception", "C-02"),
    "Supplier Exception": ("Supplier Exception", "C-03"),
    "POGRN Supplier Exception": ("POGRN Supplier Exception", "C-04"),
    "Supplier Site Exception": ("Supplier Site Exception", "C-05"),
    "Item Exception": ("Item Exception", "C-08"), "Item Conflict": ("Item Conflict", "C-09"),
    "Line Exception": ("Line Parsing Exception", "C-10"),
    "Item Quantity Mismatch": ("Quantity Mismatch", "C-11"),
    "Missing PO": ("Missing/Ambiguous PO", "C-12"), "Ambiguous PO": ("Missing/Ambiguous PO", "C-12"),
    "PO Conflict": ("Missing/Ambiguous PO", "C-12"),
    "Location ID": ("Mapping Exception", "C-13"), "Market Mapping": ("Mapping Exception", "C-13"),
    "Currency Mapping": ("Currency Exception", "C-14"), "Cross-border": ("Currency Exception", "C-14"),
    "USD Review": ("Currency Exception", "C-14"),
    "Quantity Mismatch": ("Quantity Mismatch", "C-15"), "Value Mismatch": ("Value Mismatch", "C-16"),
    "Lineage": ("Audit Exception", "C-17"), "Totals Audit": ("Audit Exception", "C-17"),
    "Malformed Source Row": ("Audit Exception", "C-17"),
    "Buyer Review": ("Header Exception", "C-02"),
}
RECORDED_NO_BARCODE, RECORDED_NO_VPN = "Recorded No Barcode", "Recorded No VPN"  # C-06 / C-07, line checks
# Owner rule BUYER-NAME (2026-10-05): Buyer Name is always the owner's own entity, read from the private
# config key ``buyer_name``; never written in code. Evidence is the printed quote, else the owner rule itself.
BUYER_RULE = "BUYER-NAME"
BUYER_RULE_EVIDENCE = "owner rule BUYER-NAME (2026-10-05)"
EVIDENCE_PRINTED, EVIDENCE_OWNER_RULE = "printed", "owner_rule"
EXTRA_HEADER_FIELDS = ["Buyer Name"]
ITEM_NOT_FOUND = "Not in Item Master"  # printed identifier with no Item Master row (not a disagreement)

# Exception types whose presence still allows approval (warnings only).
# GRN quantity differences are warnings (owner form 01a10c4d qty_cost_tolerance = invoice_flag).
NON_BLOCKING = {"Data Quality", "Entity Hint", "Description Check", "Totals Audit", "Quantity Mismatch",
                "Item Quantity Mismatch"}

# Owner form 01a10c4f (2026-10-05): item-line check; below the threshold the owner validates.
ITEM_LINE_THRESHOLD = Decimal("0.95")
ITEM_LINE_DEFINITION = ("lines resolved to exactly one Item Master ITEM_PARENT whose quantity agrees with the "
                        "accepted PO/GRN, divided by all invoice lines")
# Owner form 01a10c4f (answered 2026-10-05 13:48Z): pre-tax value tolerance per currency, compared at two decimals.
# The owner sheets name the check (POG-007, C-16); the amounts are this form answer, cited as the evidence.
VALUE_TOLERANCE_BY_CURRENCY = {"KWD": Decimal("1"), "AED": Decimal("2")}
VALUE_DECIMALS = 2
VALUE_TOLERANCE_SOURCE = "owner form 01a10c4f (2026-10-05 13:48Z): up to 1 KWD or 2 AED, rounded to 2 digits"
# Owner form 01a10c4c (2026-10-05 13:43Z): the loaded POGRN report is the owner's complete 25 Sep report.
POGRN_REPORT = "POGRN report of 25 Sep, confirmed complete by the owner (form 01a10c4c, 2026-10-05 13:43Z)"

TARGET_HEADER = ["Document", "Supplier Site", "Order No", "Location", "Location Type", "Document Date",
                 "Currency", "Gross Amount", "Tax Amount", "Net Amount", "Market", "Validation Status"]
TARGET_LINE = ["Transaction Number", "Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code", "VPN", "Brand",
               "Match Method", "Barcode Check", "VPN Check", "Description Check", "Source Row",
               "Validation Status", "POGRN RMS Order No", "POGRN Validation Status"]
MANDATORY_HEADER = ["Document", "Supplier Site", "Order No", "Location", "Location Type", "Document Date",
                    "Currency"]
MANDATORY_LINE = ["Item", "Unit Cost", "Quantity"]
# Header fields the review screen and download show; every one needs a lineage record to be displayed.
OWNER_HEADER_FIELDS = [f for f in TARGET_HEADER if f != "Validation Status"]


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
    # Owner supplier-site table: {supplier_site: {"currency", "status", "supplier_code", "supplier_name", "site_name"}}
    supplier_sites: dict = field(default_factory=dict)
    # SUP-001 bridge built from supplier_sites: {folded supplier name: {supplier code: [Active site ids]}}
    supplier_families: dict = field(default_factory=dict)
    # Owner VAT codes, type C / PV only: {vat region: {"code": ..., "rate": Decimal, "active_from": date}}
    vat_codes: dict = field(default_factory=dict)
    # Supplier sites with an approved USD exception (ALG-024).
    usd_exceptions: set = field(default_factory=set)
    entity_map: dict = field(default_factory=lambda: dict(ENTITY_MAP))
    qty_tolerance: Decimal = Decimal("0")
    value_tolerance: Decimal = Decimal("0")
    value_tolerance_by_currency: dict = field(default_factory=lambda: dict(VALUE_TOLERANCE_BY_CURRENCY))
    value_decimals: int = VALUE_DECIMALS
    # Description acceptance threshold is open (V-009); description never auto-approves.
    description_threshold: float = 0.6
    buyer_name: str = ""
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
        sites = {}
        for row in data.get("supplier_sites") or []:
            site, code, status = (str(row.get(k) or "").strip() for k in ("supplier_site", "currency", "status"))
            if not (site and re.fullmatch(r"[A-Z]{3}", code) and status in ("Active", "Inactive")):
                raise ValueError("Supplier-site rows need supplier_site, a 3-letter currency and Active/Inactive")
            if site in sites:
                raise ValueError(f"Supplier site {site} appears twice")
            sites[site] = {"currency": code, "status": status,
                           **{k: str(row.get(k) or "").strip() for k in ("supplier_code", "supplier_name", "site_name")}}
        families = defaultdict(lambda: defaultdict(list))
        for site, row in sites.items():
            if row["status"] == "Active" and row["supplier_name"] and row["supplier_code"]:
                families[name_key(row["supplier_name"])][row["supplier_code"]].append(site)
        vat = {}
        for row in data.get("vat_codes") or []:
            region, code = str(row.get("region") or "").strip().upper(), str(row.get("code") or "").strip()
            rate = to_decimal(row.get("rate"))
            try:
                active = date.fromisoformat(str(row.get("active_from") or ""))
            except ValueError:
                active = None
            if not (region and code and rate is not None and rate >= 0 and active):
                raise ValueError("VAT code rows need region, code, a non-negative rate and active_from")
            if region in vat:
                raise ValueError(f"VAT region {region} has two codes")
            vat[region] = {"code": code, "rate": rate, "active_from": active}
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
            supplier_sites=sites,
            supplier_families={k: {c: sorted(v) for c, v in codes.items()} for k, codes in families.items()},
            vat_codes=vat,
            usd_exceptions={str(x).strip() for x in data.get("usd_exceptions") or []},
            entity_map={**ENTITY_MAP, **{str(k).upper(): dict(v) for k, v in (data.get("entity_map") or {}).items()}},
            buyer_name=" ".join(str(data.get("buyer_name") or "").split()),
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


def name_key(value):
    """SUP-001: supplier names compare case-, accent-, space- and punctuation-free (letter-spaced banners too)."""
    return re.sub(r"[\W_]", "", fold(value))


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
        top = left = 0.0
        position = box.get("box")
        if isinstance(position, (list, tuple)) and len(position) == 4:
            try:
                top, left = float(position[1]), float(position[0])
            except (TypeError, ValueError):
                top = left = 0.0
        pages[page].append((top, value, left))
    page_text = {}
    if pages:
        for page, words in pages.items():
            rows, last = [], None
            for top, value, _ in sorted(words, key=lambda x: x[0]):
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
            "characters": {p: len(page_text.get(p, "")) for p in range(1, total + 1)},
            "words": {p: [(t, x, v) for t, v, x in words] for p, words in pages.items()}}


_LEGAL_SUFFIX = re.compile(
    r"(?i)\b(?:l\.?l\.?c|w\.?l\.?l|fz-?llc|fzco|fze|ltd|limited|inc|corp(?:oration)?|gmbh|s\.?a\.?r\.?l|"
    r"trading|est(?:ablishment)?|company|co\.)\b")
_SUPPLIER_LABEL = re.compile(
    r"(?i)^\s*(?:supplier|vendor|seller|sold\s+by|from|bill\s+from|beneficiary|company)(?:\s+name)?\s*[:\-]\s*(.{2,160})$")
_BUYER_LABEL = re.compile(r"(?i)\b(?:bill\s+to|ship\s+to|sold\s+to|buyer|customer|deliver\s+to|consignee)\b")


# Company-type words only (owner rule BUYER-NAME: the entity is the configured name without them).
_COMPANY_TYPES = {"co", "company", "llc", "l", "c", "wll", "w", "ltd", "limited", "inc", "plc", "spc", "corp",
                  "corporation", "fzco", "fze", "fzllc", "fz", "est", "establishment", "gmbh", "sa", "bv"}
_PARTY_BUYER = (("issued", "to"), ("bill", "to"), ("billed", "to"), ("sold", "to"), ("invoice", "to"),
                ("buyer",), ("customer",))
_PARTY_SELLER = (("issued", "by"), ("bill", "from"), ("sold", "by"), ("seller",), ("supplier",), ("vendor",))


def _tokens(value):
    return re.findall(r"[a-z0-9]+", fold(value))


def entity_tokens(value):
    """The configured name without its company-type words, as normalised whole words."""
    return [w for w in _tokens(value) if w not in _COMPANY_TYPES]


def _has_words(tokens, words):
    n = len(words)
    return bool(words) and any(tokens[i:i + n] == list(words) for i in range(len(tokens) - n + 1))


def _label_at(row, labels):
    """x position and index after the first party label found in a row of (x, word)."""
    words = [w for _, w in row]
    for label in labels:
        for i in range(len(words) - len(label) + 1):
            if tuple(_tokens(" ".join(words[i:i + len(label)]))) == label:
                return row[i][0], i + len(label)
    return None


def buyer_party(scan, depth=5):
    """Buyer side of the party row (Issued To / Bill To ...): (page, [row text]) from word positions.

    With a seller label on the same row the column boundary is midway between the two labels; without one,
    the buyer side starts at its label. Text without word positions gives no side (never guessed)."""
    for page in sorted(scan.get("words") or {}):
        rows = []
        for top, x, value in sorted(scan["words"][page]):
            if rows and abs(top - rows[-1][0]) <= 3:
                rows[-1][1].append((x, value))
            else:
                rows.append((top, [(x, value)]))
        rows = [(top, sorted(words)) for top, words in rows]
        for n, (top, row) in enumerate(rows):
            buyer = _label_at(row, _PARTY_BUYER)
            if not buyer:
                continue
            seller = _label_at(row, _PARTY_SELLER)
            boundary = (seller[0] + buyer[0]) / 2 if seller and seller[0] < buyer[0] else buyer[0]
            side = [" ".join(w for _, w in row[buyer[1]:] if _ >= boundary)]
            for _, below in rows[n + 1:n + 1 + depth]:
                if _label_at(below, _PARTY_BUYER + _PARTY_SELLER):
                    break
                side.append(" ".join(w for x, w in below if x >= boundary))
            side = [" ".join(r.split()).strip(" :|-,") for r in side]
            return page, [r for r in side if r]
    return None, []


def _without_buyer_side(value, buyer_rows):
    """Remove buyer-side party text from a supplier value: a whole buyer row, or the start of one that a reader
    joined onto the seller name across the party-row columns (left column first, so it trails the value)."""
    for row in buyer_rows:
        value = value.replace(row, "")
        words, row_words = value.split(), [fold(w) for w in row.split()]
        for k in range(min(len(words), len(row_words)), 0, -1):
            if [fold(w) for w in words[-k:]] == row_words[:k]:
                value = " ".join(words[:-k])
                break
    return value.strip(" :|-,")


def supplier_candidates(scan, printed_name=None, buyer_name=None, buyer_rows=()):
    """ALG-002: every supplier-name candidate across all pages, with page and region.

    Buyer-side party text is removed by position, never by name, since group companies sell to each other.
    A reader buyer that is only company-type words (a reader miss) excludes nothing."""
    found = []
    printed_buyer = fold(buyer_name) if buyer_name and entity_tokens(buyer_name) else ""
    buyer_rows = [r for r in buyer_rows if entity_tokens(r)]

    def add(value, page, line_no, lines, how):
        value = " ".join(text(value).split()).strip(" :|-,")
        value = _without_buyer_side(value, buyer_rows)
        if len(value) < 2 or (printed_buyer and printed_buyer in fold(value)):
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

    # The reader's supplier field loses any buyer-side text it took across the party row.
    printed_name = _without_buyer_side(" ".join(text(printed_name).split()), buyer_rows) if printed_name else ""
    if len(printed_name) >= 2:
        found.append({"name": printed_name, "page": None, "line": None, "region": "extracted",
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
    """ITM-003 / ITM-004: VPN column, else only a leading six-digit number in the description.

    Other numbers in the description are never VPN candidates (owner rule sheet, ITM-004).
    """
    if text(getattr(line, "sku", None)):
        return [{"value": text(line.sku), "origin": "VPN column", "primary": True}]
    leading = re.match(r"\s*(\d+)(?!\d)", text(getattr(line, "description", None)))
    if leading and len(leading.group(1)) == 6:
        return [{"value": leading.group(1), "origin": "description start", "primary": True}]
    return []


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


def same_market(a, b):
    return text(a).casefold() == text(b).casefold()


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
        # 03_Mandatory_Checklist: the owner's Failure Status is shown; the engine rule id stays in Rule ID.
        status, check = FAILURE_STATUS.get(kind, (kind, ""))
        entry = {"Exception ID": f"EX-{len(self.exceptions) + 1:03d}", "Invoice Number": text(self.invoice.number),
                 "Line No.": line or "", "Exception Type": status, "Description": description,
                 "Candidates / Evidence": evidence or "", "Proposed Resolution": proposed or "",
                 "Owner": owner, "Status": "Open", "Rule ID": rule, "Check ID": check, "Engine Type": kind,
                 "blocking": kind not in NON_BLOCKING}
        self.exceptions.append(entry)
        return entry["Exception ID"]

    def trace(self, target, value, rule, source, original=None, reference=None, confidence="Exact", line=None,
              evidence_kind=None):
        """R-018: original value, transformed value, rule ID, reference row and confidence."""
        if value in (None, ""):
            return
        record = {"target": target, "line": line, "original": text(original if original is not None else value),
                  "value": text(value), "rule": rule, "source": source, "reference": reference or "",
                  "confidence": confidence}
        if evidence_kind:
            record["evidence_kind"] = evidence_kind
        self.lineage.append(record)


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

    if not barcodes:
        result["checks"]["barcode"] = RECORDED_NO_BARCODE
    if not vpns:
        result["checks"]["vpn"] = RECORDED_NO_VPN
    barcode_parents = {p for _, rows in barcode_hits.values() for p in _unique_parents(_constrain(rows, sites)[0])}
    usable_vpn = vpn_hits
    vpn_parents = {p for _, rows in usable_vpn.values() for p in _unique_parents(_constrain(rows, sites)[0])}

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
        # ITM-006 / R-005: an internal item number equal to ITEM_PARENT comes after the sheet's three
        # routes and is a review candidate only (owner form 01a10c4f).
        value = text(getattr(line, "item_id", None))
        if value and _constrain([r for r in source.items_by_parent(value) if text(r.get("ITEM_PARENT")) == value],
                                sites)[0]:
            result["candidates"].insert(0, f"{value} (ITEM_PARENT printed)")
            result["method"] = "ITEM_PARENT candidate"
            reason, rules = "Printed internal item number equals an ITEM_PARENT; review candidate only", "ITM-006"
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
        # ITM-006: only an exact identifier that points elsewhere disagrees; a printed barcode with no Item
        # Master row at all is not found, and the VPN routes follow (owner answer on the e71a line-10 case).
        result["checks"]["barcode"] = "Fail" if barcode_hits else ITEM_NOT_FOUND
    hit_vpn = next((c["value"] for c in vpns if c["value"] in parent_vpns), None)
    column_vpn = next((c["value"] for c in vpns if c["origin"] == "VPN column"), None)
    if hit_vpn:
        result["checks"]["vpn"] = "Pass"
    elif (column_vpn and vpn_hits) or (vpn_parents and parent not in vpn_parents):
        result["checks"]["vpn"] = "Fail"
    elif column_vpn:
        result["checks"]["vpn"] = ITEM_NOT_FOUND
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


def supplier_family(run, candidates):
    """SUP-001 bridge: the printed supplier name in the owner's SUPPLIER SITES sheet gives one Supplier CODE
    and its Active supplier sites. Item Master SUPPLIER_NAME holds coded site names, not the legal name."""
    codes = defaultdict(set)
    for candidate in candidates:
        for code, sites in run.config.supplier_families.get(name_key(candidate["name"]), {}).items():
            codes[code].update(sites)
    return codes


def _site_names(source, site, rows):
    names = {text(r.get("SUPPLIER_NAME")) for r in rows if text(r.get("SUPPLIER")) == site} - {""}
    if not names and hasattr(source, "items_by_site"):
        rows = [r for r in source.items_by_site(site) if text(r.get("SUPPLIER")) == site]
        names = {text(r.get("SUPPLIER_NAME")) for r in rows} - {""}
    return names, rows


def resolve_supplier(run, source, matches, candidates):
    """SUP-001 / SUP-003, R-006 / ALG-002 / ALG-005: supplier site from the Item Master item-supplier relation.

    Returns (site, SUPPLIER_NAME, family). When several Active sites of one supplier family remain, the
    site is left open and ``family`` maps each site to its Item Master SUPPLIER_NAME; the accepted
    POGRN order then picks it (owner form 01a10c4f, site_choice).
    """
    by_name = []
    for candidate in candidates:
        by_name.extend(source.items_by_supplier_name(candidate["name"]))
    name_sites = {text(r.get("SUPPLIER")) for r in by_name if text(r.get("SUPPLIER"))}
    families = supplier_family(run, candidates)
    family_sites = set()
    if len(families) == 1:
        family_sites = next(iter(families.values()))
    elif len(families) > 1:
        run.exception("Supplier Exception", "Printed supplier name has several Supplier CODEs in SUPPLIER SITES",
                      "SUP-001", evidence=f"{len(families)} supplier codes", owner="Supplier operations")
    name_sites |= family_sites
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
    version = run.config.version
    if len(sites) == 1:
        site = next(iter(sites))
        names, site_rows = _site_names(source, site, rows)
        name = next(iter(names)) if len(names) == 1 else None
        bridged = site in family_sites
        run.trace("Supplier Site", site, "SUP-003", "Item Master SUPPLIER" + (" via SUPPLIER SITES" if bridged else ""),
                  reference=", ".join(x for x in (_refs(site_rows), f"{site}|{version}" if bridged else "") if x))
        if name is None:
            run.exception("Supplier Exception", "Supplier site has no single SUPPLIER_NAME in the Item Master",
                          "R-006", evidence=", ".join(sorted(names)), owner="Supplier operations")
        return site, name, None
    if len(sites) > 1 and sites <= family_sites:
        family = {}
        for site in sorted(sites):
            names, _ = _site_names(source, site, rows)
            if len(names) == 1:
                family[site] = next(iter(names))
        if family:
            return None, None, family
    if not candidates and not sites:
        run.exception("Supplier Exception", "No supplier name found anywhere on the invoice", "ALG-002",
                      owner="Supplier operations")
    elif not sites:
        run.exception("Supplier Exception", "Supplier not found in the Item Master or SUPPLIER SITES by name or "
                      "matched items", "R-006", evidence="; ".join(c["name"] for c in candidates)[:500],
                      owner="Supplier operations")
    else:
        run.exception("Supplier Exception", "Several Item Master supplier sites remain; resolve with market/entity",
                      "R-006", evidence=", ".join(sorted(sites)), owner="Supplier operations")
    return None, None, None


def choose_site(run, family, group):
    """SUP-003 with owner form 01a10c4f (site_choice = order_entity): the Active family site whose Item Master
    SUPPLIER_NAME starts with the accepted order's EBS code and whose entity/currency suffix equals the
    accepted LOCATION's ENTITY AND CURENCY in the LOCATIONS sheet. Anything else is left for review."""
    code, location = text(group["EBS Code"]).upper(), group["POGRN Location ID"]
    entity = text((run.config.location_master.get(location) or {}).get("entity_currency")).upper()
    chosen = [site for site, name in family.items() if ebs_key(name) == code and entity and
              re.sub(r"\s+", "", name).upper()[6:] == entity]
    if len(chosen) == 1:
        site = chosen[0]
        run.trace("Supplier Site", site, "SUP-003", "Item Master SUPPLIER via SUPPLIER SITES, accepted POGRN order "
                  "EBS code and LOCATIONS entity", original=family[site],
                  reference=f"{site}|{run.config.version}, {group['POGRN Row Reference']}")
        return site, family[site]
    run.exception("Supplier Site Exception", "Supplier family has several Active sites and the accepted order's "
                  "EBS code and location entity do not single one out", "SUP-003",
                  evidence=f"{len(family)} sites, {len(chosen)} agree", owner="Supplier operations")
    return None, None


# --------------------------------------------------------------------------- POGRN


def _group_rows(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(text(row.get("EBS_SUPPLIER_CODE")).upper(), text(row.get("RMS_ORDER_NO")), text(row.get("LOCATION")))].append(row)
    return groups


def _value_variance(config, invoice_value, cost, currency):
    """POG-007 with owner form 01a10c4f: both sides at two decimals, tolerance per currency (1 KWD, 2 AED)."""
    places = Decimal(1).scaleb(-config.value_decimals)
    variance = (invoice_value.quantize(places, rounding=ROUND_HALF_UP) -
                cost.quantize(places, rounding=ROUND_HALF_UP))
    tolerance = config.value_tolerance_by_currency.get(text(currency).upper(), config.value_tolerance)
    return variance, abs(variance) <= tolerance


def evaluate_pogrn(run, rows, keys, invoice_qty, invoice_value, invoice_currency, supplier_site, name_raw,
                   name_master, supplier_value, parents=()):
    """POG-001..POG-007, R-020..R-030: candidate orders aggregated by (EBS, RMS order, location).

    ``keys`` are the supplier family's EBS codes (SUP-002); ``parents`` the invoice's resolved ITEM_PARENTs,
    which must all appear in the order's RMS_ITEM_ID (item identity).
    """
    config = run.config
    keys = {k.upper() for k in keys or () if k}
    parents = set(parents or ())
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
        supplier_ok = code in keys
        order_ok = bool(order)
        order_items = {text(r.get("RMS_ITEM_ID")) for r in group}
        items_ok = bool(parents) and parents <= order_items
        if not supplier_ok:
            reasons.append("EBS supplier code is not the supplier's")
        if not order_ok:
            reasons.append("RMS_ORDER_NO blank")
        if not parents:
            reasons.append("No resolved invoice item to compare")
        elif not items_ok:
            reasons.append(f"{len(parents - order_items)} of {len(parents)} invoice items not on the order")
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
        # POG-006 / R-028: QTY_RECEIVED at the same aggregation level, tolerance default 0.
        qty_variance = invoice_qty - qty if invoice_qty is not None and qty is not None else None
        qty_ok = qty_variance is not None and abs(qty_variance) <= config.qty_tolerance
        if qty is None:
            reasons.append("No QTY_RECEIVED on this order/location")
        elif invoice_qty is None:
            reasons.append("Invoice quantity unavailable")
        elif not qty_ok:
            reasons.append(f"Quantity variance {qty_variance}")
        # POG-007 / R-029: currency must agree first, then pre-tax value over the same rows.
        currency_ok = bool(invoice_currency) and currencies == {invoice_currency.upper()}
        value_variance, value_ok = None, False
        if invoice_value is not None and cost is not None:
            value_variance, value_ok = _value_variance(config, invoice_value, cost, invoice_currency)
            value_ok = value_ok and currency_ok
        if not currency_ok:
            reasons.append(f"Currency differs or unknown (invoice {invoice_currency or 'blank'}, POGRN "
                           f"{'/'.join(sorted(currencies)) or 'blank'})")
        elif cost is None:
            reasons.append("TOTAL COST missing on counted rows")
        elif invoice_value is None:
            reasons.append("Invoice value before tax unavailable")
        elif not value_ok:
            reasons.append(f"Value variance {value_variance}")
        # Quantity is reported (warning, owner form 01a10c4d) but does not decide the pass.
        passed = all((supplier_ok, order_ok, items_ok, location_ok, type_ok, market_ok, value_ok))
        receipt_dates = sorted({text(r.get("RECEIPT_DATE"))[:10] for r in group if text(r.get("RECEIPT_DATE"))})
        evaluated.append({
            "Invoice Number": text(run.invoice.number), "Supplier Name Raw": name_raw or "",
            "Item Master Supplier Name": name_master or "", "Supplier Value": supplier_value or "",
            "EBS Supplier Code 6D": "/".join(sorted(keys)), "EBS Code": code, "Invoice Quantity": invoice_qty,
            "Invoice Value Before Tax": invoice_value,
            "POGRN Quantity": qty, "POGRN Value Before Tax": cost,
            "Qty Match": "Pass" if qty_ok else "Fail", "Pre-Tax Value Match": "Pass" if value_ok else "Fail",
            "POGRN RMS Order No": order, "Derived PO Number": "", "PO Source": "",
            "POGRN Row Reference": _refs(group), "Validation Status": "Pass" if passed else "Fail",
            "Exception Reason": "; ".join(reasons),
            "POGRN Location ID": location, "Location Type": kind["type"] or "", "Market": market or "",
            "Aggregated QTY_RECEIVED": qty, "Quantity Variance": qty_variance, "Quantity Result": "Pass" if qty_ok else "Fail",
            "Aggregated TOTAL COST": cost, "Value Variance": value_variance, "Value Result": "Pass" if value_ok else "Fail",
            "Value Tolerance Source": VALUE_TOLERANCE_SOURCE if value_variance is not None else "",
            "Receipt Dates": ", ".join(receipt_dates), "Supplier Check": "Pass" if supplier_ok else "Fail",
            "Items Check": "Pass" if items_ok else "Fail",
            "Location Check": "Pass" if location_ok and type_ok else "Fail", "Market Check": "Pass" if market_ok else "Fail",
            "Candidate Pass Count": None, "_passed": passed, "_location_type": kind, "_market": market,
            "_rows": group, "_identity": supplier_ok and order_ok and items_ok,
            "_checks": {"qty": qty_ok, "value": value_ok, "location": location_ok, "type": type_ok, "market": market_ok},
        })
    passing = [g for g in evaluated if g["_passed"]]
    for g in evaluated:
        g["Candidate Pass Count"] = len({x["POGRN RMS Order No"] for x in passing})
    return evaluated, passing


def _validate_accepted(run, group):
    """POG-006 / POG-007 on the accepted order: a GRN quantity difference is a warning with the invoice quantity
    used (owner form 01a10c4d); a value outside tolerance asks the owner to verify (owner form 01a10c4f)."""
    order = group["POGRN RMS Order No"]
    if not group["_checks"]["qty"]:
        run.exception("Quantity Mismatch", f"Order {order}: invoice quantity does not agree with aggregated "
                      "QTY_RECEIVED; warning, the invoice quantity is used", "POG-006",
                      evidence=group["Exception Reason"], owner="Buyer")
    if not group["_checks"]["value"]:
        run.exception("Value Mismatch", f"Order {order}: invoice value before tax does not agree with aggregated "
                      "TOTAL COST within tolerance; please verify", "POG-007",
                      evidence=f"{group['Exception Reason']}; tolerance {VALUE_TOLERANCE_SOURCE}", owner="Buyer")


MALFORMED_REASON = "Source row malformed in extract"


def malformed_row(row):
    """A POGRN row whose columns are shifted in the extract: RMS_ITEM_ID present and not nine digits, or a
    CREATED_DATE column that is blank or not a date. Such rows are never lookup keys or evidence."""
    item = row.get("RMS_ITEM_ID")
    if text(item) and not re.fullmatch(r"\d{9}", text(item)):
        return True
    if "CREATED_DATE" in row:
        created = row["CREATED_DATE"]
        return not (isinstance(created, (date, datetime)) or re.match(r"\d{4}-\d\d-\d\d", text(created)))
    return False


def _well_formed(run, rows, outcome, touched):
    """Drop shifted rows. When one would have taken part (``touched``), the invoice stays flagged."""
    bad = [r for r in rows if malformed_row(r)]
    hits = [r for r in bad if touched(r)]
    if hits:
        run.exception("Malformed Source Row", f"{MALFORMED_REASON}: {len(hits)} POGRN row(s) for this order or "
                      "invoice items excluded from lookup and evidence; owner's unfiltered report needed",
                      "POG-001", evidence=_refs(hits), owner="Buyer")
        outcome["malformed"] = len(hits)
    return [r for r in rows if not malformed_row(r)]


def resolve_po(run, source, keys, invoice_qty, invoice_value, supplier_site, name_raw, name_master, parents=()):
    """POG-001: the printed PO first (an exact RMS_ORDER_NO), else the SUP-002 6-character EBS link alone.

    Owner form 01a10c4d order_link = strict_6: Order No is the printed order under the supplier's 6-character EBS
    code, or the only order under that code. Items, quantity and value validate the linked order, never find one.
    Zero or several candidates leave it empty and flagged; never the closest.
    """
    invoice = run.invoice
    printed = text(invoice.po)
    currency = text(invoice.currency) or None
    outcome = {"order": None, "source": None, "group": None, "validation": [], "status": "Missing PO",
               "candidates": 0, "malformed": 0}
    common = (keys, invoice_qty, invoice_value, currency, supplier_site, name_raw, name_master, supplier_site,
              parents)
    if printed:
        rows = [r for r in source.pogrn_by_order(printed) if text(r.get("RMS_ORDER_NO")) == printed]
        rows = _well_formed(run, rows, outcome, lambda r: True)
        if not rows:
            run.exception("Missing PO", "Printed PO is not an RMS_ORDER_NO in the POGRN report (order not found)",
                          "POG-001", evidence=POGRN_REPORT, owner="Buyer")
            run.trace("Order No", printed, "R-024", "Invoice PO (not in POGRN)", confidence="Unvalidated")
            outcome.update(order=printed, source=FROM_INVOICE, status="Order not found")
            return outcome
        evaluated, _ = evaluate_pogrn(run, rows, *common)
        outcome["validation"] = evaluated
        mine = [g for g in evaluated if g["Supplier Check"] == "Pass"]
        if not mine:
            # Owner form 01a10c4d order_link = strict_6: an order is linked only through the supplier's 6-character
            # EBS code (SUP-002), so a printed order of another code is not filled.
            run.exception("POGRN Supplier Exception", f"Printed PO {printed}: its EBS_SUPPLIER_CODE is not the "
                          "invoice supplier's 6-character code; Order No left empty" if keys else
                          f"Printed PO {printed}: supplier unresolved, so the order's EBS code cannot be checked; "
                          "Order No left empty", "SUP-002",
                          evidence="/".join(sorted({g["EBS Code"] for g in evaluated})), owner="Supplier operations")
            outcome.update(order=None, source=None, candidates=0, status="Printed PO not validated")
            return outcome
        run.trace("Order No", printed, "POG-001", "Invoice PO found as POGRN RMS_ORDER_NO of the supplier's "
                  "6-character EBS code", reference=_refs(rows))
        outcome.update(order=printed, source=FROM_INVOICE, candidates=1)
        if len(mine) > 1:
            run.exception("Location ID", "Printed order has several locations; no approved multi-location rule",
                          "POG-002", evidence=f"{len(mine)} locations", owner="Buyer")
            outcome["status"] = "Location ambiguous"
            return outcome
        group = mine[0]
        group.update({"Derived PO Number": printed, "PO Source": FROM_INVOICE})
        _validate_accepted(run, group)
        outcome.update(group=group, status="Approved" if group["_passed"] else "Accepted, checks to verify")
        return outcome
    if not keys:
        run.exception("POGRN Supplier Exception", "No EBS supplier code, so POGRN cannot be searched", "SUP-002",
                      owner="Supplier operations")
        return outcome
    rows = [r for key in sorted(keys) for r in source.pogrn_by_ebs(key)
            if text(r.get("EBS_SUPPLIER_CODE")).upper() in {k.upper() for k in keys}]
    rows = _well_formed(run, rows, outcome, lambda r: True)
    if not rows:
        run.exception("POGRN Supplier Exception", "No POGRN rows for the supplier's EBS code", "SUP-002",
                      evidence="/".join(sorted(keys)), owner="Buyer")
        return outcome
    # Owner form 01a10c4d order_link = strict_6: the candidates are the orders of the 6-character EBS code only;
    # items never find an order. Exactly one order is linked; several are ambiguous (POG-001).
    evaluated, _ = evaluate_pogrn(run, rows, *common)
    outcome["validation"] = evaluated
    orders = sorted({g["POGRN RMS Order No"] for g in evaluated})
    outcome["candidates"] = len(orders)
    if len(orders) > 1:
        run.exception("Ambiguous PO", f"No printed PO and {len(orders)} POGRN orders under the supplier's "
                      "6-character EBS code; owner review", "POG-001", evidence=", ".join(orders[:20]), owner="Buyer")
        outcome["status"] = "Ambiguous PO"
        return outcome
    derived = orders[0]
    identified = evaluated
    run.trace("Order No", derived, "POG-001", "POGRN RMS_ORDER_NO (only order under the supplier's 6-character EBS "
              "code, SUP-002)", reference=_refs([r for g in identified for r in g["_rows"]]),
              confidence=DERIVED_FROM_POGRN)
    outcome.update(order=derived, source=DERIVED_FROM_POGRN)
    if len(identified) > 1:
        run.exception("Location ID", "Derived order has several locations; no approved multi-location rule",
                      "POG-002", evidence=f"{len(identified)} locations", owner="Buyer")
        outcome["status"] = "Location ambiguous"
        return outcome
    group = identified[0]
    group.update({"Derived PO Number": derived, "PO Source": DERIVED_FROM_POGRN})
    _validate_accepted(run, group)
    outcome.update(group=group, status="Approved" if group["_passed"] else "Accepted, checks to verify")
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
    source, reference = "Supplier-site-market currency map", f"{supplier_site}|{market}|{config.version}"
    site = config.supplier_sites.get(supplier_site) if configured is None else None
    if site and site["status"] != "Active":
        # R-012: the current currency maintained for the site; an inactive site is not current.
        run.exception("Currency Mapping", f"Supplier site {supplier_site} is {site['status']} in the supplier-site "
                      "table; its currency is not current", "R-012", evidence=site["currency"], owner="Finance")
        return None
    if site:
        # ALG-025: the payment currency is the one configured for the supplier site, whatever the market.
        configured, source, reference = site["currency"], "Supplier-site table", f"{supplier_site}|{config.version}"
    if configured is None:
        # R-013 / ALG-025: a cross-border pair is accepted only when configured.
        kind = "Cross-border" if hint and hint.get("mapped") and not same_market(hint["mapped"].get("market"), market) else "Currency Mapping"
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
    run.trace("Currency", configured, "ALG-023", source, reference=reference)
    return configured


# --------------------------------------------------------------------------- tax code


def resolve_tax_code(run, market, document_date):
    """R-016: Unit Tax Code is the owner's C/PV code of the receiving market's VAT region (R-013).

    The printed tax must agree with the region rate, either on the invoice net or line by line.
    """
    invoice = run.invoice
    printed = text(invoice.taxCode)
    if printed or not market or not run.config.vat_codes:
        return printed
    vat = run.config.vat_codes.get(text(market).upper())
    if not vat:
        run.exception("Tax Code", f"No C/PV VAT code for the {market} VAT region", "R-016", owner="Tax reviewer")
        return ""
    if not document_date or document_date < vat["active_from"].isoformat():
        run.exception("Tax Code", f"VAT code {vat['code']} applies from {vat['active_from'].isoformat()}; "
                      "document date is earlier or unknown", "R-016", owner="Tax reviewer")
        return ""
    if invoice.tax is None:
        run.exception("Tax Code", "Printed tax amount is missing; the region rate cannot be checked", "R-016",
                      owner="Tax reviewer")
        return ""

    def at_rate(amount):
        return (amount * vat["rate"] / 100).quantize(invoice.tax, rounding=ROUND_HALF_UP)

    nets = [line.net_amount for line in invoice.lines]
    agrees = (invoice.net is not None and at_rate(invoice.net) == invoice.tax) or (
        nets and None not in nets and sum((at_rate(n) for n in nets), Decimal(0)) == invoice.tax)
    if not agrees:
        run.exception("Tax Code", f"Printed tax {invoice.tax} does not agree with the {vat['rate']}% rate of "
                      f"VAT code {vat['code']}", "R-016", owner="Tax reviewer")
        return ""
    return vat["code"]


# --------------------------------------------------------------------------- invoice run


def _line_value(invoice):
    if invoice.net is not None:
        return invoice.net, "printed net"
    nets = [l.net_amount for l in invoice.lines]
    if nets and None not in nets:
        return sum(nets, Decimal(0)), "sum of printed line amounts"
    return None, None


def _printed_page(scan, amount):
    """Page on which a printed amount appears (as printed, with or without thousands separators)."""
    if amount is None:
        return None
    forms = {f"{amount}", f"{amount:,}"}
    for page, value in scan["pages"].items():
        flat = value.replace(" ", "")
        if any(re.search(r"(?<![\d.,])" + re.escape(f.replace(" ", "")) + r"(?![\d])", flat) for f in forms):
            return page
    return None


def _different_company(name, owner):
    """A buyer names a different company only when it has a name beyond company-type words and that name does
    not contain the owner's entity. Company-type fragments (a reader miss such as "Co. L.L.C") are not one."""
    words = entity_tokens(name)
    return bool(words) and not _has_words(words, entity_tokens(owner))


def resolve_buyer(run, scan, page, rows):
    """Owner rule BUYER-NAME: Buyer Name is always the configured owner entity. Matched by the entity (the name
    without company-type words, whole words) on the buyer side of the party row; the printed buyer line and
    page are the evidence, else the owner rule is. A buyer side naming another company is a review flag."""
    owner = run.config.buyer_name
    if not owner:
        return ""
    entity = entity_tokens(owner)
    quote = next((r for r in rows if _has_words(_tokens(r), entity)), None)
    if quote:
        run.trace("Buyer Name", owner, BUYER_RULE, "Printed buyer line (party row, buyer side)", original=quote,
                  reference=f"page {page}", evidence_kind=EVIDENCE_PRINTED)
    else:
        run.trace("Buyer Name", owner, BUYER_RULE, BUYER_RULE_EVIDENCE, reference=BUYER_RULE_EVIDENCE,
                  confidence="Owner rule", evidence_kind=EVIDENCE_OWNER_RULE)
    named = next((r for r in rows if entity_tokens(r)), None) if rows else run.invoice.buyer_name
    if not quote and named and _different_company(named, owner):
        run.exception("Buyer Review", "printed buyer differs from owner entity", BUYER_RULE,
                      evidence=f"page {page}" if rows else "reader buyer field", owner="Accounts payable")
    return owner


def trace_totals(run, scan):
    """TGT-001 financial totals: Net and Tax Amount are the printed totals, kept only with the page they are
    printed on; Gross Amount is printed Net + printed Tax. A total not found in the text is flagged."""
    invoice = run.invoice
    pages = {}
    for target, amount in (("Net Amount", invoice.net), ("Tax Amount", invoice.tax)):
        if amount is None:
            continue
        page = _printed_page(scan, amount)
        if page is None:
            run.exception("Totals Audit", f"{target} {amount} is not found in the invoice text", "TGT-001",
                          owner="Accounts payable")
            continue
        pages[target] = page
        run.trace(target, amount, "TGT-001", "Invoice printed total", reference=f"page {page}")
    if len(pages) < 2:
        return None
    gross = invoice.net + invoice.tax
    page = _printed_page(scan, gross)
    run.trace("Gross Amount", gross, "TGT-001", "Printed Net Amount + printed Tax Amount",
              original=f"{invoice.net} + {invoice.tax}",
              reference=f"page {page}" if page else f"pages {pages['Net Amount']}, {pages['Tax Amount']}")
    return gross


def item_resolution(run, matches, group):
    """Owner form 01a10c4f item-line check: lines with one ITEM_PARENT whose quantity agrees with the accepted
    PO/GRN rows of that item (C-11), over all lines. Below 95 % the owner validates."""
    received = defaultdict(lambda: None)
    for row in (group or {}).get("_rows", ()):
        qty = to_decimal(row.get("QTY_RECEIVED"))
        if qty is not None:
            parent = text(row.get("RMS_ITEM_ID"))
            received[parent] = (received[parent] or Decimal(0)) + qty
    invoiced = defaultdict(Decimal)
    for m, line in zip(matches, run.invoice.lines):
        if m["status"] == "Matched" and line.qty is not None:
            invoiced[m["parent"]] += line.qty
    resolved, mismatched = 0, []
    for m, line in zip(matches, run.invoice.lines):
        if m["status"] != "Matched" or not group:
            continue
        got = received[m["parent"]]
        if got is not None and abs(invoiced[m["parent"]] - got) <= run.config.qty_tolerance:
            resolved += 1
        else:
            mismatched.append(m["line"])
    if mismatched:
        run.exception("Item Quantity Mismatch", f"{len(mismatched)} line(s) whose item quantity does not agree "
                      "with the accepted order's QTY_RECEIVED for that item; warning, the invoice quantity is used",
                      "C-11",
                      evidence="lines " + ", ".join(map(str, mismatched[:30])), owner="Buyer")
    total = len(matches)
    rate = (Decimal(resolved) / total).quantize(Decimal("0.0001")) if total else Decimal(0)
    below = rate < ITEM_LINE_THRESHOLD
    if total and below:
        run.exception("Owner Validation", f"Item-line check {resolved}/{total} is below 95 %; owner validates",
                      "ITEM-LINE-95", owner="Owner")
    return {"resolved": resolved, "total": total, "rate": rate, "threshold": ITEM_LINE_THRESHOLD,
            "below": below, "definition": ITEM_LINE_DEFINITION}


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
    buyer_page, buyer_rows = buyer_party(scan)
    candidates = supplier_candidates(scan, invoice.supplier_name, invoice.buyer_name, buyer_rows)
    first_names = {r_site for c in candidates for r_site in
                   (text(r.get("SUPPLIER")) for r in source.items_by_supplier_name(c["name"])) if r_site}
    # A probe pass finds the supplier from unambiguous lines; the recorded pass then
    # constrains every line by that supplier (ALG-018 / R-015).
    probe = Run(invoice, config, filename)
    probe_matches = [match_line(probe, n, line, source, first_names or None) for n, line in enumerate(invoice.lines, 1)]
    probe_site, _, probe_family = resolve_supplier(probe, source, probe_matches, candidates)
    sites = {probe_site} if probe_site else (set(probe_family) if probe_family else (first_names or None))
    matches = [match_line(run, n, line, source, sites) for n, line in enumerate(invoice.lines, 1)]
    supplier_site, master_name, family = resolve_supplier(run, source, matches, candidates)
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
    keys = {key} if key else {ebs_key(n) for n in (family or {}).values()} - {None}
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
    parents = {m["parent"] for m in matches if m["status"] == "Matched"}
    po = resolve_po(run, source, keys, invoice_qty, invoice_value, supplier_site, raw_name, master_name, parents)
    group = po["group"]
    if family and not supplier_site:
        if group:
            supplier_site, master_name = choose_site(run, family, group)
        else:
            run.exception("Supplier Site Exception", "Supplier family has several Active sites; the site waits for "
                          "an accepted POGRN order", "SUP-003", evidence=f"{len(family)} sites",
                          owner="Supplier operations")
        if supplier_site:
            key = ebs_key(master_name)
            run.trace("EBS Supplier Code 6D", key, "ALG-006", "Item Master SUPPLIER_NAME", original=master_name)
            hint = entity_hint(master_name, config.entity_map)
            for m in matches:
                if m["status"] == "Matched":
                    m["rows"] = [r for r in m["rows"] if text(r.get("SUPPLIER")) == supplier_site] or m["rows"]
    location = loc_type = market = None
    if group:
        location = group["POGRN Location ID"]
        loc_type = group["_location_type"]["type"] if group["_checks"]["type"] else None
        market = group["_market"]
        if location and not loc_type:
            run.exception("Location ID", "Location type unresolved or master and prefix disagree", "POG-003",
                          evidence=group["Exception Reason"], owner="Buyer")
        if location and not market:
            run.exception("Market Mapping", "No approved market for the accepted location", "POG-004",
                          evidence=group["Exception Reason"], owner="Business")
        run.trace("Location", location, "ALG-011", "Accepted POGRN LOCATION", reference=group["POGRN Row Reference"])
        run.trace("Location Type", loc_type, "ALG-014" if group["_location_type"].get("source") == "location master"
                  else ("ALG-012" if loc_type == WAREHOUSE else "ALG-013"), group["_location_type"].get("source", ""),
                  original=location)
        run.trace("Market", market, "R-027", "Location-market mapping", original=location)
    elif po["status"] != "Location ambiguous":
        run.exception("Location ID", "Location waits for an accepted POGRN order (never from Item Master)",
                      "ALG-011", owner="Buyer")
    if hint and hint["mapped"] and market and not same_market(hint["mapped"]["market"], market):
        run.exception("Market Mapping", f"Supplier suffix suggests {hint['mapped']['market']} but the receiving "
                      f"location is {market}", "R-027", owner="Business")
    currency = resolve_currency(run, supplier_site, market, hint) if group else None
    if not group:
        run.exception("Currency Mapping", "Currency waits for the receiving market of an accepted order",
                      "ALG-023", owner="Finance")

    # Line outputs (ALG-021/022/026/027).
    tax_code = resolve_tax_code(run, market, document_date)
    tax_source = "Reviewed invoice tax code" if text(invoice.taxCode) else "Owner VAT code table (C/PV), receiving market"
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
            printed_upc = text(line.gtin) if m["barcode"] is None or text(line.gtin) else text(line.description)
            stripped = text(line.gtin)[:4].upper() == "ULT_" and upc in normalize_barcode(text(line.gtin))
            run.trace("UPC", upc, "ALG-027" + (" (ULT_ prefix removed, R-004)" if stripped else ""),
                      "Invoice barcode" + ("" if text(line.gtin) else " in description"),
                      original=printed_upc or line.description, line=n,
                      reference=f"page {line.page}" if line.page else "")
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
            run.trace(f, v, "ALG-026", "Invoice line", line=n, reference=f"page {line.page}" if line.page else "")
        if tax_code:
            run.trace("Unit Tax Code", tax_code, "R-016", tax_source, line=n)
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
    if not tax_code and invoice.lines and not any(e["Exception Type"] == "Tax Code" for e in run.exceptions):
        run.exception("Tax Code", "Unit Tax Code is blank; no approved tax mapping in the ULTA sources", "R-016",
                      owner="Tax reviewer")

    gross = trace_totals(run, scan)
    buyer = resolve_buyer(run, scan, buyer_page, buyer_rows)
    resolution = item_resolution(run, matches, group)

    header = {
        "Document": document, "Supplier Site": supplier_site or "", "Order No": po["order"] or "",
        "Location": location or "", "Location Type": loc_type or "", "Document Date": document_date or "",
        "Currency": currency or "", "Gross Amount": gross,
        "Tax Amount": invoice.tax, "Net Amount": invoice.net, "Market": market or "", "Buyer Name": buyer,
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
    elif any(e["Rule ID"] == "R-001" for e in blocking):
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
        "ebs_supplier_code": key or "/".join(sorted(keys)), "entity_hint": hint, "config_version": config.version,
        "item_resolution": resolution, "po_candidates": po["candidates"],
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

    def items_by_site(self, value):
        return [r for r in self.items if text(r.get("SUPPLIER")) == text(value)]

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
