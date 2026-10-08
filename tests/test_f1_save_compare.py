"""F1 (D132): a review save keeps the owner's line entries unless a line really changed. The stored lines are compared
with the posted ones as Line values (numbers by value, text through text_key), not as JSON text. Synthetic data only."""
from tests.test_retry_entries import _typed_line_cells, _with_entries
from tests.test_rules_wiring import H, production  # noqa: F401

FIELDS = ("item_id", "sku", "gtin", "part_code", "description", "qty", "uom", "price", "net_amount", "tax_amount",
          "evidence", "page")


def _as_client_sends(lines):
    """The review client: every field as trimmed input text, '' as null, a newline shown as a space (F3), and no
    barcode_unchecked."""
    def cell(value):
        text = " ".join(str(value).split()) if value is not None else ""
        return text or None
    return [{f: cell(line.get(f)) for f in FIELDS} for line in lines]


def _stored_with_text_and_decimals(app):
    stored = app.state.store.job("job-1")
    lines = stored["invoice"]["lines"]
    lines[0] = {**lines[0], "description": "Synthetic\none", "qty": "1.00", "price": "10.00", "net_amount": "10.00"}
    lines[1] = {**lines[1], "description": "  Synthetic two ", "uom": ""}
    app.state.store.job("job-1", stored)
    return app.state.store.job("job-1")


def _save(client, lines):
    shown = client.get("/api/jobs/job-1").json()
    saved = client.post("/api/jobs/job-1/review", headers=H,
                        json={"invoice": {**shown["invoice"], "lines": lines}, "revision": shown["revision"]})
    assert saved.status_code == 200, saved.text
    return saved.json()


def _cleared(client):
    return [e for e in client.get("/api/jobs/job-1/audit").json() if e["event"] == "edited"][-1]["payload"][
        "owner_line_entries_cleared"]


def test_F1_a_no_edit_client_save_with_entries_omitted_keeps_the_line_entries(production):  # noqa: F811
    app, client = production
    _with_entries(app, client)
    stored = _stored_with_text_and_decimals(app)

    saved = _save(client, _as_client_sends(stored["invoice"]["lines"]))
    assert saved["owner_entries"]["lines"] == {"2": {"Item": "345000009", "Quantity": "2"}}
    assert 2 in _typed_line_cells(saved["rules"])
    assert _cleared(client) == 0


def test_F1_a_stored_1_00_and_a_sent_1_are_the_same_number(production):  # noqa: F811
    app, client = production
    _with_entries(app, client)
    stored = _stored_with_text_and_decimals(app)
    sent = _as_client_sends(stored["invoice"]["lines"])
    sent[0] = {**sent[0], "qty": "1", "price": "10", "net_amount": "10.0"}

    saved = _save(client, sent)
    assert saved["owner_entries"]["lines"] == {"2": {"Item": "345000009", "Quantity": "2"}}


def test_F1_a_real_line_change_still_clears_the_line_entries(production):  # noqa: F811
    app, client = production
    for change in ({"qty": "3"}, {"sku": "S9"}, {"description": "Synthetic other"}):
        _with_entries(app, client)
        stored = _stored_with_text_and_decimals(app)
        sent = _as_client_sends(stored["invoice"]["lines"])
        sent[2] = {**sent[2], **change}
        saved = _save(client, sent)
        assert saved["owner_entries"]["lines"] == {}, change
        assert _typed_line_cells(saved["rules"]) == []
        assert _cleared(client) == 2


def test_F1_a_removed_line_clears_the_line_entries(production):  # noqa: F811
    app, client = production
    saved = _with_entries(app, client)
    assert _save(client, _as_client_sends(saved["invoice"]["lines"][:2]))["owner_entries"]["lines"] == {}
