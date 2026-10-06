from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Line(StrictModel):
    item_id: str | None = None
    sku: str | None = None
    gtin: str | None = None
    # The printed non-GTIN code of a part-number column, kept whole as printed even when
    # the description's own code fills sku. Local table reads only; never asked of an AI.
    part_code: str | None = None
    description: str | None = None
    qty: Decimal | None = None
    uom: str | None = None
    price: Decimal | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    evidence: str | None = None
    page: int | None = Field(default=None, ge=1, le=20)
    # A complete printed barcode that fails the GTIN check digit, digits only; gtin stays empty.
    # Set by the reader, never by an AI: it is not in the extraction schema.
    barcode_unchecked: str | None = None


class Invoice(StrictModel):
    number: str | None = None
    supplier_name: str | None = None
    buyer_name: str | None = None
    seller: str | None = None
    site: str | None = None
    buyer: str | None = None
    po: str | None = None
    location: str | None = None
    date_printed: str | None = Field(default=None, max_length=40)
    date: str | None = None
    currency: str | None = None
    origin: str | None = None
    market: str | None = None
    taxCode: str | None = None
    net: Decimal | None = None
    tax: Decimal | None = None
    lines: list[Line] = Field(default_factory=list, max_length=1000)


class Policy(StrictModel):
    price_tolerance: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    total_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0, le=100)
    currency_decimals: dict[str, int] = Field(default_factory=lambda: {"AED": 2, "KWD": 3, "USD": 2, "EUR": 2})


class ProcessingOptions(StrictModel):
    engine: Literal["auto", "invoice2data", "paddleocr", "docling", "ai"] = "auto"
    prefer_native_text: bool = True
    ai_fallback: bool = True
    # An AI call on an invoice the local engines read completely: flags only, a cost, so off unless chosen.
    ai_cross_check: bool = False
    provider: Literal["openai", "anthropic", "chatgpt", "claude_local", "vertex"] = "openai"
    model: str = Field(default="", max_length=150, pattern=r"^[A-Za-z0-9._:/-]*$")
    language: Literal["en", "ar", "ch", "fr", "de"] = "en"


# Printed header fields an AI reader quotes with a page: the evidence sits beside the invoice, never inside it.
HEADER_EVIDENCE_FIELDS = ("number", "supplier_name", "buyer_name", "po", "date", "currency", "net", "tax")


def extraction_schema():
    # A deliberately simple schema shared by the two provider APIs. Monetary
    # values are strings so digits survive JSON decoding without float rounding.
    nullable = lambda: {"type": ["string", "null"]}
    fields = {k: nullable() for k in Invoice.model_fields if k != "lines"}
    line = {k: nullable() for k in Line.model_fields if k not in ("page", "part_code", "barcode_unchecked")}
    line["page"] = {"type": ["integer", "null"]}
    fields["lines"] = {"type": "array", "items": {"type": "object", "properties": line,
        "required": list(line), "additionalProperties": False}}
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def ai_schema():
    """The extraction schema plus header_evidence: one {quote, page} per printed header field."""
    schema = extraction_schema()
    entry = {"type": ["object", "null"], "properties": {"quote": {"type": ["string", "null"]},
             "page": {"type": ["integer", "null"]}}, "required": ["quote", "page"], "additionalProperties": False}
    schema["properties"]["header_evidence"] = {"type": ["object", "null"],
        "properties": {field: dict(entry) for field in HEADER_EVIDENCE_FIELDS},
        "required": list(HEADER_EVIDENCE_FIELDS), "additionalProperties": False}
    schema["required"] = list(schema["properties"])
    return schema


def parse_ai_output(data):
    """Split a reader's JSON (text or dict) into (Invoice, header evidence); evidence never enters the invoice."""
    import json
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("AI output is not an object")
    raw = data.pop("header_evidence", None)
    evidence = {}
    for field in HEADER_EVIDENCE_FIELDS:
        entry = (raw or {}).get(field) if isinstance(raw, dict) else None
        quote = entry.get("quote") if isinstance(entry, dict) else None
        page = entry.get("page") if isinstance(entry, dict) else None
        if isinstance(quote, str) and quote.strip():
            evidence[field] = {"quote": quote.strip()[:300],
                               "page": page if isinstance(page, int) and 1 <= page <= 20 else None}
    for line in data.get("lines") or []:
        if isinstance(line, dict):
            line.pop("part_code", None)  # a local table fact; an AI never supplies it
            line.pop("barcode_unchecked", None)  # the reader's verdict on a printed code; an AI never sets it
    return Invoice.model_validate(data), evidence
