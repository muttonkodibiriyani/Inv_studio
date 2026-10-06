"""A job saved before part_code existed (FT3) keeps its line entries on an unchanged save or re-read: the stored
lines are compared in the current model's shape (decision 65). Synthetic data only."""

from tests.test_retry_entries import _reread, _with_entries
from tests.test_rules_wiring import H, production  # noqa: F401  (pytest fixture)

ENTRIES = {"2": {"Item": "345000009", "Quantity": "2"}}


def _pre_ft3(app, client):
    """The saved job as an earlier release stored it: no part_code key on any line."""
    saved = _with_entries(app, client)
    stored = app.state.store.job("job-1")
    lines = [{k: v for k, v in line.items() if k != "part_code"} for line in stored["invoice"]["lines"]]
    app.state.store.job("job-1", {**stored, "invoice": {**stored["invoice"], "lines": lines}})
    assert all("part_code" not in line for line in app.state.store.job("job-1")["invoice"]["lines"])
    return saved


def _save(client, invoice):
    shown = client.get("/api/jobs/job-1").json()
    response = client.post("/api/jobs/job-1/review", headers=H, json={"invoice": invoice, "revision": shown["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def test_an_unchanged_save_of_a_pre_ft3_job_keeps_its_line_entries(production):  # noqa: F811
    app, client = production
    _pre_ft3(app, client)
    stored = app.state.store.job("job-1")["invoice"]
    for invoice in (stored, client.get("/api/jobs/job-1").json()["invoice"]):  # as stored, and as the screen sends it
        assert _save(client, invoice)["owner_entries"]["lines"] == ENTRIES


def test_a_changed_line_on_a_pre_ft3_job_still_clears_the_line_entries(production):  # noqa: F811
    app, client = production
    _pre_ft3(app, client)
    invoice = app.state.store.job("job-1")["invoice"]
    lines = [dict(line) for line in invoice["lines"]]
    lines[1]["qty"] = 3
    saved = _save(client, {**invoice, "lines": lines})
    assert saved["owner_entries"]["lines"] == {} and saved["owner_entries"]["header"] == {"po": "13000009"}


def test_a_reread_with_the_same_lines_keeps_a_pre_ft3_jobs_entries(production, monkeypatch, tmp_path):  # noqa: F811
    app, client = production
    saved = _pre_ft3(app, client)
    shown = _reread(app, client, monkeypatch, saved["invoice"], tmp_path)
    assert shown["owner_entries"]["lines"] == ENTRIES
    assert "cleared" not in (shown.get("extraction_note") or "")
