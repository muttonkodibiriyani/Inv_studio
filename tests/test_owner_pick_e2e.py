"""Decision 41 end to end: RULES' owner pick (fine_rules) through the review route, view, target check and export.
All data is RULES' synthetic R-006 fixture; only the lookup source is swapped for its in-memory rows."""
import io

from openpyxl import load_workbook

from app import fine_rules as fr
from app.excel import rules_workbook
from tests import test_fine_rules as syn
from tests.test_rules_wiring import H, job, production  # noqa: F401

ITEMS = syn.ITEMS + [syn.item(p, b, v, site="92006", name="DEF002RB2SAR", ref=r)
                     for p, b, v, r in (("345000001", "ULT_0012345678905", "100001", 7),
                                        ("345000002", "ULT_0098765432109", "100002", 8))]
SITES = [{"supplier_site": s, "currency": c, "status": "Active", "supplier_code": code,
          "supplier_name": "ABC Trading LLC", "site_name": n}
         for s, c, code, n in (("22001", "KWD", "1", "ABC001RA1KWD"), ("92006", "SAR", "2", "DEF002RB2SAR"))]
CONFIG = {**syn.CONFIG, "supplier_sites": SITES,
          "location_master": {"38091": {"type": fr.STORE, "market": "Kuwait", "entity_currency": "RA1KWD"}}}


def _rules_on_synthetic_rows(monkeypatch):
    def run_batch(entries, _source, _config):
        return fr.run_batch(entries, fr.RowsSource(ITEMS, syn.POGRN), fr.RulesConfig.from_dict(CONFIG))
    monkeypatch.setattr("app.main.run_batch", run_batch)


def _cells(shown):
    return {(c["column"], c.get("line")): c for c in shown["rules"]["target_check"]["cells"] if c["sheet"] == "Header"}


def _save(client, shown, **body):
    return client.post("/api/jobs/job-1/review", headers=H,
                       json={"invoice": shown["invoice"], "revision": shown["revision"], **body})


PRINTED = ("TAX INVOICE  Invoice No: INV-1  Date: 15/01/2026\nBill To: Ulta Buyer Co   Deliver To: Store Name\n"
           "Net 70.000\nTax 0\nTotal 70")


def _store_job(app):
    app.state.store.job("job-1", {**job(PRINTED), "invoice": syn.invoice(po=None).model_dump(mode="json")})


def test_the_owner_picks_a_supplier_code_and_the_rules_cascade_is_scored_as_owner_assisted(production, monkeypatch):  # noqa: F811
    app, client = production
    _rules_on_synthetic_rows(monkeypatch)
    _store_job(app)
    shown = client.get("/api/jobs/job-1").json()
    assert [c["supplier_code"] for c in shown["rules"]["supplier_site_candidates"]] == ["1", "2"]
    before = _cells(shown)
    assert {before[(c, None)]["status"] for c in ("Supplier Site", "Order No", "Location")} == {"empty_flagged"}

    # A code the rules did not offer changes nothing.
    assert _save(client, shown, supplier_code="9").status_code == 400
    assert client.get("/api/jobs/job-1").json()["rules"]["target_check"] == shown["rules"]["target_check"]

    picked = _save(client, shown, supplier_code="1").json()
    fields = picked["rules"]["fields"]
    assert (fields["site"]["value"], fields["po"]["value"], fields["location"]["value"]) == ("22001", "13000001", "38091")
    assert fields["site"]["evidence"][0]["kind"] == "owner_entry" and fields["site"]["evidence"][0]["rule"] == "OWNER-PICK"
    assert fields["po"]["evidence"][0]["assisted_by"] == "OWNER-PICK"
    assert fields["location"]["evidence"][0]["assisted_by"] == "OWNER-PICK"
    assert "assisted_by" not in str(shown["rules"]["fields"])

    after = _cells(picked)
    site = after[("Supplier Site", None)]
    assert (site["status"], site["sub"]) == ("verified", "owner_entry")
    # No cell is verified on the strength of the pick except the picked site itself, as an owner entry.
    newly = {k for k, c in after.items() if c["status"] == "verified" and before[k]["status"] != "verified"}
    for k in newly - {("Supplier Site", None)}:
        assert after[k]["evidence"]["kind"] not in ("owner_entry", "") and after[k]["evidence"]["rule"] != "OWNER-PICK", k
    assert [k for k, c in after.items() if c["evidence"]["rule"] == "OWNER-PICK"] == [("Supplier Site", None)]
    # The candidates stay for the picker; the export reads the header only.
    assert [c["supplier_code"] for c in picked["rules"]["supplier_site_candidates"]] == ["1", "2"]
    content, _ = rules_workbook([picked["rules"]])
    values = [str(c.value) for ws in load_workbook(io.BytesIO(content)) for row in ws.iter_rows() for c in row]
    assert "22001" in values
    assert not {"92006", "DEF002RB2SAR"} & set(values)
