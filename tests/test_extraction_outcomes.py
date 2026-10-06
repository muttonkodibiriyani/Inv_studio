import json
import time
import pytest
from fastapi.testclient import TestClient
from app.models import Invoice


@pytest.mark.parametrize('invoice,text,event', [
    ({}, 'Readable source text, without invoice fields', 'text_read'),
    ({'number': 'TEST-INV'}, 'Invoice TEST-INV', 'fields_extracted'),
    ({}, '', 'extraction_failed'),
])
def test_audit_records_actual_extraction_outcome(tmp_path, monkeypatch, invoice, text, event):
    monkeypatch.setenv('INV_STUDIO_DATA',str(tmp_path / 'module-default'))
    from app import main
    def process(*args):
        return {'invoice': Invoice.model_validate(invoice).model_dump(mode='json'),
                'text': text, 'boxes': [], 'trace': [], 'selected_engine': 'invoice2data',
                'completeness': 0}
    monkeypatch.setattr(main, 'process', process)
    app = main.create_app(tmp_path)
    with TestClient(app) as client:
        headers = {'X-Studio-Request': '1'}
        options = {'engine': 'auto', 'ai_fallback': False}
        content = b'Synthetic invoice source'
        plan = client.post('/api/preflight', headers=headers, json={
            'options': options, 'files': [{'name': 'invoice.txt', 'size': len(content)}]}).json()
        client.post('/api/preflight/confirm', headers=headers, json={'token': plan['token']}).raise_for_status()
        response = client.post('/api/invoices', headers=headers, files={'file': ('invoice.txt', content)},
                               data={'options': json.dumps(options), 'preflight_token': plan['token']})
        response.raise_for_status()
        jid = response.json()['id']
        for _ in range(200):
            job = client.get('/api/jobs/' + jid).json()
            if job['status'] not in ('queued', 'processing'):
                break
            time.sleep(.01)
        assert job['extraction_status'] == event
        assert job['validation']['ready'] is False
        events = client.get('/api/jobs/' + jid + '/audit').json()
        assert all(entry['event'] != 'extracted' for entry in events)
        outcome = next(entry['payload'] for entry in events if entry['event'] == event)
        assert outcome['approved'] is False
        assert outcome['header_fields'] == (1 if invoice else 0)
        assert outcome['line_items'] == 0


def test_managed_vertex_default_does_not_replace_saved_model(tmp_path,monkeypatch):
    monkeypatch.setenv('INV_STUDIO_DATA',str(tmp_path/'module-default'))
    monkeypatch.setenv('VERTEX_PROJECT_ID','invoice-studio-12345')
    monkeypatch.setenv('VERTEX_MODEL','gemini-3.7-flash')
    from app import main
    app=main.create_app(tmp_path)
    with TestClient(app) as client:
        state=client.get('/api/state').json()
        assert state['connections']['vertex'] is True
        assert state['settings']['provider']=='vertex'
        assert state['settings']['model']=='gemini-3.7-flash'
        chosen={'provider':'anthropic','model':'chosen-by-operator','ai_fallback':False}
        client.post('/api/settings',headers={'X-Studio-Request':'1'},json=chosen).raise_for_status()
        assert client.get('/api/state').json()['settings']==chosen
        assert app.state.store.get('settings')==chosen


def test_ai_only_scan_gets_a_deferred_local_evidence_pass_that_never_changes_values(tmp_path, monkeypatch):
    monkeypatch.setenv('INV_STUDIO_DATA',str(tmp_path / 'module-default'))
    from app import main
    invoice = {'number': 'INV-10045', 'net': '10', 'lines': [{'qty': '2', 'price': '5'}]}
    def process(*args):
        return {'invoice': Invoice.model_validate(invoice).model_dump(mode='json'), 'text': '', 'boxes': [],
                'trace': [{'engine': 'vertex', 'status': 'extracted'}], 'selected_engine': 'vertex / gemini-test',
                'completeness': 1, 'evidence': {'header': {'number': {'quote': 'INV-10045', 'page': 1, 'source': 'ai'}},
                                                'lines': [{}]}}
    monkeypatch.setattr(main, 'process', process)
    monkeypatch.setattr(main, 'needs_scan_evidence', lambda result, opts: True)
    boxes = [{'text': 'INV-10045', 'page': 1, 'box': [100, 50, 160, 60], 'size': [600, 800]}]
    passes = []
    def verify_scan(path, root, language):
        passes.append(path); return boxes, 'INV-10045'
    monkeypatch.setattr(main, 'verify_scan', verify_scan)
    app = main.create_app(tmp_path)
    with TestClient(app) as client:
        headers = {'X-Studio-Request': '1'}
        options = {'engine': 'auto', 'ai_fallback': False}
        from reportlab.pdfgen import canvas
        page = canvas.Canvas(str(tmp_path / 'scan-10046.pdf')); page.drawString(72, 720, 'scan'); page.showPage(); page.save()
        content = (tmp_path / 'scan-10046.pdf').read_bytes()
        plan = client.post('/api/preflight', headers=headers, json={
            'options': options, 'files': [{'name': 'scan-10046.pdf', 'size': len(content)}]}).json()
        client.post('/api/preflight/confirm', headers=headers, json={'token': plan['token']}).raise_for_status()
        response = client.post('/api/invoices', headers=headers, files={'file': ('scan-10046.pdf', content)},
                               data={'options': json.dumps(options), 'preflight_token': plan['token']})
        response.raise_for_status()
        jid = response.json()['id']
        for _ in range(300):
            job = client.get('/api/jobs/' + jid).json()
            if any(entry.get('status') == 'evidence' for entry in job.get('trace') or []):
                break
            time.sleep(.01)
        assert job['status'] == 'review' and len(passes) == 1
        assert job['invoice']['number'] == 'INV-10045' and job['revision'] == 2  # the evidence pass bumps nothing
        number = job['evidence']['header']['number']
        assert number['quote'] == 'INV-10045' and number['source'] == 'ai' and number['box'][0] == round(100 / 600, 4)
        assert number['review']['other_value'] == '10046'
        assert job['trace'][-1]['reason'].startswith('Local OCR text layer read for evidence only')
