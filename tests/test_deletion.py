import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Invoice


MUTATION = {"X-Studio-Request": "1"}


@pytest.fixture
def deletion_client(tmp_path, monkeypatch):
    monkeypatch.setenv("INV_STUDIO_DATA", str(tmp_path / "module-default"))
    app = create_app(tmp_path / "data")
    with TestClient(app) as client:
        yield client, app.state.store


def _seed_job(store, job_id, *, status="review", revision=3, **extra):
    path = store.root / "uploads" / f"{job_id}.pdf"
    path.write_bytes(("synthetic source " + job_id).encode())
    job = {
        "id": job_id,
        "filename": f"{job_id}.pdf",
        "size": path.stat().st_size,
        "path": str(path),
        "sha256": "a" * 64,
        "status": status,
        "revision": revision,
        "invoice": Invoice(number="SENSITIVE-SYNTHETIC").model_dump(mode="json"),
        "reviewed": False,
        "text": "SENSITIVE OCR TEXT",
        "boxes": [{"text": "SENSITIVE BOX"}],
        **extra,
    }
    store.job(job_id, job)
    store.audit(
        "reviewed",
        {
            "job_id": job_id,
            "before": {"number": "SENSITIVE-SYNTHETIC"},
            "after": job["invoice"],
        },
    )
    return job, path


def _delete(client, *jobs, confirm=True):
    return client.post(
        "/api/jobs/delete",
        headers=MUTATION,
        json={
            "jobs": [{"id": job["id"], "revision": job["revision"]} for job in jobs],
            "confirm_permanent": confirm,
        },
    )


def test_delete_requires_confirmation_and_revision_then_erases_live_invoice_data(
    deletion_client,
):
    client, store = deletion_client
    job, path = _seed_job(store, "delete-review")
    store.audit(
        "processing_plan_confirmed",
        {"signature": "synthetic", "files": [{"name": job["filename"], "size": job["size"]}]},
    )

    unprotected = client.post(
        "/api/jobs/delete",
        json={
            "jobs": [{"id": job["id"], "revision": job["revision"]}],
            "confirm_permanent": True,
        },
    )
    assert unprotected.status_code == 403
    assert _delete(client, job, confirm=False).status_code == 400
    invalid_confirmation = client.post(
        "/api/jobs/delete",
        headers=MUTATION,
        json={
            "jobs": [{"id": job["id"], "revision": job["revision"]}],
            "confirm_permanent": "true",
        },
    )
    assert invalid_confirmation.status_code == 422
    stale = {**job, "revision": job["revision"] - 1}
    response = _delete(client, stale)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVISION_CHANGED"
    assert store.job(job["id"]) is not None
    assert path.exists()

    response = _delete(client, job)

    assert response.status_code == 200
    body = response.json()
    assert body["deleted_ids"] == [job["id"]]
    assert body["count"] == 1
    assert body["ledger_tombstones"] == 0
    assert body["retained"] == {"deletion_audit": 1, "ledger_receipts": 0}
    assert "provider backups" in body["backup_notice"].lower()
    assert store.job(job["id"]) is None
    assert not path.exists()
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/document").status_code == 404
    with store.connection() as connection:
        tombstone = connection.execute(
            "SELECT payload FROM deleted_invoices WHERE job_id=?", (job["id"],)
        ).fetchone()
        audits = connection.execute("SELECT event,payload FROM audit ORDER BY id").fetchall()
    assert json.loads(tombstone[0]) == {"job_id": job["id"], "deleted": True}
    assert [row["event"] for row in audits] == ["invoice_deleted"]
    retained = " ".join(row["payload"] for row in audits) + tombstone[0]
    assert "SENSITIVE" not in retained
    assert job["filename"] not in retained


def test_active_selection_rejects_the_whole_delete_without_removing_other_jobs(
    deletion_client,
):
    client, store = deletion_client
    review, review_path = _seed_job(store, "delete-review-atomic")
    active, active_path = _seed_job(store, "delete-active", status="processing")

    response = _delete(client, review, active)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "INVOICE_ACTIVE",
        "message": "Wait for every selected invoice to finish processing before deleting it.",
        "job_ids": [active["id"]],
    }
    assert store.job(review["id"]) is not None
    assert store.job(active["id"]) is not None
    assert review_path.exists() and active_path.exists()


