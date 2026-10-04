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
