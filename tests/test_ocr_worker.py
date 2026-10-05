from copy import deepcopy
from types import ModuleType, SimpleNamespace

import pytest

from app import layout_extract, ocr_worker
from app.ocr_worker import paddle_model_config, spatial_text, structured_extract


def _fake_docling_modules(monkeypatch):
    class PipelineOptions:
        def __init__(self,**kwargs):
            vars(self).update(kwargs)
            self.table_structure_options=SimpleNamespace(mode=None,do_cell_matching=False)

    class RapidOptions:
        def __init__(self,**kwargs):
            self.kwargs=kwargs

    class FormatOption:
        def __init__(self,pipeline_options):
            self.pipeline_options=pipeline_options

    class Converter:
        format_options=None

        def __init__(self,format_options):
            type(self).format_options=format_options

        def convert(self,path):
            return SimpleNamespace(document="document",pages=["page"])

    modules={name:ModuleType(name) for name in (
        "docling","docling.document_converter","docling.datamodel",
        "docling.datamodel.base_models","docling.datamodel.pipeline_options",
    )}
    modules["docling.document_converter"].DocumentConverter=Converter
    modules["docling.document_converter"].PdfFormatOption=FormatOption
    modules["docling.document_converter"].ImageFormatOption=FormatOption
    modules["docling.datamodel.base_models"].InputFormat=SimpleNamespace(PDF="pdf",IMAGE="image")
    modules["docling.datamodel.pipeline_options"].PdfPipelineOptions=PipelineOptions
    modules["docling.datamodel.pipeline_options"].RapidOcrOptions=RapidOptions
    modules["docling.datamodel.pipeline_options"].OcrMode=SimpleNamespace(
        FULL_PAGE="full_page",PDF_AWARE_LAYOUT_REGIONS="pdf_aware_layout_regions",
    )
    modules["docling.datamodel.pipeline_options"].TableFormerMode=SimpleNamespace(ACCURATE="accurate")
    for name,module in modules.items():
        monkeypatch.setitem(__import__("sys").modules,name,module)
    return Converter


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
    monkeypatch.setattr(ocr_worker,'paddle_reader',lambda language,max_side=None:Reader())
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


def test_measured_ocr_words_are_parsed_when_reader_has_no_prebuilt_table(monkeypatch):
    from app import docling_extract
    boxes = [{"text": "Printed product", "page": 1, "box": [0, 0, 40, 10], "geometry": "word"}]
    seen = []
    monkeypatch.setattr(ocr_worker, "template_extract", lambda *args: None)
    monkeypatch.setattr(layout_extract, "extract_invoice", lambda *args: {})
    def from_geometry(text, tables, *, boxes):
        seen.append((text, tables, boxes))
        return {"number": "SYN-WORDS", "lines": [{"description": "Printed product", "qty": "2", "price": "5"}]}
    monkeypatch.setattr(docling_extract, "extract_invoice_from_tables", from_geometry)
    result, method = structured_extract("Printed source", boxes, [], tables=[])
    assert seen == [("Printed source", [], boxes)]
    assert method == "table" and len(result["lines"]) == 1


@pytest.mark.parametrize(("page_texts","expected_ocr","expected_mode"),[
    ([""],True,"full_page"),
    (["Native text on this page is definitely long enough",""],True,"pdf_aware_layout_regions"),
    (["Native text on this page is definitely long enough",
      "Native text on the second page is also long enough"],False,"pdf_aware_layout_regions"),
])
def test_docling_uses_full_page_paddle_ocr_and_keeps_mixed_pdf_ocr(
    tmp_path,monkeypatch,page_texts,expected_ocr,expected_mode,
):
    import sys
    from app import docling_extract

    converter=_fake_docling_modules(monkeypatch)
    pages=[SimpleNamespace(extract_text=lambda text=text:text) for text in page_texts]

    class Pdf:
        def __enter__(self): return SimpleNamespace(pages=pages)
        def __exit__(self,*_): return None

    monkeypatch.setitem(sys.modules,"pdfplumber",SimpleNamespace(open=lambda _:Pdf()))
    monkeypatch.setattr(docling_extract,"document_payload",lambda document,pages:("read",[],[]))
    path=tmp_path/"invoice.pdf"

    assert ocr_worker.docling(path,"en")==("read",[],[])
    options=converter.format_options["pdf"].pipeline_options
    assert options.do_ocr is expected_ocr
    assert options.do_table_structure is True
    assert options.generate_parsed_pages is True
    assert options.table_structure_options.mode=="accurate"
    assert options.table_structure_options.do_cell_matching is True
    assert options.ocr_options.kwargs=={
        "backend":"paddle","lang":["iso:en"],"mode":expected_mode,
        "scale":3.0,"model_size":"small",
    }


def test_docling_images_always_use_full_page_ocr(tmp_path,monkeypatch):
    from app import docling_extract

    converter=_fake_docling_modules(monkeypatch)
    monkeypatch.setattr(docling_extract,"document_payload",lambda document,pages:("read",[],[]))

    assert ocr_worker.docling(tmp_path/"invoice.png","ch")==("read",[],[])
    options=converter.format_options["image"].pipeline_options
    assert options.do_ocr is True
    assert options.ocr_options.kwargs["mode"]=="full_page"
    assert options.ocr_options.kwargs["lang"]==["ch"]


