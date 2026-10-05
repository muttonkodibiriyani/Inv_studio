from types import SimpleNamespace
import pytest
from app import engines


def setup(monkeypatch,tmp_path,text):
    calls=[]
    monkeypatch.setattr(engines,'capabilities',lambda:[{'id':x,'installed':True} for x in ('invoice2data','paddleocr','docling')])
    def read(engine,*args):
        calls.append(engine)
        return {'text':text,'boxes':[],'invoice':None}
    monkeypatch.setattr(engines,'local_read',read)
    opts=SimpleNamespace(engine='auto',provider='openai',model='',ai_fallback=False,
                         language='en',prefer_native_text=True)
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


def test_invoice2data_scan_uses_named_local_ocr_input_without_ai(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='invoice2data'
    def read(engine,*args):
        calls.append(engine)
        if engine=='invoice2data':return {'text':'\n','boxes':[],'invoice':None}
        return {'text':'Scanned source words','boxes':[],
                'recovery':{'attempted':True,'selected_pass':'higher_resolution'},
                'invoice':{'number':'SYN-SCAN','lines':[{'description':'Printed product','qty':'2','price':'5'}]}}
    monkeypatch.setattr(engines,'local_read',read)
    def no_ai(*args):raise AssertionError('No AI is enabled')
    result=engines.process(tmp_path/'scan.pdf',opts,store,no_ai)
    assert calls==['invoice2data','paddleocr']
    assert result['selected_engine']=='invoice2data + PaddleOCR'
    assert len(result['invoice']['lines'])==1
    assert any(t['status']=='needs_ocr' for t in result['trace'])
    assert next(t for t in result['trace'] if t['engine']=='paddleocr')['recovery']['selected_pass']=='higher_resolution'
    calls.clear()
    result=engines.process(tmp_path/'scan.png',opts,store,no_ai)
    assert calls==['paddleocr']
    assert result['selected_engine']=='invoice2data + PaddleOCR'


@pytest.mark.parametrize("slots", [1, 2])
def test_bulk_heavy_local_readers_respect_configured_slots(monkeypatch,tmp_path,slots):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    _,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='paddleocr'
    opts.prefer_native_text=False
    monkeypatch.setattr(engines,'_LOCAL_OCR_SLOTS',threading.BoundedSemaphore(slots))
    active=0;peak=0;counter=threading.Lock();waiting=[]
    def read(*args):
        nonlocal active,peak
        with counter:active+=1;peak=max(peak,active)
        time.sleep(0.03)
        with counter:active-=1
        return {'text':'Synthetic words','boxes':[],'invoice':{'number':'SYN-SLOT','lines':[]}}
    monkeypatch.setattr(engines,'local_read',read)
    def run(index):
        return engines.process(tmp_path/f'{index}.pdf',opts,store,lambda *args:None,
                               lambda *args:waiting.append(args[1]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,range(2)))
    assert peak==slots and len(results)==2
    assert waiting.count('Waiting for the local OCR reader')==2
    queue_times=[result['trace'][0]['queue_seconds'] for result in results]
    if slots==1:assert max(queue_times)>0


def complete_native_invoice(number='NATIVE-1'):
    return {'number':number,'currency':'AED','net':'15.00','tax':'0.75','lines':[
        {'sku':'0001','description':'First','qty':'2','uom':'PCE','price':'5.00','net_amount':'10.00'},
        {'gtin':'0123456789012','description':'Second','qty':'1','uom':'PCE','price':'5.00','net_amount':'5.00'},
    ]}


def test_selected_paddle_skips_ocr_only_after_reconciled_native_quality_gate(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='paddleocr'
    def read(engine,*args):
        calls.append(engine)
        if engine!='invoice2data':raise AssertionError('Paddle must not run after the native gate passes')
        return {'text':'embedded invoice text','boxes':[],'invoice':complete_native_invoice(),
                'extraction_method':'table','tables':[{'rows':[]}]}
    monkeypatch.setattr(engines,'local_read',read)

    result=engines.process(tmp_path/'invoice.pdf',opts,store,lambda *args:None)

    assert calls==['invoice2data']
    assert result['selected_engine']=='native PDF text'
    assert [entry['engine'] for entry in result['trace']]==['native_pdf_text','paddleocr']
    assert result['trace'][0]['status']=='extracted'
    assert result['trace'][1]['status']=='skipped'
    assert 'did not run' in result['trace'][1]['reason']


def test_selected_paddle_runs_when_native_lines_are_incomplete(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='paddleocr'
    incomplete=complete_native_invoice('NATIVE-INCOMPLETE')
    incomplete['lines'][0]['qty']=None
    def read(engine,*args):
        calls.append(engine)
        invoice=incomplete if engine=='invoice2data' else complete_native_invoice('PADDLE-1')
        return {'text':f'{engine} text','boxes':[],'invoice':invoice,'extraction_method':'table'}
    monkeypatch.setattr(engines,'local_read',read)

    result=engines.process(tmp_path/'invoice.pdf',opts,store,lambda *args:None)

    assert calls==['invoice2data','paddleocr']
    assert result['selected_engine']=='paddleocr'
    assert [entry['status'] for entry in result['trace']]==['extracted','extracted']
    assert 'line 1 qty' in result['trace'][0]['reason']


def test_selected_ocr_wins_quality_tie_after_native_gate_failure(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='paddleocr'
    incomplete=complete_native_invoice('NATIVE-INCOMPLETE')
    incomplete['lines'][0]['net_amount']=None
    def read(engine,*args):
        calls.append(engine)
        invoice=incomplete if engine=='invoice2data' else complete_native_invoice('PADDLE-1')
        return {'text':f'{engine} text','boxes':[],'invoice':invoice,'extraction_method':'table'}
    monkeypatch.setattr(engines,'local_read',read)

    result=engines.process(tmp_path/'invoice.pdf',opts,store,lambda *args:None)

    assert calls==['invoice2data','paddleocr']
    assert result['selected_engine']=='paddleocr'
    assert result['invoice']['number']=='PADDLE-1'


def test_native_text_preference_can_be_disabled_to_force_selected_ocr(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine='docling';opts.prefer_native_text=False
    monkeypatch.setattr(engines,'local_read',lambda engine,*args:(
        calls.append(engine) or {'text':'reader text','boxes':[],
                                 'invoice':complete_native_invoice('DOCLING-1')}))

    result=engines.process(tmp_path/'invoice.pdf',opts,store,lambda *args:None)

    assert calls==['docling']
    assert result['selected_engine']=='docling'
    assert [entry['engine'] for entry in result['trace']]==['docling']


def test_native_quality_gate_rejects_unreconciled_printed_totals():
    from app.models import Invoice
    invoice=complete_native_invoice()
    invoice['net']='15.02'
    accepted,issues=engines.native_pdf_quality(Invoice.model_validate(invoice))
    assert accepted is False
    assert issues==['printed line amounts do not reconcile with net total or one explicit document discount']


def test_native_quality_gate_accepts_one_explicit_document_discount():
    from app.models import Invoice
    invoice=complete_native_invoice()
    invoice['net']='12.50'
    candidate=Invoice.model_validate(invoice)
    accepted,issues=engines.native_pdf_quality(
        candidate,'Total 15.00\nDiscount 2.50\nNet Total 12.50',
    )
    assert accepted is True and issues==[]
    assert engines._native_reconciliation(candidate,'Discount 2.50')=='explicit_document_discount'
    assert engines.native_pdf_quality(candidate,'Discount 1.00')[0] is False
    assert engines.native_pdf_quality(candidate,'Discount 2.50\nDiscount 2.50')[0] is False


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
    source=Invoice(number='SYN-1',buyer_name='Example Buyer LLC',buyer='VENDOR-CUSTOMER-1',site='INVENTED-SITE',lines=[{'item_id':'INVENTED-ITEM','sku':'VISIBLE-SKU'}])
    result=engines.process(tmp_path/'scan.pdf',opts,store,lambda *args:(source,{}))
    assert result['invoice']['buyer'] is None
    assert result['invoice']['site'] is None
    assert result['invoice']['number']=='SYN-1'
    assert result['invoice']['buyer_name']=='Example Buyer LLC'
    assert result['invoice']['lines'][0]['item_id'] is None
    assert result['invoice']['lines'][0]['sku']=='VISIBLE-SKU'
    assert set(result['trace'][0]['usage']['unverified_internal_fields_ignored'])=={'buyer','site'}


def identityless_native_invoice(number='NATIVE-1'):
    invoice=complete_native_invoice(number)
    invoice['lines'][0]['tax_amount']='0.50';invoice['lines'][1]['tax_amount']='0.25'
    invoice['lines'][1]['gtin']=None;invoice['date']='2026-01-15'
    return invoice


def test_native_gate_keeps_an_identityless_line_only_when_every_amount_checks():
    from app.models import Invoice
    accepted,notes=engines.native_pdf_quality(Invoice.model_validate(identityless_native_invoice()))
    assert accepted is True and notes==['line 2 prints no item code; kept blank for review']
    for field,value in (('tax','0.76'),('net','15.01')):
        broken=identityless_native_invoice();broken[field]=value
        assert engines.native_pdf_quality(Invoice.model_validate(broken))[0] is False
    for change in ({'price':'5.01'},{'tax_amount':None}):
        broken=identityless_native_invoice();broken['lines'][1].update(change)
        assert engines.native_pdf_quality(Invoice.model_validate(broken))[0] is False
    no_codes=identityless_native_invoice();no_codes['lines'][0]['sku']=None
    assert engines.native_pdf_quality(Invoice.model_validate(no_codes))[0] is False


def ai_on(monkeypatch,tmp_path,native,ai_invoice,engine='paddleocr'):
    from app.models import Invoice
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.engine=engine;opts.ai_fallback=True;opts.model='stub-model'
    def read(engine,*args):
        calls.append(engine)
        return {'text':f'{engine} text','boxes':[],'invoice':native if engine=='invoice2data' else None}
    monkeypatch.setattr(engines,'local_read',read)
    def ai_reader(*args):
        calls.append('ai')
        return Invoice.model_validate(ai_invoice),{}
    return engines.process(tmp_path/'invoice.pdf',opts,store,ai_reader),calls


# The paddleocr choice checks the PDF text first; auto (the default) reads it through invoice2data.
ENGINES=[('paddleocr',['invoice2data','paddleocr','ai'],'native PDF text'),
         ('auto',['invoice2data','ai'],'invoice2data')]


def invented_code_ai_read(number='AI-1'):
    invoice=identityless_native_invoice(number);invoice['lines'][1]['sku']='INVENTED-9'
    return invoice


@pytest.mark.parametrize('engine,calls_expected,native_selected',ENGINES)
def test_ai_on_completeness_tie_keeps_the_native_read(monkeypatch,tmp_path,engine,calls_expected,native_selected):
    native=identityless_native_invoice();native['lines'][1]['tax_amount']=None
    ai=identityless_native_invoice('AI-1');ai['lines'][1]['tax_amount']=None
    result,calls=ai_on(monkeypatch,tmp_path,native,ai,engine)
    assert calls==calls_expected
    ai_entry=next(x for x in result['trace'] if x['engine']=='openai')
    assert result['trace'][0]['completeness']==ai_entry['completeness']==result['completeness']
    assert result['selected_engine']==native_selected and result['invoice']['number']=='NATIVE-1'


@pytest.mark.parametrize('engine,calls_expected,native_selected',ENGINES)
def test_ai_on_strictly_more_complete_ai_read_is_kept(monkeypatch,tmp_path,engine,calls_expected,native_selected):
    native=identityless_native_invoice();native['lines'][1]['tax_amount']=None
    result,calls=ai_on(monkeypatch,tmp_path,native,{**complete_native_invoice('AI-1'),'date':'2026-01-15'},engine)
    assert calls==calls_expected
    assert result['selected_engine']=='openai / stub-model' and result['invoice']['number']=='AI-1'


@pytest.mark.parametrize('engine,calls_expected,native_selected',ENGINES)
def test_ai_on_identityless_line_with_every_amount_checking_skips_ocr_and_ai(monkeypatch,tmp_path,engine,
                                                                            calls_expected,native_selected):
    result,calls=ai_on(monkeypatch,tmp_path,identityless_native_invoice(),invented_code_ai_read(),engine)
    assert calls==['invoice2data']
    assert result['selected_engine']==native_selected and result['invoice']['number']=='NATIVE-1'
    line=result['invoice']['lines'][1]
    assert line['gtin'] is None and line['sku'] is None
    assert 'line 2 prints no item code' in result['trace'][0]['reason']
    assert 'line 2 prints no item code; kept blank for review' in result['extraction_note']


def test_auto_identity_gap_that_fails_a_check_still_reaches_the_ai(monkeypatch,tmp_path):
    native=identityless_native_invoice();native['net']='15.01'
    result,calls=ai_on(monkeypatch,tmp_path,native,invented_code_ai_read(),'auto')
    assert calls==['invoice2data','ai'] and result['extraction_note'] is None
    assert 'line 2 item identity' in result['trace'][0]['reason']


def test_ai_evidence_is_carried_beside_the_invoice_not_in_the_trace(monkeypatch,tmp_path):
    from app.models import Invoice
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    opts.ai_fallback=True;opts.provider='vertex';opts.model='gemini-test'
    def ai(*args):
        return Invoice(number='SYN-1',net='10',lines=[{'qty':'2','price':'5','evidence':'2 x 5.00','page':1}]),{
            'promptTokenCount':1,'evidence':{'number':{'quote':'Invoice SYN-1','page':1},'po':{'quote':'PO 7','page':1}}}
    result=engines.process(tmp_path/'scan.pdf',opts,store,ai)
    assert result['selected_engine']=='vertex / gemini-test'
    assert result['evidence']['header']=={'number':{'quote':'Invoice SYN-1','page':1,'source':'ai'}}
    assert result['evidence']['lines']==[{'qty':{'quote':'2 x 5.00','page':1,'source':'ai'},
                                          'price':{'quote':'2 x 5.00','page':1,'source':'ai'}}]
    assert next(t for t in result['trace'] if t['engine']=='vertex')['usage']=={'promptTokenCount':1}
    assert 'header_evidence' not in result['invoice'] and 'evidence' not in result['invoice']
    assert engines.needs_scan_evidence(result,opts)


def test_native_evidence_locates_values_in_the_text_layer(monkeypatch,tmp_path):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    boxes=[{'text':'NATIVE-1','page':1,'box':[100,50,160,60],'size':[600,800]},
           {'text':'0123456789012','page':1,'box':[100,90,190,100],'size':[600,800]}]
    def read(engine,*args):
        return {'text':'Invoice NATIVE-1 '*20,'boxes':boxes,'invoice':complete_native_invoice()}
    monkeypatch.setattr(engines,'local_read',read)
    result=engines.process(tmp_path/'digital.pdf',opts,store,None)
    number=result['evidence']['header']['number']
    assert number['source']=='native' and number['page']==1 and number['box'][0]==round(100/600,4)
    assert result['evidence']['lines'][1]['gtin']['quote']=='0123456789012'
    assert result['evidence']['lines'][0]=={}
    assert not engines.needs_scan_evidence(result,opts)


def test_verify_scan_asks_the_local_reader_for_text_only_under_the_ocr_slots(monkeypatch,tmp_path):
    seen=[]
    def read(engine,path,root,language='en',extra=()):
        seen.append((engine,tuple(extra)));return {'text':'INV-1','boxes':[{'text':'INV-1'}],'invoice':None}
    monkeypatch.setattr(engines,'local_read',read)
    assert engines.verify_scan(tmp_path/'scan.pdf',tmp_path)==([{'text':'INV-1'}],'INV-1')
    assert seen==[('paddleocr',('--text-only',))]


def scan_reads(monkeypatch,tmp_path,paddle_lines,docling_lines):
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    def invoice(lines):
        return {'number':'SYN-SCAN','date':'2026-01-15','currency':'AED','net':'40.00','tax':'2.00','lines':[
            {'sku':f'S{n}','qty':'1','uom':'PCE','price':'10.00','net_amount':'10.00','page':1+n//2} for n in range(lines)]}
    def read(engine,*args):
        calls.append(engine)
        found={'paddleocr':paddle_lines,'docling':docling_lines}.get(engine)
        return {'text':f'{engine} words','boxes':[],'invoice':invoice(found) if found else None}
    monkeypatch.setattr(engines,'local_read',read)
    return engines.process(tmp_path/'scan.pdf',opts,store,lambda *args:None),calls


def test_a_scan_whose_paddle_lines_do_not_sum_to_the_net_still_reaches_docling(monkeypatch,tmp_path):
    # A 2-page scan printing 4 lines; the PaddleOCR table read 2 of them, so its lines fall short of the net.
    result,calls=scan_reads(monkeypatch,tmp_path,2,4)
    assert calls==['invoice2data','paddleocr','docling']
    assert result['selected_engine']=='docling' and len(result['invoice']['lines'])==4
    # The existing score decides: a docling read no better than PaddleOCR's leaves PaddleOCR's read.
    result,calls=scan_reads(monkeypatch,tmp_path,2,1)
    assert calls==['invoice2data','paddleocr','docling'] and result['selected_engine']=='paddleocr'


def test_a_scan_whose_paddle_lines_reconcile_stops_after_paddle(monkeypatch,tmp_path):
    result,calls=scan_reads(monkeypatch,tmp_path,4,4)
    assert calls==['invoice2data','paddleocr'] and result['selected_engine']=='paddleocr'
    assert result['trace'][-1]['engine']=='docling' and result['trace'][-1]['status']=='skipped'


def test_a_later_reader_that_loses_keeps_the_selected_readers_text_and_boxes(monkeypatch,tmp_path):
    # Docling runs after an unreconciled PaddleOCR read, reads longer text but fewer lines, and loses:
    # the target check must re-check PaddleOCR's values against PaddleOCR's own text and boxes.
    calls,opts,store=setup(monkeypatch,tmp_path,'')
    lines=lambda n:[{'sku':f'S{i}','qty':'1','uom':'PCE','price':'10.00','net_amount':'10.00'} for i in range(n)]
    reads={'paddleocr':{'text':'paddle page','boxes':[{'text':'paddle box'}],'invoice':{'number':'SYN-SCAN','date':'2026-01-15','currency':'AED','net':'40.00','tax':'2.00','lines':lines(2)}},
           'docling':{'text':'a much longer docling page text','boxes':[{'text':'docling box'}],'invoice':{'number':'SYN-SCAN','lines':lines(1)}}}
    def read(engine,*args):
        calls.append(engine)
        return reads.get(engine,{'text':'','boxes':[],'invoice':None})
    monkeypatch.setattr(engines,'local_read',read)
    result=engines.process(tmp_path/'scan.pdf',opts,store,lambda *args:None)
    assert calls==['invoice2data','paddleocr','docling'] and result['selected_engine']=='paddleocr'
    assert result['text']=='paddle page' and result['boxes']==[{'text':'paddle box'}]
