import io
from openpyxl import load_workbook
from fastapi.testclient import TestClient
from app.models import Invoice
from app.extraction_draft import extraction_workbook


def test_review_copy_preserves_printed_values_and_leaves_unknown_codes_blank():
    invoice=Invoice(number='=NOT_A_FORMULA',date='2026-01-02',net='10.01',tax='0.50',
        lines=[{'sku':'00077','gtin':'00012345678905','qty':'3','price':'3.33','description':'Test only'}])
    book=load_workbook(io.BytesIO(extraction_workbook(invoice,'synthetic.pdf',2)))
    assert book.sheetnames==['Header','Tax_Breakdown','Details']
    assert [sheet.max_column for sheet in book]==[13,3,6]
    assert book['Header']['B2'].value=='=NOT_A_FORMULA'
    assert book['Header']['B2'].data_type=='s'
    assert book['Header']['H2'].value==10.01
    assert book['Header']['C2'].value is None
    assert book['Details']['B2'].value is None
    assert '00077' in book['Details']['B2'].comment.text
    assert book['Details']['C2'].value=='00012345678905'
    assert book['Details']['D2'].value==3.33
    assert all(cell.data_type!='f' for sheet in book for row in sheet for cell in row)
    assert 'EXTRACTION_REVIEW_ONLY' in book['Header']['M2'].value


def test_review_copy_download_requires_ack_and_never_approves_or_changes_job(tmp_path,monkeypatch):
    monkeypatch.setenv('INV_STUDIO_DATA',str(tmp_path/'module-default'))
    from app.main import create_app
    app=create_app(tmp_path)
    record={'id':'synthetic','filename':'synthetic.pdf','status':'review','revision':2,'reviewed':False,
            'invoice':Invoice(number='REVIEW-1',net='10',lines=[{'sku':'0001','qty':'2','price':'5'}]).model_dump(mode='json')}
    app.state.store.job('synthetic',record)
    with TestClient(app) as client:
        headers={'X-Studio-Request':'1'};url='/api/jobs/synthetic/extraction-draft'
        assert client.post(url,headers=headers,json={'revision':2}).status_code==400
        assert client.post(url,headers=headers,json={'revision':1,'acknowledge_unvalidated':True}).status_code==409
        response=client.post(url,headers=headers,json={'revision':2,'acknowledge_unvalidated':True})
        assert response.status_code==200
        assert 'EXTRACTION_REVIEW_ONLY' in response.headers['Content-Disposition']
        assert app.state.store.job('synthetic')==record
        with app.state.store.connection() as connection:assert app.state.store.ledger(connection)==[]


def test_batch_review_workbook_links_each_invoice_and_rejects_stale_or_unfinished(tmp_path,monkeypatch):
    monkeypatch.setenv('INV_STUDIO_DATA',str(tmp_path/'module-default'))
    from app.main import create_app
    app=create_app(tmp_path)
    originals=[]
    for index,count in enumerate((1,2),1):
        record={'id':str(index),'filename':f'synthetic-{index}.pdf','status':'review','revision':2,'reviewed':False,
                'invoice':Invoice(number=f'INVOICE-{index}',net=str(count*10),tax='0',
                    lines=[{'sku':f'SKU-{index}-{n}','gtin':f'000{index}{n}','qty':'2','price':'5'} for n in range(count)]).model_dump(mode='json')}
        originals.append(record);app.state.store.job(str(index),record)
    with TestClient(app) as client:
        url='/api/exports/extraction-batch';headers={'X-Studio-Request':'1'}
        selected=[{'id':'1','revision':2},{'id':'2','revision':2}]
        assert client.post(url,headers=headers,json={'jobs':selected}).status_code==400
        body={'jobs':selected,'acknowledge_unvalidated':True}
        response=client.post(url,headers=headers,json=body)
        assert response.status_code==200
        book=load_workbook(io.BytesIO(response.content))
        assert book.sheetnames==['Header','Tax_Breakdown','Details']
        assert [s.max_column for s in book]==[13,3,6]
        assert [r[0] for r in book['Header'].iter_rows(min_row=2,values_only=True)]==[1,2]
        assert [r[0] for r in book['Tax_Breakdown'].iter_rows(min_row=2,values_only=True)]==[1,2]
        assert [r[0] for r in book['Details'].iter_rows(min_row=2,values_only=True)]==[1,2,2]
        assert book['Header']['B3'].value=='INVOICE-2'
        assert book['Details']['C4'].value=='00021'
        assert [app.state.store.job(str(i)) for i in (1,2)]==originals
        with app.state.store.connection() as c:assert app.state.store.ledger(c)==[]
        assert client.post(url,headers=headers,json={**body,'jobs':[selected[0],selected[0]]}).status_code==400
        assert client.post(url,headers=headers,json={**body,'jobs':[{'id':'1','revision':1}]}).status_code==409
        for status in ('processing','queued'):
            app.state.store.job('2',{**originals[1],'status':status})
            assert client.post(url,headers=headers,json=body).status_code==409
        app.state.store.job('2',{**originals[1],'invoice':Invoice().model_dump(mode='json')})
        assert client.post(url,headers=headers,json=body).status_code==409
