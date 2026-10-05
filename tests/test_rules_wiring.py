"""Review fields, banner and download from ONE fine-rules result with evidence. Synthetic data only."""

import io
import json
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import matching
from app.excel import HEADERS, rules_workbook
from app.matching import extraction_evidence, printed_evidence, rules_validation, rules_view
from tests.test_fine_rules_api import CONFIG, INVOICE, imported

H = {"x-studio-request": "1"}
# The target check re-reads printed evidence: the synthetic text prints what the invoice model holds.
PRINTED = "Invoice INV-API dated 15/01/2026\nLine 1 qty 3 unit 10\nLine 2 qty 2 unit 20\nTotal before tax 70.00\nVAT 0.00"


def lineage(target, value, source, reference="", line=None):
    return {"target": target, "line": line, "original": value, "value": value, "rule": "R-T", "source": source,
            "reference": reference, "confidence": "Exact"}


def approved_result(**extra):
    header = {"Document": "INV-1", "Supplier Site": "22001", "Order No": "13000001", "Location": "38091",
              "Location Type": "Store (S)", "Document Date": "2026-01-15", "Currency": "KWD", "Market": "Kuwait",
              "Net Amount": "30", "Tax Amount": "0", "Validation Status": "Approved"}
    lines = [{"Item": "345000001", "UPC": "0012345678905", "Unit Cost": "10", "Quantity": "3",
              "Unit Tax Code": "VAT0", "Validation Status": "Matched", "Source Row": "line 1"}]
    trace = [lineage("Document", "INV-1", "Invoice Document"),
             lineage("Supplier Site", "22001", "Item Master SUPPLIER", "Item Master!2"),
             lineage("Order No", "13000001", "POGRN RMS_ORDER_NO", "POGRN!2"),
             lineage("Location", "38091", "Accepted POGRN LOCATION", "POGRN!2"),
             lineage("Location Type", "Store (S)", "location master", "38091|v1"),
             lineage("Document Date", "2026-01-15", "Invoice Document Date"),
             lineage("Currency", "KWD", "Supplier sites", "22001|v1"),
             lineage("Market", "Kuwait", "Location-market mapping", "38091|v1"),
             lineage("Net Amount", "30", "Invoice totals", "page 1"),
             lineage("Tax Amount", "0", "Invoice totals", "page 1"),
             lineage("Item", "345000001", "Item Master ITEM_PARENT", "Item Master!2", 1),
             lineage("UPC", "0012345678905", "Invoice barcode", "", 1),
             lineage("Unit Cost", "10", "Invoice line", "", 1), lineage("Quantity", "3", "Invoice line", "", 1),
             lineage("Unit Tax Code", "VAT0", "Owner VAT code table", "KWT|v1", 1)]
    return {"status": "Approved", "header": header, "lines": lines, "lineage": trace, "exceptions": [], **extra}


def test_every_shown_value_carries_its_evidence():
    view = rules_view(approved_result())
    site = view["fields"]["site"]
    assert site["value"] == "22001" and site["evidence"][0]["kind"] == "sheet"
    assert site["evidence"][0]["reference"] == "Item Master!2"
    assert view["fields"]["market"]["evidence"][0]["kind"] == "table"
    assert view["fields"]["number"]["evidence"][0]["kind"] == "printed"
    assert view["fields"]["taxCode"]["value"] == "VAT0" and view["fields"]["taxCode"]["evidence"]
    for f in view["fields"].values():
        assert (f["value"] is None) == f["flagged"] and (f["value"] is None or f["evidence"])


def test_value_without_evidence_stays_empty_and_is_flagged_never_guessed():
    result = approved_result()
    result["lineage"] = [x for x in result["lineage"] if x["target"] != "Location"]
    view = rules_view(result)
    assert view["fields"]["location"] == {"label": "Delivery location", "target": "Location", "value": None,
                                          "evidence": [], "flagged": True, "reason": "No evidence"}
    # Not in the owner's target and in no owner sheet: not offered at all, so never filled.
    assert not {"seller", "buyer", "origin"} & set(view["fields"])
    blocking = {i["message"] for i in view["issues"] if i["blocking"]}
    assert any(m.startswith("Delivery location has no evidence and is left empty") for m in blocking)
    assert rules_validation(view, reviewed=True)["ready"] is False


