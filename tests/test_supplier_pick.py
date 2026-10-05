"""The owner's one-click supplier-code pick reaches the fine rules and survives only while it still applies.
Synthetic data only."""

from tests.test_rules_wiring import H, job, production  # noqa: F401

CANDIDATES = [{"supplier_code": "SYN1", "rule": "R-006", "reason": "2 of 2 codes serve entity E",
               "sites": [{"supplier_site": "22001", "site_name": "Synthetic Trading E", "entity": "E",
                          "reference": "22001|v1"}]},
              {"supplier_code": "SYN2", "rule": "R-006", "reason": "2 of 2 codes serve entity E",
               "sites": [{"supplier_site": "22002", "site_name": "Synthetic Goods E", "entity": "E",
                          "reference": "22002|v1"}]}]


def _rules_offering_candidates(monkeypatch):
    import app.main as main
    real, seen = main.run_batch, []

    def run_batch(entries, *args, **kwargs):
        seen.append(entries[0].get("owner_supplier_code"))
        results = real(entries, *args, **kwargs)
        if entries[0].get("owner_supplier_code") is None:
            results[0]["header"]["Supplier Site"] = ""
            results[0]["lineage"] = [x for x in results[0]["lineage"] if x["target"] != "Supplier Site"]
            results[0]["supplier_site_candidates"] = CANDIDATES
        return results
    monkeypatch.setattr("app.main.run_batch", run_batch)
    return seen


def test_candidates_reach_the_view_and_a_pick_is_passed_to_the_rules(production, monkeypatch):  # noqa: F811
    app, client = production
    seen = _rules_offering_candidates(monkeypatch)
    app.state.store.job("job-1", job())
    shown = client.get("/api/jobs/job-1").json()
    assert [c["supplier_code"] for c in shown["rules"]["supplier_site_candidates"]] == ["SYN1", "SYN2"]
    assert shown["rules"]["fields"]["site"]["value"] is None

    base = {"invoice": shown["invoice"], "revision": shown["revision"]}
    picked = client.post("/api/jobs/job-1/review", headers=H, json={**base, "supplier_code": "SYN2"}).json()
    assert seen[-1] == "SYN2" and picked["owner_supplier_code"] == "SYN2"
    assert "supplier_site_candidates" not in picked["rules"]

    # A later save without a pick keeps it; the job refresh re-runs the rules with it too.
    kept = client.post("/api/jobs/job-1/review", headers=H,
                       json={"invoice": picked["invoice"], "revision": picked["revision"]}).json()
    assert seen[-1] == "SYN2" and kept["owner_supplier_code"] == "SYN2"
    audit = [e for e in client.get("/api/jobs/job-1/audit").json() if e["event"] == "edited"][-1]["payload"]
    assert audit["owner_supplier_pick"] is True and "SYN2" not in str(audit.get("owner_entries"))

    again = client.post("/api/jobs/job-1/review", headers=H,
                        json={"invoice": kept["invoice"], "revision": kept["revision"], "supplier_code": "SYN2"})
    assert again.status_code == 200
    kept = again.json()

    # Clearing it brings the candidates back.
    cleared = client.post("/api/jobs/job-1/review", headers=H,
                          json={"invoice": kept["invoice"], "revision": kept["revision"], "supplier_code": ""}).json()
    assert seen[-1] is None and "owner_supplier_code" not in cleared
    assert len(cleared["rules"]["supplier_site_candidates"]) == 2


def test_a_code_the_rules_did_not_offer_is_refused(production, monkeypatch):  # noqa: F811
    app, client = production
    _rules_offering_candidates(monkeypatch)
    app.state.store.job("job-1", job())
    shown = client.get("/api/jobs/job-1").json()
    refused = client.post("/api/jobs/job-1/review", headers=H,
                          json={"invoice": shown["invoice"], "revision": shown["revision"], "supplier_code": "SYN9"})
    assert refused.status_code == 400 and "candidates" in refused.json()["detail"]
    assert client.get("/api/jobs/job-1").json()["revision"] == shown["revision"]


def test_a_changed_supplier_name_drops_the_earlier_pick(production, monkeypatch):  # noqa: F811
    app, client = production
    seen = _rules_offering_candidates(monkeypatch)
    app.state.store.job("job-1", job())
    shown = client.get("/api/jobs/job-1").json()
    picked = client.post("/api/jobs/job-1/review", headers=H,
                         json={"invoice": shown["invoice"], "revision": shown["revision"], "supplier_code": "SYN1"}).json()
    renamed = {**picked["invoice"], "supplier_name": "Another Synthetic Supplier"}
    after = client.post("/api/jobs/job-1/review", headers=H, json={"invoice": renamed, "revision": picked["revision"]}).json()
    assert seen[-1] is None and "owner_supplier_code" not in after