def test_delete_redacts_only_selected_file_from_mixed_preflight_audit(deletion_client):
    client, store = deletion_client
    selected, _ = _seed_job(store, "delete-mixed-selected")
    retained, retained_path = _seed_job(store, "delete-mixed-retained")
    store.audit(
        "processing_plan_confirmed",
        {
            "signature": "synthetic-mixed",
            "files": [
                {"name": selected["filename"], "size": selected["size"]},
                {"name": retained["filename"], "size": retained["size"]},
            ],
        },
    )

    response = _delete(client, selected)

    assert response.status_code == 200
    assert store.job(retained["id"]) is not None
    assert retained_path.exists()
    with store.connection() as connection:
        audits = connection.execute("SELECT event,payload FROM audit ORDER BY id").fetchall()
    plan = next(
        json.loads(row["payload"])
        for row in audits
        if row["event"] == "processing_plan_confirmed"
    )
    assert plan["files"] == [
        {"name": retained["filename"], "size": retained["size"]}
    ]
    serialized = " ".join(row["payload"] for row in audits)
    assert selected["filename"] not in serialized
    assert retained["filename"] in serialized


def test_individual_export_is_removed_but_minimal_receipt_ledger_is_retained(
    deletion_client,
):
    client, store = deletion_client
    export_id = "export-delete-one"
    job, path = _seed_job(
        store,
        "delete-exported",
        status="exported",
        revision=5,
        export_id=export_id,
    )
    receipt = {
        "id": export_id,
        "job_id": job["id"],
        "invoice_key": "SYNTHETIC|BUYER|NUMBER",
        "number": "SENSITIVE-SYNTHETIC",
        "policy": {"sensitive": "removed"},
        "allocations": [{"key": "PO-SYNTHETIC|1", "qty": "2.500"}],
    }
    with store.connection(True) as connection:
        connection.execute(
            "INSERT INTO exports VALUES (?,?,?,?,?)",
            (export_id, receipt["invoice_key"], job["id"], json.dumps(receipt), b"SENSITIVE XLSX"),
        )

    response = _delete(client, job)

    assert response.status_code == 200
    assert response.json()["ledger_tombstones"] == 1
    assert response.json()["deleted_exports"] == 1
    assert not path.exists()
    assert client.get(f"/api/exports/{export_id}").status_code == 404
    with store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM exports WHERE id=?", (export_id,)
        ).fetchone() is None
        ledger = store.ledger(connection)
        tombstone = connection.execute(
            "SELECT payload FROM deleted_invoices WHERE job_id=?", (job["id"],)
        ).fetchone()[0]
    assert ledger == [
        {
            "job_id": job["id"],
            "deleted": True,
            "invoice_key": receipt["invoice_key"],
            "allocations": [{"key": "PO-SYNTHETIC|1", "qty": "2.500"}],
        }
    ]
    assert "SENSITIVE-SYNTHETIC" not in tombstone
    assert "policy" not in tombstone
    assert "XLSX" not in tombstone


def test_partial_exported_batch_is_rejected_and_full_batch_removes_every_copy(
    deletion_client,
):
    client, store = deletion_client
    batch_id = "batch-delete-all"
    first, first_path = _seed_job(
        store,
        "delete-batch-a",
        status="exported",
        revision=6,
        export_id="export-batch-a",
        batch_id=batch_id,
    )
    second, second_path = _seed_job(
        store,
        "delete-batch-b",
        status="exported",
        revision=7,
        export_id="export-batch-b",
        batch_id=batch_id,
    )
    receipts = []
    with store.connection(True) as connection:
        for index, job in enumerate((first, second), 1):
            receipt = {
                "id": job["export_id"],
                "job_id": job["id"],
                "batch_id": batch_id,
                "invoice_key": f"SYNTHETIC|BUYER|{index}",
                "number": f"SENSITIVE-{index}",
                "allocations": [{"key": f"PO|{index}", "qty": str(index)}],
            }
            receipts.append(receipt)
            connection.execute(
                "INSERT INTO exports VALUES (?,?,?,?,?)",
                (
                    receipt["id"],
                    receipt["invoice_key"],
                    receipt["job_id"],
                    json.dumps(receipt),
                    b"SENSITIVE SHARED BATCH",
                ),
            )
        connection.execute(
            "INSERT INTO batches VALUES (?,?,?)",
            (batch_id, json.dumps(receipts), b"SENSITIVE SHARED BATCH"),
        )

    partial = _delete(client, first)

    assert partial.status_code == 409
    assert partial.json()["detail"] == {
        "code": "PARTIAL_EXPORTED_BATCH",
        "message": "Select every invoice in the exported batch to delete it.",
        "batch_id": batch_id,
        "required_job_ids": sorted([first["id"], second["id"]]),
    }
    assert first_path.exists() and second_path.exists()
    assert store.job(first["id"]) is not None
    assert store.job(second["id"]) is not None

    complete = _delete(client, first, second)

    assert complete.status_code == 200
    assert complete.json()["deleted_batches"] == 1
    assert complete.json()["deleted_exports"] == 2
    assert not first_path.exists() and not second_path.exists()
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        ledger = store.ledger(connection)
    assert len(ledger) == 2
    assert all(receipt["deleted"] is True for receipt in ledger)