def test_printed_amount_evidence_only_when_found_on_the_document():
    result = approved_result()
    result["lineage"] = [x for x in result["lineage"] if x["target"] not in ("Net Amount", "Tax Amount")]
    boxes = [{"page": 2, "text": "Net 30.00", "box": [1, 2, 3, 4]}, {"page": 2, "text": "VAT 0.00", "box": [1, 9, 3, 4]}]
    view = rules_view(result, "", boxes)
    assert view["fields"]["net"]["evidence"][0]["reference"] == "page 2 box [1, 2, 3, 4]"
    assert view["fields"]["tax"]["value"] == "0"
    missing = rules_view(result, "nothing printed here", [])
    assert missing["fields"]["net"]["value"] is None and missing["fields"]["net"]["flagged"]
    # A longer number is not a match for the amount.
    assert printed_evidence("30", "Total 130.00") is None


def test_item_line_rate_below_95_goes_to_owner_review():
    result = approved_result()
    result["lines"] = result["lines"] * 20
    result["lineage"] += [lineage(c, v, "s", "Item Master!2" if c == "Item" else "", n) for n in range(2, 21)
                          for c, v in (("Item", "345000001"), ("Unit Cost", "10"), ("Quantity", "3"),
                                       ("Unit Tax Code", "VAT0"))]
    assert rules_view(result)["item_lines"]["owner_review"] is False
    result["lines"] = [*result["lines"][:19], {**result["lines"][0], "Item": ""}]
    view = rules_view(result)
    assert view["item_lines"]["resolved"] == 19 and view["item_lines"]["rate"] == "0.9500"
    result["lines"] = [*result["lines"][:18], *[{**result["lines"][0], "Item": ""}] * 2]
    view = rules_view(result)
    assert view["item_lines"]["owner_review"] is True
    assert any(i["code"] == "Owner Review" and i["blocking"] for i in view["issues"])
    # RULES' own numbers win when present.
    view = rules_view(approved_result(item_resolution={"resolved": 1, "total": 2, "rate": "0.5", "below": True,
                                                       "definition": "one ITEM_PARENT agreeing with POGRN"}))
    assert view["item_lines"]["resolved"] == 1 and view["item_lines"]["owner_review"] is True
    assert view["item_lines"]["definition"] == "one ITEM_PARENT agreeing with POGRN"


def test_banner_uses_rules_exceptions_verbatim():
    result = approved_result(status="Review", exceptions=[{
        "Exception Type": "Missing/Ambiguous PO", "Description": "d", "Owner": "Buyer", "Line No.": "",
        "Rule ID": "ALG-008", "Candidates / Evidence": "", "blocking": True}])
    issues = rules_validation(rules_view(result))["issues"]
    assert issues[0] == {"code": "Missing/Ambiguous PO", "message": "d", "owner": "Buyer", "line": None,
                         "rule": "ALG-008", "evidence": "", "blocking": True, "check": "", "type": "", "level": "review"}
    # The reviewer's confirm accepts it: still shown, no longer holding the invoice.
    confirmed = rules_validation(rules_view(result), reviewed=True)
    assert confirmed["issues"][0]["accepted"] is True and confirmed["issues"][0]["blocking"] is False
    assert confirmed["ready"] is True and confirmed["accepted"] == [{"rule": "ALG-008", "code": "Missing/Ambiguous PO", "line": None}]


def test_download_matches_the_owner_target_format():
    content, evidence = rules_workbook([rules_view(approved_result())], upc="barcode")
    wb = load_workbook(io.BytesIO(content))
    assert wb.sheetnames == ["Header", "Tax_Breakdown", "Details"]
    header = wb["Header"]
    assert [c.value for c in header[1]] == HEADERS["Header"] and len(HEADERS["Header"]) == 13
    assert [header.cell(2, c).value for c in range(1, 7)] == [1, "INV-1", 22001, 13000001, 38091, "Store (S)"]
    assert header["G2"].value == datetime(2026, 1, 15) and header["G2"].number_format == "m/d/yyyy"
    assert header["H2"].value == 30 and header["I2"].value == 0
    assert all(header.cell(2, c).value is None for c in range(10, 14))
    tax = wb["Tax_Breakdown"]
    assert [c.value for c in tax[2]] == [1, "VAT0", 30] and tax.max_row == 2
    details = wb["Details"]
    assert [c.value for c in details[2]] == [1, 345000001, "0012345678905", 10, 3, "VAT0"]  # upc="barcode"
    assert details["D2"].number_format == "General"
    assert evidence[1]["Header!C2"][0]["reference"] == "Item Master!2"
    assert evidence[1]["Details!B2"][0]["reference"] == "Item Master!2"
    no_upc, _ = rules_workbook([rules_view(approved_result())])
    assert load_workbook(io.BytesIO(no_upc))["Details"]["C2"].value is None


