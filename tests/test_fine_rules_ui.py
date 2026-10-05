"""The website panel for the fine-grained rules: every element the script uses exists and is served."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def fine_rules_script():
    source = (STATIC / "app.js").read_text()
    start = source.index("const FINE_RULES_STATUS")
    return source[start:source.index("async function exportBatch()")]


def test_panel_elements_exist_and_are_wired():
    html = (STATIC / "index.html").read_text()
    ids = set(re.findall(r'\$\("#([\w-]+)"\)', fine_rules_script())) | {"fine-rules-review", "fine-rules-target"}
    assert ids >= {"fine-rules-dialog", "fine-rules-config-note", "fine-rules-target-reason"}
    for element in ids | {"batch-fine-rules"}:
        assert f'id="{element}"' in html, element
    source = (STATIC / "app.js").read_text()
    assert '$("#batch-fine-rules").addEventListener("click", openFineRules)' in source
    assert '$("#batch-fine-rules").disabled' in source


def test_panel_calls_only_the_fine_rules_endpoints():
    script = fine_rules_script()
    assert set(re.findall(r'"/api/[\w/.-]+"', script)) == {'"/api/fine-rules/config"', '"/api/fine-rules/run"'}
    assert "`/api/fine-rules/${kind}.xlsx`" in script
    # V-007 / V-010 stay visible until the governed lists exist; the target needs every invoice Approved.
    assert "V-007" in script and "V-010" in script
    assert 'result.status !== "Approved"' in script


def test_panel_is_served(tmp_path):
    from app.main import create_app

    client = TestClient(create_app(tmp_path / "data"))
    assert 'id="fine-rules-dialog"' in client.get("/").text
    assert "openFineRules" in client.get("/static/app.js").text
