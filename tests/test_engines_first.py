"""Engines first, then the AI per field: fallback, gap fill, cross-check, disagreement flags. Synthetic data only."""

import pytest

from app import engines
from app.models import Invoice
from tests.test_engine_flow import complete_native_invoice, identityless_native_invoice, setup


def run(monkeypatch, tmp_path, native, ai_invoice, ai_fallback=True, model="stub-model", error=None):
    calls, opts, store = setup(monkeypatch, tmp_path, "")
    opts.ai_fallback = ai_fallback
    opts.model = model

    def read(engine, *args):
        calls.append(engine)
        return {"text": f"{engine} text", "boxes": [], "invoice": native if engine == "invoice2data" else None}

    monkeypatch.setattr(engines, "local_read", read)

    def ai_reader(*args):
        calls.append("ai")
        if error:
            raise error
        evidence = {"number": {"quote": "Invoice AI-1", "page": 1}}
        return Invoice.model_validate(ai_invoice), {"evidence": evidence}

    return engines.process(tmp_path / "invoice.pdf", opts, store, ai_reader), calls


def test_a_complete_reconciled_engine_read_makes_no_ai_call_unless_cross_check_is_on(monkeypatch, tmp_path):
    monkeypatch.delenv("INV_STUDIO_AI_CROSS_CHECK", raising=False)
    native = {**complete_native_invoice(), "date": "2026-01-15"}
    result, calls = run(monkeypatch, tmp_path, native, {**native, "number": "AI-1"})
    assert calls.count("ai") == 0
    assert result["readers"]["ai"] == {"status": "skipped", "reason": engines.AI_REASONS["skipped"], "calls": 0}

    monkeypatch.setenv("INV_STUDIO_AI_CROSS_CHECK", "1")
    result, calls = run(monkeypatch, tmp_path, native, {**native, "number": "AI-1"})
    assert calls.count("ai") == 1 and result["readers"]["ai"]["status"] == "cross_check"
    # The cross-check never overwrites: the engine number stays, the AI's is a review flag with its evidence.
    assert result["invoice"]["number"] == "NATIVE-1"
    review = result["evidence"]["header"]["number"]["review"]
    assert review == {"reason": engines.AI_DISAGREEMENT, "other_value": "AI-1", "other_quote": "Invoice AI-1",
                      "other_page": 1}
    assert result["readers"]["header"]["number"] == "native"


def test_gap_fill_fills_only_empty_fields_and_marks_them_as_the_ais(monkeypatch, tmp_path):
    native = identityless_native_invoice()
    native["currency"] = None
    native["lines"][1]["tax_amount"] = None
    ai = identityless_native_invoice()
    ai.update(currency="AED", net="99.00")
    ai["lines"][1]["tax_amount"] = "0.25"
    result, calls = run(monkeypatch, tmp_path, native, ai)
    assert calls.count("ai") == 1 and result["readers"]["ai"]["status"] == "gap_fill"
    assert result["invoice"]["currency"] == "AED" and result["readers"]["header"]["currency"] == "ai"
    assert result["invoice"]["lines"][1]["tax_amount"] == "0.25"
    assert result["readers"]["lines"][1]["tax_amount"] == "ai"
    # An evidenced engine value is never silently overwritten.
    assert result["invoice"]["net"] == "15.00" and result["readers"]["header"]["net"] == "native"
    assert result["evidence"]["header"]["net"]["review"]["other_value"] == "99.00"


def test_fallback_when_the_engines_fail_the_basic_checks(monkeypatch, tmp_path):
    native = {"number": None, "net": None, "lines": []}
    ai = {**complete_native_invoice("AI-1"), "date": "2026-01-15"}
    result, calls = run(monkeypatch, tmp_path, native, ai)
    assert calls == ["invoice2data", "paddleocr", "docling", "ai"]
    assert result["readers"]["ai"]["status"] == "fallback"
    assert result["invoice"]["number"] == "AI-1" and result["readers"]["header"]["number"] == "ai"
    assert len(result["invoice"]["lines"]) == 2 and result["readers"]["lines"][0]["qty"] == "ai"


def test_fallback_keeps_local_lines_when_counts_match_and_flags_disagreements():
    engine = {"number": "N-1", "net": None, "lines": [{"sku": "A", "qty": "2", "price": "5.00"}]}
    ai = {"number": "N-1", "net": "10.00", "lines": [{"sku": "A", "qty": "3", "price": "5.00", "uom": "PCE"}]}
    merged, evidence, header, lines, notes = engines.merge_ai_fields(engine, ai, {}, {}, "ocr", "fallback")
    assert merged["net"] == "10.00" and header == {"number": "ocr", "net": "ai"}
    assert merged["lines"][0]["qty"] == "2" and merged["lines"][0]["uom"] == "PCE"
    assert lines == [{"sku": "ocr", "qty": "ocr", "price": "ocr", "uom": "ai"}]
    assert evidence["lines"][0]["qty"]["review"]["other_value"] == "3" and not notes


def test_misaligned_lines_are_never_filled_and_a_different_line_count_keeps_the_local_lines():
    engine = {"lines": [{"sku": "A", "qty": "2", "price": "5.00"}]}
    ai = {"lines": [{"sku": "B", "qty": "7", "price": "1.00", "uom": "PCE"}]}
    merged, _, _, lines, notes = engines.merge_ai_fields(engine, ai, {}, {}, "native", "gap_fill")
    assert "uom" not in merged["lines"][0] and lines[0] == {"sku": "native", "qty": "native", "price": "native"}
    assert notes == ["Line 1: the AI line does not align with the local line; nothing filled"]
    merged, _, _, _, notes = engines.merge_ai_fields(engine, {"lines": ai["lines"] * 2}, {}, {}, "native", "gap_fill")
    assert merged["lines"] == engine["lines"] and "the local lines are kept" in notes[0]


@pytest.mark.parametrize("ai_fallback,model,status", [(False, "stub-model", "off"), (True, "", "unavailable")])
def test_ai_off_or_unconnected_leaves_the_engine_result_and_the_gaps(monkeypatch, tmp_path, ai_fallback, model, status):
    native = identityless_native_invoice()
    native["currency"] = None
    result, calls = run(monkeypatch, tmp_path, native, complete_native_invoice("AI-1"), ai_fallback, model)
    engines_only, _ = run(monkeypatch, tmp_path, native, complete_native_invoice("AI-1"), False, "")
    assert "ai" not in calls and result["readers"]["ai"]["status"] == status and result["readers"]["ai"]["calls"] == 0
    assert result["invoice"] == engines_only["invoice"] and result["evidence"] == engines_only["evidence"]
    assert result["selected_engine"] == engines_only["selected_engine"] and "currency" in result["readers"]["gaps"]


def test_a_429_after_the_bounded_retry_keeps_the_engine_result(monkeypatch, tmp_path):
    native = identityless_native_invoice()
    native["currency"] = None
    engines_only, _ = run(monkeypatch, tmp_path, native, {}, False)
    result, calls = run(monkeypatch, tmp_path, native, {}, error=RuntimeError("Vertex AI returned HTTP 429"))
    assert calls.count("ai") == 1
    assert result["invoice"] == engines_only["invoice"] and result["evidence"] == engines_only["evidence"]
    assert result["readers"]["ai"] == {"status": "failed", "reason": "Vertex AI returned HTTP 429", "calls": 1}
    assert result["trace"][-1]["status"] == "failed" and result["trace"][-1]["role"] == "gap_fill"
    assert "currency" in result["readers"]["gaps"]