def test_download_refuses_unapproved_or_unevidenced_values():
    with pytest.raises(ValueError, match="approved"):
        rules_workbook([rules_view(approved_result(status="Review"))])
    result = approved_result()
    result["lineage"] = [x for x in result["lineage"] if x["target"] != "Order No"]
    with pytest.raises(ValueError, match="Purchase order has no evidenced value"):
        rules_workbook([rules_view(result)])


def job(text=PRINTED):
    return {"id": "job-1", "status": "review", "reviewed": False, "revision": 1, "filename": "synthetic.pdf",
            "invoice": INVOICE.model_dump(mode="json"), "text": text, "boxes": [], "provenance": []}


@pytest.fixture
def production(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_DATA", str(tmp_path / "module-default"))
    monkeypatch.delenv("INV_STUDIO_DEMO_REFERENCES", raising=False)
    from app.main import create_app

    def never(*_, **__):
        raise AssertionError("demo validation references used in production")
    monkeypatch.setattr("app.main.enrich", never)
    monkeypatch.setattr("app.main.validate", never)
    imported(tmp_path, monkeypatch)
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        assert client.post("/api/fine-rules/config", json=CONFIG, headers=H).status_code == 200
        yield app, client


def test_review_banner_and_download_come_from_one_rules_result(production):
    app, client = production
    app.state.store.job("job-1", job())
    shown = client.get("/api/jobs/job-1").json()
    rules = shown["rules"]
    assert rules["status"] == "Approved" and rules["revision"] == shown["revision"]
    assert rules["fields"]["site"]["value"] == "22001"
    assert rules["fields"]["site"]["evidence"][0]["reference"].startswith("Item Master!2")
    assert rules["fields"]["net"]["evidence"][0]["kind"] == "printed"
    assert rules["item_lines"] == {**rules["item_lines"], "resolved": 2, "total": 2, "owner_review": False}
    assert shown["validation"]["source"] == "fine_rules"
    assert [i["code"] for i in shown["validation"]["issues"] if i["blocking"]] == ["REVIEW"]
    # A rules 'Approved' alone never makes the job approved: export stays held until a person confirms review.
    assert shown["status"] == "review" and shown["validation"]["ready"] is False
    held = client.post("/api/jobs/job-1/export", headers=H, json={"revision": shown["revision"]})
    assert held.status_code == 409
    # The invoice the reviewer edits is not filled from derived values.
    assert shown["invoice"]["site"] is None
    reviewed = client.post("/api/jobs/job-1/review", headers=H,
                           json={"invoice": shown["invoice"], "revision": shown["revision"], "confirm": True}).json()
    assert reviewed["validation"]["ready"] is True and reviewed["rules"]["revision"] == reviewed["revision"]
    applied = [e for e in client.get("/api/jobs/job-1/audit").json() if e["event"] == "exported"]
    assert applied == []
    exported = client.post("/api/jobs/job-1/export", headers=H, json={"revision": reviewed["revision"]}).json()
    wb = load_workbook(io.BytesIO(client.get(exported["url"]).content))
    assert wb["Header"]["C2"].value == int(reviewed["rules"]["fields"]["site"]["value"])
    assert [wb["Details"].cell(r, 2).value for r in (2, 3)] == [345000001, 345000002]
    evidence = client.get(f"/api/exports/{exported['id']}/evidence").json()["cells"]
    assert evidence["Header!C2"][0]["reference"].startswith("Item Master!2")
    audit = [e for e in client.get("/api/jobs/job-1/audit").json() if e["event"] == "exported"][0]["payload"]
    assert "evidence" not in audit and audit["evidence_cells"] == len(evidence)


def test_empty_not_guessed_blocks_export(production):
    app, client = production
    app.state.store.job("job-1", job(text="no totals printed"))
    shown = client.get("/api/jobs/job-1").json()
    assert shown["rules"]["fields"]["net"] == {**shown["rules"]["fields"]["net"], "value": None, "flagged": True}
    reviewed = client.post("/api/jobs/job-1/review", headers=H,
                           json={"invoice": shown["invoice"], "revision": shown["revision"], "confirm": True}).json()
    assert reviewed["validation"]["ready"] is False
    assert "Net total not found" not in str(reviewed["validation"]["issues"])
    assert any(i["rule"] == "EVIDENCE" and i["blocking"] for i in reviewed["validation"]["issues"])
    assert client.post("/api/jobs/job-1/export", headers=H, json={"revision": reviewed["revision"]}).status_code == 409


def test_rules_rerun_when_mapping_tables_change(production):
    app, client = production
    app.state.store.job("job-1", job())
    first = client.get("/api/jobs/job-1").json()["rules"]
    assert client.post("/api/fine-rules/config", json={}, headers=H).status_code == 200
    second = client.get("/api/jobs/job-1").json()["rules"]
    assert second["signature"] != first["signature"] and second["status"] != "Approved"


def test_demo_references_are_test_only(production, monkeypatch, tmp_path):
    _, client = production
    assert client.post("/api/references/demo", headers=H).status_code == 404
    assert client.get("/api/state").json()["references"] is None
    monkeypatch.setenv("INV_STUDIO_DEMO_REFERENCES", "1")
    monkeypatch.setenv("INV_STUDIO_CLOUD", "1")
    from app.main import create_app
    with pytest.raises(RuntimeError, match="test-only"):
        create_app(tmp_path / "cloud")


def test_legacy_demo_validation_is_still_available_to_tests():
    assert matching.validate and matching.enrich and Decimal("0.95") == matching.ITEM_THRESHOLD


def test_extraction_evidence_backs_printed_fields_and_disagreement_is_a_warning():
    result={"status":"Approved","header":{"Document":"SYN-1"},"lines":[],"lineage":[],"exceptions":[]}
    printed={"header":{"number":{"quote":"Invoice SYN-1","page":1,"box":[0.1,0.1,0.3,0.12],"source":"ai",
                                 "review":{"reason":"OCR read a different number","other_value":"SYN-7"}}},
             "lines":[{"qty":{"quote":"2","page":1,"source":"ocr"}}]}
    view=rules_view(result,printed=printed)
    number=view["fields"]["number"]
    assert number["value"]=="SYN-1" and not number["flagged"]
    assert number["evidence"][0]["kind"]=="printed" and number["evidence"][0]["reference"]=="page 1 box [0.1, 0.1, 0.3, 0.12]"
    warn=[i for i in view["issues"] if i["rule"]=="EVID-OCR"]
    assert len(warn)==1 and warn[0]["blocking"] is False and "SYN-1" not in warn[0]["message"]
    # Without extraction evidence the same value stays empty and flagged.
    assert rules_view(result)["fields"]["number"]["value"] is None


def test_owner_rule_and_printed_evidence_kinds_and_buyer_name():
    result={"status":"Approved","header":{"Document":"SYN-2","Buyer Name":"Synthetic Buyer"},"lines":[],"exceptions":[],
            "lineage":[{"target":"Buyer Name","line":None,"original":"","value":"Synthetic Buyer","rule":"BUYER-NAME",
                        "source":"Owner rule","reference":"owner rule BUYER-NAME (2026-10-05)","confidence":"Owner rule",
                        "evidence_kind":"owner_rule"},
                       {"target":"Document","line":None,"original":"Invoice SYN-2","value":"SYN-2","rule":"",
                        "source":"Invoice","reference":"page 1","confidence":"Exact","evidence_kind":"printed"}]}
    view=rules_view(result)
    buyer=view["fields"]["buyer_name"]
    assert buyer["value"]=="Synthetic Buyer" and not buyer["flagged"] and buyer["evidence"][0]["kind"]=="owner_rule"
    assert view["fields"]["number"]["evidence"][0]["kind"]=="printed"
    assert not any("Buyer" in i["message"] for i in view["issues"])
    result["header"]["Buyer Name"]=""
    assert "buyer_name" not in rules_view(result)["fields"]


def test_owner_review_is_not_doubled_when_rules_raises_it():
    result={"status":"Review","header":{},"lines":[],"lineage":[],
            "item_resolution":{"resolved":1,"total":2,"rate":"0.5","threshold":"0.95","below":True,"definition":"d"},
            "exceptions":[{"Exception Type":"Owner Validation","Description":"Item-line check 1/2 is below 95 %","Rule ID":"ITEM-LINE-95","blocking":True}]}
    view=rules_view(result)
    assert view["item_lines"]["owner_review"] is True
    assert [i["rule"] for i in view["issues"] if i["rule"] in ("ITEM-95","ITEM-LINE-95")]==["ITEM-LINE-95"]


def test_failure_status_check_and_po_candidates_reach_the_view():
    result={"status":"Review","header":{},"lines":[],"lineage":[],"po_candidates":3,
            "exceptions":[{"Exception Type":"Missing/Ambiguous PO","Engine Type":"Ambiguous PO","Check ID":"C-12",
                           "Description":"3 orders","Rule ID":"POG-001","blocking":True}]}
    view=rules_view(result)
    issue=next(i for i in view["issues"] if i["rule"]=="POG-001")
    assert (issue["code"],issue["type"],issue["check"])==("Missing/Ambiguous PO","Ambiguous PO","C-12")
    assert view["po_candidates"]==3


def test_rules_review_job_is_ready_after_the_owners_confirm_and_exports(production, monkeypatch):
    app, client = production
    import app.main as main
    real = main.run_batch

    def below_95(*args, **kwargs):
        results = real(*args, **kwargs)
        results[0]["status"] = "Review"
        results[0]["exceptions"].append({"Exception Type": "Owner Validation", "Engine Type": "Owner Validation",
                                         "Rule ID": "ITEM-LINE-95", "Check ID": "C-11", "Line No.": "",
                                         "Description": "Item-line check 1/2 is below 95 %; owner validates", "blocking": True})
        return results
    monkeypatch.setattr("app.main.run_batch", below_95)
    app.state.store.job("job-1", job())
    shown = client.get("/api/jobs/job-1").json()
    assert shown["rules"]["status"] == "Review" and shown["validation"]["ready"] is False
    assert client.post("/api/jobs/job-1/export", headers=H, json={"revision": shown["revision"]}).status_code == 409
    confirmed = client.post("/api/jobs/job-1/review", headers=H,
                            json={"invoice": shown["invoice"], "revision": shown["revision"], "confirm": True}).json()
    assert confirmed["status"] == "ready" and confirmed["validation"]["ready"] is True
    assert confirmed["validation"]["accepted"] == [{"rule": "ITEM-LINE-95", "code": "Owner Validation", "line": None}]
    exported = client.post("/api/jobs/job-1/export", headers=H, json={"revision": confirmed["revision"]})
    assert exported.status_code == 200
    receipt = [e for e in client.get("/api/jobs/job-1/audit").json() if e["event"] == "exported"][0]["payload"]
    assert receipt["rules_status"] == "Review" and receipt["owner_accepted"][0]["rule"] == "ITEM-LINE-95"


def test_reviewer_entry_fills_an_empty_required_cell_and_is_its_evidence(production):
    app, client = production
    app.state.store.job("job-1", job(text="no totals printed"))
    shown = client.get("/api/jobs/job-1").json()
    base = {"invoice": shown["invoice"], "revision": shown["revision"], "confirm": True}
    assert client.post("/api/jobs/job-1/review", headers=H, json={**base, "entries": {"header": {"site": "abc"}}}).status_code == 400
    assert client.post("/api/jobs/job-1/review", headers=H, json={**base, "entries": {"header": {"seller": "1"}}}).status_code == 400
    confirmed = client.post("/api/jobs/job-1/review", headers=H,
                            json={**base, "entries": {"header": {"net": "70.00", "tax": "0.00"}}}).json()
    net = confirmed["rules"]["fields"]["net"]
    assert net["value"] == "70.00" and net["evidence"][0]["kind"] == "owner_entry"
    assert confirmed["validation"]["ready"] is True
    exported = client.post("/api/jobs/job-1/export", headers=H, json={"revision": confirmed["revision"]}).json()
    cells = client.get(f"/api/exports/{exported['id']}/evidence").json()["cells"]
    assert cells["Header!H2"][0]["kind"] == "owner_entry"


def test_a_confirm_is_asked_again_when_the_rules_issues_change(production):
    app, client = production
    app.state.store.job("job-1", job())
    shown = client.get("/api/jobs/job-1").json()
    confirmed = client.post("/api/jobs/job-1/review", headers=H,
                            json={"invoice": shown["invoice"], "revision": shown["revision"], "confirm": True}).json()
    assert confirmed["validation"]["ready"] is True
    assert client.post("/api/fine-rules/config", json={}, headers=H).status_code == 200
    again = client.get("/api/jobs/job-1").json()
    assert again["reviewed"] is False and again["validation"]["ready"] is False


def test_upc_defaults_to_empty_per_the_owner(production):
    _, client = production
    assert client.get("/api/target-export/config").json()["upc"] == "empty"
    assert client.post("/api/target-export/config", json={"upc": "barcode"}, headers=H).status_code == 200
    assert client.get("/api/target-export/config").json()["upc"] == "barcode"


def test_deferred_extraction_evidence_refreshes_the_rules_view_without_a_revision(production):
    app, client = production
    app.state.store.job("job-1", job())
    first = client.get("/api/jobs/job-1").json()
    # A deferred OCR pass adds evidence with boxes; status and revision stay the same.
    later = {**app.state.store.job("job-1"), "evidence": {"header": {"number": {"quote": "Invoice SYN", "page": None, "source": "ocr"}}, "lines": []}}
    app.state.store.job("job-1", later)
    second = client.get("/api/jobs/job-1").json()
    assert second["revision"] == first["revision"]
    assert second["rules"]["evidence_hash"] != first["rules"]["evidence_hash"]
    assert client.get("/api/jobs/job-1").json()["rules"]["computed_at"] == second["rules"]["computed_at"]


def test_extraction_evidence_without_a_page_is_not_given_a_page():
    assert extraction_evidence({"quote": "SYN", "page": None, "source": "native"})["reference"] == "page not given"
    # An entry placed on a page but not on a text-layer row has no box; a line with nothing located is {}.
    assert extraction_evidence({"quote": "SYN", "page": 2, "source": "ai"})["reference"] == "page 2"
    result = {"status": "Review", "header": {"Document": "SYN-3"}, "lines": [{"line": 1}, {"line": 2}], "lineage": [], "exceptions": []}
    view = rules_view(result, printed={"header": {}, "lines": [{}, {"qty": {"quote": "2", "page": 1, "source": "ocr"}}]})
    assert len(view["lines"]) == 2


def test_an_order_selected_by_the_rules_is_shown_as_picked_not_printed():
    result = {"status": "Review", "header": {"Document": "SYN-4", "Order No": "70002"}, "lines": [], "exceptions": [],
              "lineage": [{"target": "Order No", "line": None, "original": "", "value": "70002", "rule": "POG-001",
                           "source": "Selected by POG-001 among 2 order/location candidates; not printed on the invoice",
                           "reference": "POGRN!12", "confidence": "Derived from POGRN", "evidence_kind": "selected"}]}
    po = rules_view(result)["fields"]["po"]
    assert po["value"] == "70002" and po["evidence"][0]["kind"] == "selected"


def test_config_audit_records_field_names_and_lengths_never_values(production):
    app, client = production
    secret = "Synthetic Buyer Holding Co"
    assert client.post("/api/fine-rules/config", json={**CONFIG, "buyer_name": secret, "version": "syn-v9", "value_decimals": 2},
                       headers=H).status_code == 200
    with app.state.store.connection() as c:
        audits = [json.loads(r[0]) for r in c.execute("SELECT payload FROM audit WHERE event='fine_rules_config_changed'")]
    assert audits[-1]["buyer_name"] == len(secret) and audits[-1]["version"] == len("syn-v9") and audits[-1]["value_decimals"] == "int"
    assert secret not in json.dumps(audits) and "syn-v9" not in json.dumps(audits)
