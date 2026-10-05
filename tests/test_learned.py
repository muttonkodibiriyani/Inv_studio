"""Learning from verified answers: synthetic layouts only, no real invoice content."""
import json

import pytest
import yaml

from app import learned as L
from app.learned import LearnedStore, apply_template, finish_row, learn_template
from app.ocr_worker import apply_learned, template_extract, templates_from

# A made-up two-page layout: serial, description (wrapped), brand, qty, uom, price, amount, rate, vat, gross.
TEXT_A = """TAX INVOICE
# ACME26-00000042
Date of Issuing: March 3, 2026
ACME SUPPLIES FZCO
Qty Unit Price Amount VAT VAT Total
%
AED AED AED AED
1 WIDGET BLUE Brandy 12.000 Pcs 10.00 120.00 5 6.00 126.00
large, 1234567890128 2021
2 GADGET RED Brandy 3.000 Pcs 20.00 60.00 5 3.00 63.00
WIDGETCODE,
2234567890125
Made with Acme Accounting
3 THING GREEN Brandy 1.000 Pcs 5.50 5.50 5 0.28 5.78
3234567890122 - D
TOTAL OF SUPPLY: One hundred eighty five and 50/100 AED
Sub Total, AED: 185.50
Total VAT, AED: 9.28
Total, AED: 194.78
Made with Acme Accounting
"""
INVOICE_A = {
    "number": "ACME26-00000042", "date": "2026-03-03", "currency": "AED", "net": "185.50", "tax": "9.28",
    "supplier_name": "ACME SUPPLIES FZCO",
    "lines": [
        {"description": "WIDGET BLUE large", "gtin": "1234567890128", "qty": "12.000", "uom": "Pcs",
         "price": "10.00", "net_amount": "120.00", "tax_amount": "6.00"},
        {"description": "GADGET RED WIDGETCODE", "gtin": "2234567890125", "qty": "3.000", "uom": "Pcs",
         "price": "20.00", "net_amount": "60.00", "tax_amount": "3.00"},
        {"description": "THING GREEN - D", "gtin": "3234567890122", "qty": "1.000", "uom": "Pcs",
         "price": "5.50", "net_amount": "5.50", "tax_amount": "0.28"},
    ],
}
# A second invoice from the same supplier with different values: the held-out check.
TEXT_B = """TAX INVOICE
# ACME26-00000077
Date of Issuing: April 9, 2026
ACME SUPPLIES FZCO
Qty Unit Price Amount VAT VAT Total
%
AED AED AED AED
1 SPROCKET GOLD Brandy 2.000 Pcs 1,250.00 2,500.00 5 125.00 2,625.00
XL, 4234567890129 2021
2 BOLT Brandy 10.000 Pcs 1.00 10.00 5 0.50 10.50
BOLTCODE,
5234567890126
TOTAL OF SUPPLY: Two thousand five hundred ten AED
Sub Total, AED: 2,510.00
Total VAT, AED: 125.50
Total, AED: 2,635.50
Made with Acme Accounting
"""
INVOICE_B = {
    "number": "ACME26-00000077", "date": "2026-04-09", "currency": "AED", "net": "2510.00", "tax": "125.50",
    "supplier_name": "ACME SUPPLIES FZCO",
    "lines": [
        {"description": "SPROCKET GOLD XL", "gtin": "4234567890129", "qty": "2.000", "uom": "Pcs",
         "price": "1250.00", "net_amount": "2500.00", "tax_amount": "125.00"},
        {"description": "BOLT BOLTCODE", "gtin": "5234567890126", "qty": "10.000", "uom": "Pcs",
         "price": "1.00", "net_amount": "10.00", "tax_amount": "0.50"},
    ],
}


def lines_of(invoice):
    return [{k: str(line.get(k) or "") for k in L.LINE_FIELDS} for line in invoice["lines"]]


def test_learned_template_reproduces_its_source_and_reads_a_sibling_invoice():
    template, report = learn_template(TEXT_A, INVOICE_A, "job-a")
    assert template is not None, report
    assert report["fidelity"]["ok"] and report["fidelity"]["exact"] == 3
    assert template["keywords"] == ["ACME SUPPLIES FZCO"]
    assert template["learned"]["source_id"] == "job-a" and template["learned"]["gtin_tail"] == "text"
    assert template["fields"]["lines"]["rules"][0]["skip_line"] == ["^Made\\s+with\\s+Acme\\s+Accounting$"]
    # The same template reads the sibling invoice without having seen it.
    read = apply_template(TEXT_B, template)
    assert read["number"] == "ACME26-00000077" and read["date"] == "2026-04-09" and read["currency"] == "AED"
    assert read["supplier_name"] == "ACME SUPPLIES FZCO"
    assert L._dec(read["net"]) == L._dec("2510.00") and L._dec(read["tax"]) == L._dec("125.50")
    assert lines_of(read) == lines_of(INVOICE_B)
    assert "learned" not in read