def _seed_export(store, job, export_id, invoice_key, allocations):
    receipt = {"id": export_id, "job_id": job["id"], "invoice_key": invoice_key, "allocations": allocations}
    with store.connection(True) as connection:
        connection.execute(
            "INSERT INTO exports VALUES (?,?,?,?,?)",
            (export_id, invoice_key, job["id"], json.dumps(receipt), b"SYNTHETIC XLSX"),
        )


def test_exported_invoice_whose_key_was_deleted_before_merges_into_the_old_tombstone(deletion_client):
    # 2026-10-08: the tombstone INSERT hit deleted_invoices_invoice_key_key and every delete answered 500.
    client, store = deletion_client
    key = "rules|SYNTHETIC-SITE|SYN-1"
    first, _ = _seed_job(store, "delete-first", status="exported", revision=5, export_id="export-first")
    _seed_export(store, first, "export-first", key, [{"key": "PO-SYN|1", "qty": "1"}])
    assert _delete(client, first).status_code == 200
    again, path = _seed_job(store, "delete-again", status="exported", revision=5, export_id="export-again")
    _seed_export(store, again, "export-again", key, [{"key": "PO-SYN|1", "qty": "2"}])

    response = _delete(client, again)

    assert response.status_code == 200, response.text
    assert response.json()["source_files_not_removed"] == 0
    assert not path.exists()
    with store.connection() as connection:
        ledger = store.ledger(connection)
        own = json.loads(connection.execute(
            "SELECT payload FROM deleted_invoices WHERE job_id=? AND invoice_key IS NULL", (again["id"],)
        ).fetchone()[0])
    # One ledger entry for the key, carrying both exports' allocations for the receipt control.
    assert ledger == [{"job_id": first["id"], "deleted": True, "invoice_key": key,
                       "allocations": [{"key": "PO-SYN|1", "qty": "1"}, {"key": "PO-SYN|1", "qty": "2"}]}]
    assert own == {"job_id": again["id"], "deleted": True, "invoice_key_kept_by": first["id"]}


def test_a_failed_delete_keeps_every_source_file(deletion_client, monkeypatch):
    # The source objects used to go before the database writes, so a failed delete lost them all.
    client, store = deletion_client
    one, one_path = _seed_job(store, "delete-keep-1")
    two, two_path = _seed_job(store, "delete-keep-2")
    import app.deletion as deletion

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(deletion, "_remove_invoice_audit", fail)
    with pytest.raises(RuntimeError):
        _delete(client, one, two)

    assert one_path.exists() and two_path.exists()
    assert store.job(one["id"]) is not None and store.job(two["id"]) is not None
    assert client.get(f"/api/jobs/{one['id']}/document").status_code == 200


def test_retry_after_a_partial_delete_treats_missing_source_files_as_removed(deletion_client):
    client, store = deletion_client
    job, path = _seed_job(store, "delete-retry")
    path.unlink()
    # The invoice opens with a readable 404 for its document, not a 500.
    assert client.get(f"/api/jobs/{job['id']}/document").status_code == 404

    response = _delete(client, job)

    assert response.status_code == 200, response.text
    assert response.json()["source_files_not_removed"] == 0
    assert store.job(job["id"]) is None

