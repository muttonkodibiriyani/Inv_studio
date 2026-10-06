"""A re-read that changes the lines drops the owner's line entries (keyed by line number) with a visible note;
header entries stay. Synthetic data only."""
import time

from app.models import Line
from tests.test_rules_wiring import H, PREFLIGHT, job, production  # noqa: F401
from tests.test_fine_rules_api import INVOICE


def _three_lines():
    return INVOICE.model_copy(update={"lines": [
        Line(sku="S1", description="Synthetic one", qty=1, price=10, net_amount=10),
        Line(sku="S2", description="Synthetic two", qty=2, price=10, net_amount=20),
        Line(sku="S3", description="Synthetic three", qty=4, price=10, net_amount=40)]}).model_dump(mode="json")


def _reread(app, client, monkeypatch, invoice, tmp_path):
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"0" * 10)
    stored = app.state.store.job("job-1")
    app.state.store.job("job-1", {**stored, "path": str(source), "size": 10, "selected_engine": "local"})

    def process(*_args, **_kwargs):
        return {"invoice": invoice, "text": stored["text"], "boxes": [], "evidence": [], "trace": [],
                "selected_engine": "local", "extraction_note": None}
    monkeypatch.setattr("app.main.process", process)
    token = client.post("/api/preflight", headers=H, json=PREFLIGHT).json()["token"]
    assert client.post("/api/preflight/confirm", headers=H, json={"token": token}).status_code == 200
    queued = client.post("/api/jobs/job-1/retry", headers=H,
                         json={"options": PREFLIGHT["options"], "preflight_token": token})
    assert queued.status_code == 200, queued.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        shown = client.get("/api/jobs/job-1").json()
        if shown["status"] not in ("queued", "processing"):
            return shown
        time.sleep(0.02)
    raise AssertionError("synthetic re-read did not finish")


def _typed_line_cells(view):
    return [line["line"] for line in view.get("lines") or [] for f in line["cells"].values()
            if any(e.get("kind") == "owner_entry" for e in (f.get("evidence") or []))]


def _with_entries(app, client):
    app.state.store.job("job-1", {**job(), "invoice": _three_lines()})
    shown = client.get("/api/jobs/job-1").json()
    entries = {"header": {"po": "13000009"}, "lines": {"2": {"Item": "345000009", "Quantity": "2"}}}
    saved = client.post("/api/jobs/job-1/review", headers=H,
                        json={"invoice": shown["invoice"], "revision": shown["revision"], "entries": entries})
    assert saved.status_code == 200, saved.text
    assert set(saved.json()["owner_entry_attribution"]) == {"header:po", "line:2:Item", "line:2:Quantity"}
    assert 2 in _typed_line_cells(saved.json()["rules"])
    return saved.json()


def test_a_reread_with_fewer_lines_clears_the_line_entries_and_says_so(production, monkeypatch, tmp_path):  # noqa: F811
    app, client = production
    saved = _with_entries(app, client)
    two = {**saved["invoice"], "lines": saved["invoice"]["lines"][:2]}

    shown = _reread(app, client, monkeypatch, two, tmp_path)
    assert shown["status"] == "review" and len(shown["invoice"]["lines"]) == 2
    assert shown["owner_entries"] == {"header": {"po": "13000009"}, "lines": {}}
    assert set(shown["owner_entry_attribution"]) == {"header:po"}
    assert shown["extraction_note"].startswith("2 line corrections were cleared because the invoice was read again")
    assert _typed_line_cells(shown["rules"]) == []
    audit = [e for e in client.get("/api/jobs/job-1/audit").json() if e["event"] == "fields_extracted"][-1]["payload"]
    assert audit["owner_line_entries_cleared"] == 2


def test_a_reread_with_the_same_lines_keeps_every_entry(production, monkeypatch, tmp_path):  # noqa: F811
    app, client = production
    saved = _with_entries(app, client)

    shown = _reread(app, client, monkeypatch, saved["invoice"], tmp_path)
    assert shown["owner_entries"]["lines"] == {"2": {"Item": "345000009", "Quantity": "2"}}
    assert "cleared" not in (shown.get("extraction_note") or "")