def test_learned_template_is_valid_invoice2data_yaml_and_goes_through_the_worker(tmp_path):
    template, _ = learn_template(TEXT_A, INVOICE_A, "job-a")
    folder = tmp_path / "learned" / "templates"
    folder.mkdir(parents=True)
    (folder / "acme.yml").write_text(yaml.safe_dump(template, sort_keys=False))
    loaded = templates_from([folder, tmp_path / "missing"])
    assert len(loaded) == 1
    parsed, meta = template_extract(TEXT_B, loaded, with_meta=True)
    assert meta["supplier_key"] == "acme-supplies-fzco"
    assert lines_of(parsed) == lines_of(INVOICE_B)
    assert template_extract("unrelated text", loaded) is None


def test_learning_fails_honestly_when_the_text_does_not_support_the_answer():
    wrong = json.loads(json.dumps(INVOICE_A))
    wrong["lines"][1]["qty"] = "4.000"
    template, report = learn_template(TEXT_A, wrong, "job-a")
    assert template is None and "not found" in report["reason"]
    template, report = learn_template("no table here", INVOICE_A, "job-a")
    assert template is None
    template, report = learn_template(TEXT_A, {**INVOICE_A, "lines": []}, "job-a")
    assert template is None and report["reason"] == "verified invoice has no lines"


def test_finish_row_cleans_wrapped_descriptions_codes_and_numbers():
    row = {"description": "WIDGET BLUE\nlarge\n", "gtin": "\n\n1234567890128", "tail": "\n 2021\n",
           "qty": "12.000", "price": "1,250.00", "net_amount": "15,000.00", "tax_amount": "750.00", "uom": "Pcs"}
    assert finish_row(row, {"gtin_tail": "text"}) == {
        "description": "WIDGET BLUE large", "gtin": "1234567890128", "qty": "12.000", "price": "1250.00",
        "net_amount": "15000.00", "tax_amount": "750.00", "uom": "Pcs"}
    assert finish_row({"name": "x", "tail": " - D", "gtin": "1"}, {"gtin_tail": "text"})["description"] == "x - D"
    assert finish_row({"name": "x", "tail": " - D"}, {"gtin_tail": "none"})["description"] == "x"
    assert finish_row({"sku": "SYNTH02-0 SRK-"}, {"sku_join": "dash"})["sku"] == "SRK-SYNTH02-0"
    assert finish_row({"sku": "SRK- SYNTH02-0"}, {"sku_join": "dash"})["sku"] == "SRK-SYNTH02-0"
    assert finish_row({"sku": "AB CD"}, {"sku_join": "ba"})["sku"] == "CDAB"
    assert finish_row({"name": "x"}, {"constants": {"uom": "PCE"}})["uom"] == "PCE"


def learned_templates_for(template, tmp_path):
    folder = tmp_path / "templates"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "acme.yml").write_text(yaml.safe_dump(template, sort_keys=False))
    return templates_from([folder])


