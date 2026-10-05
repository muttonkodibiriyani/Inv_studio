from fastapi.testclient import TestClient

from app.main import create_app
from app.matching import validate
from app.models import Invoice, Policy, extraction_schema


MUTATION = {"X-Studio-Request": "1"}


def test_ambiguous_printed_date_is_separate_from_canonical_provider_field():
    invoice = Invoice(date_printed="03-08-2026")

    assert invoice.date_printed == "03-08-2026"
    assert invoice.date is None
    assert Invoice.model_validate(invoice.model_dump(mode="json")) == invoice
    schema = extraction_schema()
    assert schema["properties"]["date_printed"] == {"type": ["string", "null"]}
    assert "date_printed" in schema["required"]


def test_printed_date_alone_never_satisfies_canonical_date_validation():
    invoice = Invoice(date_printed="03-08-2026")

    result = validate(invoice, {}, Policy(), reviewed=True)

    assert invoice.date is None
    assert {issue["code"] for issue in result["issues"]} >= {"MISSING", "DATE"}
    assert any(
        issue["code"] == "MISSING" and issue["message"] == "Missing date"
        for issue in result["issues"]
    )


def test_review_api_preserves_printed_date_until_reviewer_supplies_iso(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_DATA", str(tmp_path / "module-default"))
    app = create_app(tmp_path / "data")
    invoice = Invoice(number="SYN-DATE", date_printed="03-08-2026")
    job = {
        "id": "printed-date-review",
        "filename": "synthetic-date.pdf",
        "size": 20,
        "path": str(tmp_path / "synthetic-date.pdf"),
        "sha256": "a" * 64,
        "status": "review",
        "revision": 1,
        "reviewed": False,
        "invoice": invoice.model_dump(mode="json"),
        "provenance": [],
    }
    app.state.store.job(job["id"], job)

    with TestClient(app) as client:
        first = client.post(
            f"/api/jobs/{job['id']}/review",
            headers=MUTATION,
            json={"invoice": job["invoice"], "revision": 1, "confirm": True},
        )
        assert first.status_code == 200, first.text
        held = first.json()
        assert held["invoice"]["date_printed"] == "03-08-2026"
        assert held["invoice"]["date"] is None
        # The banner comes from the fine rules; key on the rule id, not the Failure Status text.
        assert "ALG-004" in {issue.get("rule") for issue in held["validation"]["issues"]}
        assert held["validation"]["ready"] is False

        reviewed_invoice = held["invoice"]
        reviewed_invoice["date"] = "2026-08-03"
        second = client.post(
            f"/api/jobs/{job['id']}/review",
            headers=MUTATION,
            json={
                "invoice": reviewed_invoice,
                "revision": held["revision"],
                "confirm": True,
            },
        )
        assert second.status_code == 200, second.text
        confirmed = second.json()
        assert confirmed["invoice"]["date_printed"] == "03-08-2026"
        assert confirmed["invoice"]["date"] == "2026-08-03"
        assert "DATE" not in {issue["code"] for issue in confirmed["validation"]["issues"]}
    app.state.pool.shutdown(wait=True)
