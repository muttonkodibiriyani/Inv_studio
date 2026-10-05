from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Line(StrictModel):
    item_id: str | None = None
    sku: str | None = None
    gtin: str | None = None
    description: str | None = None
    qty: Decimal | None = None
    uom: str | None = None
    price: Decimal | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    evidence: str | None = None
    page: int | None = Field(default=None, ge=1, le=20)


class Invoice(StrictModel):
    number: str | None = None
    supplier_name: str | None = None
    buyer_name: str | None = None
    seller: str | None = None
    site: str | None = None
    buyer: str | None = None
    po: str | None = None
    location: str | None = None
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
    ai_fallback: bool = True
    provider: Literal["openai", "anthropic", "chatgpt", "claude_local", "vertex"] = "openai"
    model: str = Field(default="", max_length=150, pattern=r"^[A-Za-z0-9._:/-]*$")
    language: Literal["en", "ar", "ch", "fr", "de"] = "en"


def extraction_schema():
    # A deliberately simple schema shared by the two provider APIs. Monetary
    # values are strings so digits survive JSON decoding without float rounding.
    nullable = lambda: {"type": ["string", "null"]}
    fields = {k: nullable() for k in Invoice.model_fields if k != "lines"}
    line = {k: nullable() for k in Line.model_fields if k != "page"}
    line["page"] = {"type": ["integer", "null"]}
    fields["lines"] = {"type": "array", "items": {"type": "object", "properties": line,
        "required": list(line), "additionalProperties": False}}
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