def test_apply_learned_stays_behind_the_built_in_readers(tmp_path):
    template, _ = learn_template(TEXT_A, INVOICE_A, "job-a")
    learned = learned_templates_for(template, tmp_path)
    # No match: untouched, no note.
    assert apply_learned("other", learned, {"lines": [{"qty": "1"}]}, "layout") == ({"lines": [{"qty": "1"}]}, "layout", None)
    # The built-in reading has no lines: the learned template reads alone.
    invoice, method, note = apply_learned(TEXT_B, learned, {"number": "x", "lines": []}, "text_only")
    assert method == "learned_template" and note["mode"] == "read" and lines_of(invoice) == lines_of(INVOICE_B)
    # Same arithmetic and nothing proven to fix: byte-identical baseline, no note.
    baseline = json.loads(json.dumps(INVOICE_B))
    baseline["lines"][0]["description"] = "SPROCKET GOLD XL, 4234567890129"
    assert apply_learned(TEXT_B, learned, baseline, "columnar_tax_invoice") == (baseline, "columnar_tax_invoice", None)
    # Same arithmetic with a proven fix: only that field is overlaid.
    fixed = json.loads(json.dumps(template))
    fixed["learned"]["fixes"] = ["description"]
    invoice, method, note = apply_learned(TEXT_B, learned_templates_for(fixed, tmp_path / "fixed"), baseline, "columnar_tax_invoice")
    assert method == "columnar_tax_invoice+learned" and note["mode"] == "overlay" and note["fields"] == ["description"]
    assert invoice["lines"][0]["description"] == "SPROCKET GOLD XL" and invoice["lines"][1] == baseline["lines"][1]
    # Different arithmetic: replace only when the learned reading reconciles and the built-in one does not.
    broken = {"net": "2510.00", "lines": [{"qty": "2", "price": "1250.00", "net_amount": "2500.00"}]}
    invoice, method, note = apply_learned(TEXT_B, learned, broken, "layout")
    assert method == "learned_template" and note["mode"] == "replace"
    fine = {"net": "2510.00", "lines": [{"qty": "1", "price": "2510.00", "net_amount": "2510.00"}]}
    assert apply_learned(TEXT_B, learned, fine, "layout") == (fine, "layout", None)


def test_store_learns_confirms_relearns_and_forgets(tmp_path):
    persisted, removed = [], []
    store = LearnedStore(tmp_path, persist=persisted.append, remove=removed.append)
    first = store.record_verified(INVOICE_A, TEXT_A, "job-a", baseline={"lines": []})
    assert first["template"] == "learned" and first["supplier_key"] == "acme-supplies-fzco"
    assert "rows" in first["fixes"] and "sku" not in first["fixes"]  # nothing is proven for a column never printed
    template_path = store.templates_dir / "acme-supplies-fzco.yml"
    supplier_path = store.root / "suppliers" / "acme-supplies-fzco.json"
    assert template_path.exists() and (template_path.stat().st_mode & 0o777) == 0o600
    assert template_path in persisted and supplier_path in persisted
    assert "ACME26-00000042" in supplier_path.read_text()  # the store holds real values: never in git
    second = store.record_verified(INVOICE_B, TEXT_B, "job-b")
    assert second["template"] == "confirmed" and second["examples"] == 2
    summary = store.summary()
    assert summary["suppliers"][0]["examples"] == 2 and summary["suppliers"][0]["confirmed"] == 1
    assert "ACME26" not in json.dumps(summary)
    # Examples go to the AI prompt only for this supplier's documents and never from the current document.
    block = store.prompt_examples(TEXT_B)
    assert "ACME26-00000042" in block and "never copy these" in block
    assert store.prompt_examples("another supplier entirely") == ""
    # Corrections approved by the owner are attributed to the supplier of the invoice they name.
    entry = {"Feedback ID": "FB-1", "Invoice / Line": "ACME26-00000077 / line 2", "Original Suggestion": "BOLT",
             "User Correction": "BOLT BOLTCODE", "Reason": "code belongs to the description"}
    record = store.promote_feedback(entry, [{"invoice": {"number": "ACME26-00000077", "supplier_name": "ACME SUPPLIES FZCO"}}])
    assert record["supplier_key"] == "acme-supplies-fzco"
    assert "corrected to 'BOLT BOLTCODE'" in store.prompt_examples(TEXT_B)
    assert store.promote_feedback({**entry, "Feedback ID": "FB-2", "Invoice / Line": "ZZZ"}, [])["supplier_key"] == "unassigned"
    assert store.retract_feedback("FB-2") == 1 and store.summary()["corrections"] == 1
    # Deleting the source invoice deletes its template; the remaining sample teaches a new one.
    assert store.forget_sources(["job-a"]) == ["acme-supplies-fzco"]
    assert template_path in removed and template_path.exists()
    relearned = yaml.safe_load(template_path.read_text())
    assert relearned["learned"]["source_id"] == "job-b" and "ACME26-00000042" not in supplier_path.read_text()
    assert store.forget_sources(["job-b"]) == ["acme-supplies-fzco"] and not template_path.exists()
    # Owner-approved corrections come from the feedback log, not from a job, so deleting jobs keeps them.
    assert store.summary()["suppliers"] == [] and store.summary()["corrections"] == 1
    assert store.forget("acme-supplies-fzco") is True and store.summary()["corrections"] == 0
    assert store.forget("acme-supplies-fzco") is False


