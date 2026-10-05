from types import SimpleNamespace
from app import engines


def setup(monkeypatch,tmp_path,text):
    calls=[]
    monkeypatch.setattr(engines,'capabilities',lambda:[{'id':x,'installed':True} for x in ('invoice2data','paddleocr','docling')])
    def read(engine,*args):
        calls.append(engine)
        return {'text':text,'boxes':[],'invoice':None}
    monkeypatch.setattr(engines,'local_read',read)
    opts=SimpleNamespace(engine='auto',provider='openai',model='',ai_fallback=False,language='en')
    return calls,opts,SimpleNamespace(root=tmp_path)


def test_readable_unknown_document_does_not_repeat_expensive_ocr(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'Supplier invoice document text\n'+'Readable product and quantity data. '*20)
    events=[]
    result=engines.process(tmp_path/'invoice.pdf',opts,store,lambda *args:None,lambda *args:events.append(args))
    assert calls==['invoice2data']
    assert [x['status'] for x in result['trace']]==['text_only','skipped','skipped']
    assert result['text'] and not result['invoice']['lines']
    assert 'manually' in result['extraction_note']
    assert any(len(x)==3 and x[2]['characters']>250 for x in events)


def test_purchase_order_hint_does_not_invent_invoice(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'Purchase Order\nStore Purchase Order # :\n'+'PO product quantity price evidence '*20)
    result=engines.process(tmp_path/'order.pdf',opts,store,lambda *args:None)
    assert calls==['invoice2data']
    assert result['document_type_hint']=='possible_purchase_order'
    assert result['invoice']['number'] is None
    assert not result['invoice']['lines']


def test_invoice_heading_prevents_purchase_order_hint(monkeypatch,tmp_path):
    _,opts,store=setup(monkeypatch,tmp_path,'Tax Invoice\nPurchase Order\nStore Purchase Order # :\n'+'Product data '*35)
    result=engines.process(tmp_path/'invoice.pdf',opts,store,lambda *args:None)
    assert result['document_type_hint'] is None


def test_scan_uses_selected_vision_before_heavy_ocr(monkeypatch,tmp_path):
    from app.models import Invoice
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.ai_fallback=True;opts.provider='vertex';opts.model='gemini-test'
    def ai(*args):
        calls.append('vertex')
        return Invoice(number='SYN-1',net='10',lines=[{'qty':'2','price':'5'}]),{}
    result=engines.process(tmp_path/'scan.pdf',opts,store,ai)
    assert calls==['invoice2data','vertex']
    assert len(result['invoice']['lines'])==1
    assert result['selected_engine']=='vertex / gemini-test'


def test_failed_vision_falls_back_once_to_local_readers(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.ai_fallback=True;opts.model='vision'
    def ai(*args):
        calls.append('ai');raise ValueError('Unavailable')
    result=engines.process(tmp_path/'scan.pdf',opts,store,ai)
    assert calls==['invoice2data','ai','paddleocr','docling']
    assert not result['invoice']['lines']


def test_empty_ai_response_is_not_reported_as_extracted(monkeypatch,tmp_path):
    from app.models import Invoice
    _,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='ai';opts.model='vision'
    result=engines.process(tmp_path/'scan.pdf',opts,store,lambda *args:(Invoice(),{}))
    assert result['trace'][0]['status']=='no_fields'
    assert result['selected_engine']=='none'


def test_known_purchase_order_does_not_call_ai_as_an_invoice(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'Purchase Order\nStore Purchase Order # :\n'+'PO product quantity price evidence '*20)
    opts.ai_fallback=True;opts.model='vision'
    def forbidden(*args):
        raise AssertionError('A known purchase order must not be sent for invoice extraction')
    result=engines.process(tmp_path/'order.pdf',opts,store,forbidden)
    assert result['document_type_hint']=='possible_purchase_order'
    assert result['extraction_note']
    assert result['trace'][-1]['status']=='skipped'
    assert not result['invoice']['lines']


def test_printed_line_net_is_extraction_evidence_not_a_repriced_unit():
    from app.models import Invoice
    invoice=Invoice(number='SYN-PRINTED',date='2026-01-02',currency='AED',net='10.01',tax='0.50',
        lines=[{'sku':'0001','qty':'3','uom':'PCE','price':'3.33','net_amount':'10.01','tax_amount':'0.50'}])
    score,missing=engines.quality(invoice)
    assert score==1
    assert not missing
    assert str(invoice.lines[0].price)=='3.33'


def test_ai_customer_number_does_not_become_internal_buyer(monkeypatch,tmp_path):
    from app.models import Invoice
    _,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='ai';opts.model='vision'
    source=Invoice(number='SYN-1',buyer='VENDOR-CUSTOMER-1',site='INVENTED-SITE')
    result=engines.process(tmp_path/'scan.pdf',opts,store,lambda *args:(source,{}))
    assert result['invoice']['buyer'] is None
    assert result['invoice']['site'] is None
    assert result['invoice']['number']=='SYN-1'
    assert set(result['trace'][0]['usage']['unverified_internal_fields_ignored'])=={'buyer','site'}
