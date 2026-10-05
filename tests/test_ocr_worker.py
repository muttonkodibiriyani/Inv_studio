from copy import deepcopy

import pytest

from app import layout_extract, ocr_worker
from app.ocr_worker import paddle_model_config, spatial_text, structured_extract


def test_paddle_keeps_recognised_text_lines_and_separate_word_geometry(tmp_path,monkeypatch):
    import sys
    from types import SimpleNamespace
    from PIL import Image
    path=tmp_path/'synthetic.png';Image.new('RGB',(80,30),'white').save(path)
    class Reader:
        def predict(self,image,return_word_box=False):
            assert return_word_box is True
            return [SimpleNamespace(json={'res':{
                'rec_texts':['Invoice Number: INV-1'],'rec_scores':[0.99],
                'rec_polys':[[[0,0],[80,0],[80,20],[0,20]]],
                'text_word':[['Invoice','Number:','INV-1']],
                'text_word_boxes':[[[0,0,25,20],[26,0,50,20],[51,0,80,20]]]
            }})]
    monkeypatch.setattr(ocr_worker,'paddle_reader',lambda language:Reader())
    monkeypatch.setitem(sys.modules,'numpy',SimpleNamespace(array=lambda image:image))
    text,boxes=ocr_worker.paddle(path,'en')
    assert text=='Invoice Number: INV-1'
    assert [box['text'] for box in boxes]==['Invoice','Number:','INV-1']
    assert all(box['geometry']=='word' for box in boxes)


def test_paddle_models_are_pinned_by_supported_script():
    english = paddle_model_config("en")
    assert english == {
        "text_detection_model_name": "PP-OCRv5_mobile_det",
        "text_recognition_model_name": "PP-OCRv5_mobile_rec",
        "text_det_limit_side_len": 1600,
        "text_det_limit_type": "max",
    }
    assert paddle_model_config("ch")["text_recognition_model_name"] == "PP-OCRv5_mobile_rec"
    assert paddle_model_config("ar")["text_recognition_model_name"] == "arabic_PP-OCRv5_mobile_rec"
    assert paddle_model_config("fr")["text_recognition_model_name"] == "latin_PP-OCRv5_mobile_rec"
    assert paddle_model_config("de")["text_recognition_model_name"] == "latin_PP-OCRv5_mobile_rec"
    with pytest.raises(ValueError, match="Unsupported OCR language"):
        paddle_model_config("unknown")


def test_spatial_text_groups_rows_and_preserves_raw_boxes():
    boxes = [
        {"text": "second", "page": 1, "box": [100, 10, 150, 20]},
        {"text": "first", "page": 1, "box": [10, 11, 60, 21]},
        {"text": "next row", "page": 1, "box": [10, 32, 80, 42]},
        {"text": "without geometry", "page": 1, "box": None},
        {"text": "page two", "page": 2, "box": [5, 5, 50, 15]},
    ]
    original = deepcopy(boxes)

    assert spatial_text(boxes) == (
        "first  second\nnext row\nwithout geometry\npage two"
    )
    assert boxes == original


def test_structured_extract_uses_validated_layout_only_after_template_miss(monkeypatch):
    candidate = {
        "number": "DEMO-1",
        "supplier_name": None,
        "seller": None,
        "site": None,
        "buyer": None,
        "po": None,
        "location": None,
        "date": None,
        "currency": None,
        "origin": None,
        "market": None,
        "taxCode": None,
        "net": None,
        "tax": None,
        "lines": [],
    }
    seen = {}
    monkeypatch.setattr(ocr_worker, "template_extract", lambda text, templates: None)
    monkeypatch.setattr(
        layout_extract,
        "extract_invoice",
        lambda text, boxes: seen.update({"text": text, "boxes": boxes}) or candidate,
    )
    boxes = [{"text": "Invoice", "page": 1, "box": [0, 0, 10, 10]}]

    parsed, method = structured_extract("Invoice DEMO-1", boxes, [])

    assert method == "layout"
    assert parsed["number"] == "DEMO-1"
    assert seen == {"text": "Invoice DEMO-1", "boxes": boxes}


def test_structured_extract_keeps_template_precedence(monkeypatch):
    monkeypatch.setattr(ocr_worker, "template_extract", lambda text, templates: {"number": "T-1"})
    monkeypatch.setattr(
        layout_extract,
        "extract_invoice",
        lambda text, boxes: pytest.fail("layout extraction should not run after a template match"),
    )

    assert structured_extract("Invoice T-1", [], [object()]) == ({"number": "T-1"}, "template")
    monkeypatch.setattr(ocr_worker, "template_extract", lambda text, templates: None)
    assert structured_extract("", [], []) == (None, "text_only")


def test_tables_keep_lines_and_only_add_compatible_same_reader_headers(monkeypatch):
    from app import docling_extract
    monkeypatch.setattr(ocr_worker,"template_extract",lambda *_:None)
    table={"number":"INV-1","lines":[{"sku":"TABLE-1","qty":"2","price":"4"}]}
    monkeypatch.setattr(docling_extract,"extract_invoice_from_tables",lambda text,tables,boxes=None:deepcopy(table))
    monkeypatch.setattr(layout_extract,"extract_invoice",lambda *_:{"number":"INV-1","date":"2026-01-02","net":"8","buyer":"UNPROVEN","lines":[{"sku":"OTHER"}]})
    result,method=structured_extract("Invoice INV-1",[],[],tables=[{"rows":[]}])
    assert method=='table'
    assert result['lines'][0]['sku']=='TABLE-1'
    assert result['date']=='2026-01-02'
    assert result['net']=='8'
    assert result['buyer'] is None
    monkeypatch.setattr(layout_extract,"extract_invoice",lambda *_:{"number":"DIFFERENT","date":"2026-01-02"})
    result,_=structured_extract("Invoice INV-1",[],[],tables=[{"rows":[]}])
    assert result['date'] is None
