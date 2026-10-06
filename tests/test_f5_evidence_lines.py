"""F5 (D146(2), D153): the reader's line reviews are positional, so a save keeps them only while every line is the
stored line at the same index; header evidence always stays. Synthetic data only."""
from tests.test_f1_save_compare import _as_client_sends, _save
from tests.test_retry_entries import _three_lines
from tests.test_rules_wiring import job, production  # noqa: F401

REVIEW = {"review": {"reason": "SKU column present, cell empty", "code": "not_read"}}
HEADER = {"tax": {"review": {"reason": "no tax printed", "code": "not_printed"}}}


def _with_reviews(app):
    app.state.store.job("job-1", {**job(), "invoice": _three_lines(),
                                  "evidence": {"header": HEADER, "lines": [{}, {"sku": REVIEW}, {"gtin": REVIEW}]}})
    return app.state.store.job("job-1")["invoice"]["lines"]


def _line_reviews(saved):
    return sum(1 for row in saved["evidence"]["lines"] for cell in row.values() if cell.get("review"))


def test_F5_a_no_edit_save_keeps_the_line_reviews(production):  # noqa: F811
    app, client = production
    saved = _save(client, _as_client_sends(_with_reviews(app)))
    assert _line_reviews(saved) == 2 and len(saved["evidence"]["lines"]) == 3
    assert saved["evidence"]["header"] == HEADER


def test_F5_an_edit_that_keeps_every_line_in_place_keeps_the_line_reviews(production):  # noqa: F811
    app, client = production
    sent = _as_client_sends(_with_reviews(app))
    sent[1] = {**sent[1], "qty": "5", "item_id": "SYN-1"}
    assert _line_reviews(_save(client, sent)) == 2


def test_F5_deleting_a_line_drops_the_line_reviews_and_keeps_the_header(production):  # noqa: F811
    app, client = production
    sent = _as_client_sends(_with_reviews(app))
    saved = _save(client, [sent[0], sent[2]])
    assert _line_reviews(saved) == 0 and saved["evidence"]["header"] == HEADER
    assert client.get("/api/jobs/job-1").json()["evidence"]["lines"] == []


def test_F5_delete_one_and_add_one_with_the_same_count_drops_the_line_reviews(production):  # noqa: F811
    app, client = production
    sent = _as_client_sends(_with_reviews(app))
    added = {**sent[0], "sku": "S4", "description": "Synthetic four"}
    saved = _save(client, [sent[0], sent[2], added])
    assert len(saved["invoice"]["lines"]) == 3
    assert _line_reviews(saved) == 0 and saved["evidence"]["header"] == HEADER