def test_docling_preserves_measured_rapidocr_cells_for_payload(tmp_path,monkeypatch):
    from app import docling_extract

    converter=_fake_docling_modules(monkeypatch)
    native=SimpleNamespace(text="native",from_ocr=False)
    ocr=SimpleNamespace(text="detected cell",from_ocr=True)
    page=SimpleNamespace(
        parsed_page=SimpleNamespace(word_cells=[native],textline_cells=[ocr]),
        size=SimpleNamespace(width=100,height=200),
    )
    monkeypatch.setattr(
        converter,"convert",
        lambda self,path:SimpleNamespace(document="document",pages=[page]),
    )
    seen={}
    def payload(document,pages):
        seen["document"]=document
        seen["pages"]=pages
        return "read",[{"from_ocr":True,"geometry":"word"}],[]
    monkeypatch.setattr(docling_extract,"document_payload",payload)

    text,boxes,tables=ocr_worker.docling(tmp_path/"invoice.png","en")

    assert (text,tables)==("read",[])
    assert boxes==[{"from_ocr":True,"geometry":"ocr_cell"}]
    assert seen["document"]=="document"
    assert seen["pages"][0] is not page
    assert seen["pages"][0].parsed_page.word_cells==[native,ocr]
    assert page.parsed_page.word_cells==[native]


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


def _recovery_candidate(lines, **overrides):
    candidate = {
        "number": "SYN-001", "date": "2026-01-02", "currency": "AED",
        "po": None, "net": "19.00", "tax": "0.95", "lines": lines,
    }
    candidate.update(overrides)
    return candidate


def test_paddle_recovery_requires_strict_missing_field_improvement():
    baseline = _recovery_candidate([
        {"sku": "A", "qty": "2", "price": "5", "net_amount": "10"},
        {"sku": "B", "qty": None, "price": "3", "net_amount": "9"},
    ])
    unchanged = deepcopy(baseline)

    criteria, accepted = ocr_worker.paddle_recovery_criteria(baseline, unchanged)

    assert accepted is False
    assert criteria["missing_qty_price_strictly_lower"] is False


@pytest.mark.parametrize("alternate", [
    _recovery_candidate(
        [{"sku": "A", "qty": "2", "price": "5", "net_amount": "10"}],
    ),
    _recovery_candidate([
        {"sku": "A", "qty": "2", "price": "5", "net_amount": "10"},
        {"sku": "B", "qty": "3", "price": "3", "net_amount": "9"},
    ], number="DIFFERENT"),
])
def test_paddle_recovery_rejects_row_or_header_regression(alternate):
    baseline = _recovery_candidate([
        {"sku": "A", "qty": "2", "price": "5", "net_amount": "10"},
        {"sku": "B", "qty": None, "price": "3", "net_amount": "9"},
    ])

    criteria, accepted = ocr_worker.paddle_recovery_criteria(baseline, alternate)

    assert accepted is False
    assert not criteria["row_count_not_lower"] or not criteria["preserved_headers"]


def test_paddle_recovery_accepts_normalized_headers_totals_and_better_lines():
    baseline = _recovery_candidate([
        {"sku": "A", "qty": "2", "price": "5", "net_amount": "10"},
        {"sku": "B", "qty": None, "price": "3", "net_amount": "9"},
    ])
    alternate = _recovery_candidate([
        {"sku": "A", "qty": "2", "price": "5.0", "net_amount": "10.00"},
        {"sku": "B", "qty": "3", "price": "3", "net_amount": "9"},
    ], number=" syn 001 ", currency=" aed ", net="19.000", tax="0.950")

    criteria, accepted = ocr_worker.paddle_recovery_criteria(baseline, alternate)

    assert accepted is True
    assert all(criteria.values())


def test_complete_paddle_baseline_never_starts_second_pass(tmp_path, monkeypatch):
    baseline = _recovery_candidate([
        {"sku": "A", "qty": "2", "price": "5", "net_amount": "10"},
    ])
    monkeypatch.setattr(ocr_worker, "_pdf_page_count", lambda path: 1)
    monkeypatch.setattr(
        ocr_worker, "_run_paddle_recovery_pass",
        lambda *args: pytest.fail("complete baseline must not start recovery"),
    )

    result = ocr_worker.maybe_recover_paddle(
        tmp_path / "invoice.pdf", "en", [], "baseline", [], baseline,
        "table", 40.0, 42.0,
    )

    assert result[:4] == ("baseline", [], baseline, "table")
    assert result[4]["attempted"] is False
    assert result[4]["eligibility"]["missing_qty_or_price"] is False


def test_timed_out_paddle_recovery_keeps_complete_baseline_output(tmp_path, monkeypatch):
    baseline = _recovery_candidate([
        {"sku": "A", "qty": None, "price": "5", "net_amount": "10"},
    ])
    monkeypatch.setattr(ocr_worker, "_pdf_page_count", lambda path: 1)
    monkeypatch.setattr(
        ocr_worker, "_run_paddle_recovery_pass",
        lambda *args: {"status": "timed_out", "seconds": 120.0},
    )

    result = ocr_worker.maybe_recover_paddle(
        tmp_path / "invoice.pdf", "en", [], "baseline", [], baseline,
        "table", 50.0, 55.0,
    )

    assert result[:4] == ("baseline", [], baseline, "table")
    assert result[4]["attempted"] is True
    assert result[4]["alternate_status"] == "timed_out"
    assert result[4]["selected_pass"] == "baseline"