def test_store_identifies_layouts_without_a_supplier_name(tmp_path):
    store = LearnedStore(tmp_path)
    nameless = {k: v for k, v in INVOICE_A.items() if k != "supplier_name"}
    outcome = store.record_verified(nameless, TEXT_A, "job-a")
    assert outcome["template"] == "learned" and outcome["supplier_key"] == "layout-aed-aed-aed"
    read = apply_template(TEXT_B, yaml.safe_load((store.templates_dir / "layout-aed-aed-aed.yml").read_text()))
    assert lines_of(read) == lines_of(INVOICE_B) and read.get("supplier_name") is None


def test_learning_can_be_switched_off_and_summary_is_public(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    monkeypatch.setenv("INV_STUDIO_LEARN", "0")
    with TestClient(create_app(tmp_path / "off")) as client:
        assert client.get("/api/learned").json()["enabled"] is False
        assert client.delete("/api/learned/x", headers={"X-Studio-Request": "1"}).status_code == 409
    monkeypatch.delenv("INV_STUDIO_LEARN")
    with TestClient(create_app(tmp_path / "on")) as client:
        assert client.get("/api/learned").json() == {"enabled": True, "suppliers": [], "corrections": 0, "unassigned_corrections": 0}
        assert client.delete("/api/learned/nobody", headers={"X-Studio-Request": "1"}).json() == {"forgotten": False}
        assert client.post("/api/learned/jobs/missing", headers={"X-Studio-Request": "1"}).status_code == 404
        assert (tmp_path / "on" / "learned" / "templates").is_dir()


@pytest.mark.parametrize("value,expected", [("12,000.50", "12000.50"), ("12.000", "12.000"), ("1,2", "1,2")])
def test_plain_number_strips_only_thousands_separators(value, expected):
    assert L._plain_number(value) == expected


# An OCR'd layout: no serial on some rows, a two-part code, and a labelled barcode printed under the name
# that OCR reading order puts after the name on one document and before it on the next.
TEXT_C = """TAX INVOICE
Invoice SYN-001 Ref 03-Mar-2026
No. Code Description  Unit  Qty  Price  Amount  Tax  Total
1  AB1234-0  ZZ-  WIDGET BLUE - WITH CASE  EAN: 4006381333931  EA  2.00  100.00  200.00  10.00  210.00
2  CD5678-0  ZZ-  GADGET RED  EAN: 4006381333948  EA  1.00  50.00  50.00  2.50  52.50
Total in AED: Two Hundred Sixty Two and Fils Fifty Only  Total  3.00  250.00  12.50  262.50
"""
INVOICE_C = {"number": "SYN-001", "date": "2026-03-03", "currency": "AED", "net": "250.00", "tax": "12.50",
             "lines": [{"sku": "ZZ-AB1234-0", "description": "WIDGET BLUE - WITH CASE", "qty": "2.00", "uom": "EA",
                        "price": "100.00", "net_amount": "200.00", "tax_amount": "10.00"},
                       {"sku": "ZZ-CD5678-0", "description": "GADGET RED", "qty": "1.00", "uom": "EA",
                        "price": "50.00", "net_amount": "50.00", "tax_amount": "2.50"}]}
TEXT_D = """TAX INVOICE
Invoice SYN-002 Ref 09-Apr-2026
No. Code Description  Unit  Qty  Price  Amount  Tax  Total
EF9012-0  ZZ-  EAN:4006381333955  THING GREEN - MASK  EA  3.00  10.00  30.00  1.50  31.50
Total in AED: Thirty One and Fils Fifty Only  Total  3.00  30.00  1.50  31.50
"""


def test_learned_scan_layout_tolerates_ocr_reading_order_of_the_barcode():
    template, report = learn_template(TEXT_C, INVOICE_C, "scan-c")
    assert template is not None, report
    assert template["learned"]["sku_join"] == "dash"
    read = apply_template(TEXT_D, template)
    assert read["number"] == "SYN-002" and read["date"] == "2026-04-09"
    assert L._dec(read["net"]) == L._dec("30.00") and L._dec(read["tax"]) == L._dec("1.50")
    assert lines_of(read) == [{"sku": "ZZ-EF9012-0", "gtin": "", "description": "THING GREEN - MASK", "qty": "3.00",
                               "uom": "EA", "price": "10.00", "net_amount": "30.00", "tax_amount": "1.50"}]


def test_header_anchor_needs_a_label_word_so_sibling_number_date_pairs_do_not_match():
    import re
    text = "ACME SUPPLIES FZCO\nInvoice No 12345 01.07.2026\nTotal 10.00\n"
    regex, extra, _ = L._header_spec(text, text.splitlines(), "date", "2026-07-01")
    assert extra == {"date_format": "%d.%m.%Y"}
    assert "No" in regex and not regex.startswith("-?")  # the label word, not the bare number before the date
    sibling = "ACME SUPPLIES FZCO\nInvoice No 12399 02.08.2026\nDelivery 777 30.09.2026\nTotal 10.00\n"
    assert re.findall(regex, sibling) == ["02.08.2026"]
    assert L._header_spec(text, text.splitlines(), "number", "12345")[0].startswith("Invoice")
    symbol = "# ACME26-1\nTotal 1.00\n"  # a symbol label such as '#' is a label, not a number
    assert L._header_spec(symbol, symbol.splitlines(), "number", "ACME26-1")[0].startswith("\\#")


def test_a_learned_header_regex_that_matches_twice_yields_no_value_instead_of_a_list(tmp_path):
    template, _ = learn_template(TEXT_A, INVOICE_A, "job-a")
    template["fields"]["date"] = {"parser": "regex", "regex": r"([A-Z][a-z]+ \d{1,2}, \d{4})", "type": "date"}
    template["fields"]["invoice_number"] = r"(?:#|No\.)\s*(ACME\S+)"
    template["learned"].pop("fallbacks", None)  # isolate the list handling from the fallback anchors
    loaded = learned_templates_for(template, tmp_path)
    twice = TEXT_B.replace("TAX INVOICE\n", "TAX INVOICE\nDelivery No. ACME26-00000077 of September 30, 2026\n", 1)
    parsed, meta = template_extract(twice, loaded, with_meta=True)
    assert meta["supplier_key"] == "acme-supplies-fzco"
    assert "date" not in parsed  # two different dates: ambiguous, so no value rather than a list
    assert parsed["number"] == INVOICE_B["number"]  # the same number twice is one value
    assert lines_of(parsed) == lines_of(INVOICE_B)


def test_learned_header_keeps_the_printed_decimals_of_amounts(tmp_path):
    template, _ = learn_template(TEXT_A, INVOICE_A, "job-a")
    assert template["learned"]["decimals"] == {"net": 2, "tax": 2}
    read = template_extract(TEXT_B, learned_templates_for(template, tmp_path))
    assert (read["net"], read["tax"]) == (INVOICE_B["net"], INVOICE_B["tax"])  # "2510.00", not "2510.0"


def test_header_fallback_anchors_read_a_sibling_when_the_first_anchor_is_wording_that_varies(tmp_path):
    terms = "Invoice Date Amount Order\nPayable within 30 days 03.03.2026 185.50 PO-123\n"
    training = TEXT_A.replace("ACME SUPPLIES FZCO\n", "ACME SUPPLIES FZCO\n" + terms, 1)
    template, report = learn_template(training, {**INVOICE_A, "po": "PO-123"}, "job-t")
    assert template is not None, report
    regex, extra, fallbacks = L._header_spec(training, training.splitlines(), "po", "PO-123")
    assert regex.startswith("days")  # the payment-terms wording came first: it finds PO-123 once here
    assert template["learned"]["fallbacks"]["po"] == fallbacks and len(fallbacks) == 1
    # Only the bare-number anchor is a fallback: "Order" on the line above would count six tokens into the
    # terms line, and on a sibling with different wording that count lands on some other token just as surely.
    assert fallbacks[0]["regex"].startswith("-?")
    sibling = TEXT_B.replace("ACME SUPPLIES FZCO\n",
                             "ACME SUPPLIES FZCO\nInvoice Date Amount Order\nPayable on receipt 09.04.2026 2,510.00 PO-456\n", 1)
    read = template_extract(sibling, learned_templates_for(template, tmp_path))
    assert (read["number"], read["date"], read["po"]) == (INVOICE_B["number"], INVOICE_B["date"], "PO-456")
    assert lines_of(read) == lines_of(INVOICE_B)
    # No fallback may guess: a sibling whose terms line prints no order at all yields no po.
    assert "po" not in template_extract(sibling.replace(" PO-456", ""), learned_templates_for(template, tmp_path))
