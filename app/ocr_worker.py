"""Isolated OCR process. No API credentials are passed to this worker."""
import argparse
import json
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path


PADDLE_DETECTION_MODEL = "PP-OCRv5_mobile_det"
PADDLE_RECOGNITION_MODELS = {
    "en": "PP-OCRv5_mobile_rec",
    "ch": "PP-OCRv5_mobile_rec",
    "ar": "arabic_PP-OCRv5_mobile_rec",
    "fr": "latin_PP-OCRv5_mobile_rec",
    "de": "latin_PP-OCRv5_mobile_rec",
}
PADDLE_MAX_SIDE = 1600


def paddle_model_config(language):
    try:
        recognition = PADDLE_RECOGNITION_MODELS[language]
    except KeyError:
        raise ValueError("Unsupported OCR language") from None
    return {
        "text_detection_model_name": PADDLE_DETECTION_MODEL,
        "text_recognition_model_name": recognition,
        "text_det_limit_side_len": PADDLE_MAX_SIDE,
        "text_det_limit_type": "max",
    }


def spatial_text(boxes):
    """Rebuild reading rows while leaving raw OCR boxes unchanged."""
    pages = {}
    for index, entry in enumerate(boxes):
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        page = entry.get("page", 1)
        box = entry.get("box")
        positioned = None
        if isinstance(box, (list, tuple)) and len(box) == 4 and all(
            isinstance(value, (int, float)) and math.isfinite(value) for value in box
        ):
            left, top, right, bottom = (float(value) for value in box)
            if right >= left and bottom >= top:
                positioned = {"text": text, "left": left, "top": top, "bottom": bottom,
                              "center": (top + bottom) / 2, "height": max(1.0, bottom - top)}
        pages.setdefault(page, {"positioned": [], "loose": []})[
            "positioned" if positioned else "loose"
        ].append(positioned or (index, text))

    output = []
    for page in sorted(pages, key=lambda value: (not isinstance(value, int), str(value))):
        rows = []
        for item in sorted(pages[page]["positioned"], key=lambda value: (value["center"], value["left"])):
            best = None
            for row in rows:
                center = sum(row["centers"]) / len(row["centers"])
                height = sorted(row["heights"])[len(row["heights"]) // 2]
                distance = abs(item["center"] - center)
                overlap = min(item["bottom"], row["bottom"]) - max(item["top"], row["top"])
                aligned = distance <= max(3.0, 0.55 * max(item["height"], height))
                if aligned or overlap >= 0.25 * min(item["height"], height):
                    if best is None or distance < best[0]:
                        best = (distance, row)
            if best is None:
                rows.append({"items": [item], "centers": [item["center"]],
                             "heights": [item["height"]], "top": item["top"],
                             "bottom": item["bottom"]})
            else:
                row = best[1]
                row["items"].append(item)
                row["centers"].append(item["center"])
                row["heights"].append(item["height"])
                row["top"] = min(row["top"], item["top"])
                row["bottom"] = max(row["bottom"], item["bottom"])
        rows.sort(key=lambda row: sum(row["centers"]) / len(row["centers"]))
        output.extend("  ".join(item["text"] for item in sorted(row["items"], key=lambda x: x["left"]))
                      for row in rows)
        output.extend(text for _, text in sorted(pages[page]["loose"]))
    return "\n".join(output)


def paddle_reader(language):
    # Cached official model names skip mutable defaults and network discovery.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR
    return PaddleOCR(**paddle_model_config(language),use_doc_orientation_classify=False,
                     use_doc_unwarping=False,use_textline_orientation=False,
                     text_recognition_batch_size=16,device="cpu",enable_mkldnn=False,cpu_threads=2)


def templates_from(paths):
    from invoice2data.extract.loader import read_templates
    return [t for path in paths for t in read_templates(str(path))]


def template_extract(text, templates):
    # Reuse invoice2data's parser after a local OCR/layout engine has read text.
    from invoice2data import extract_data
    class TextReader:
        @staticmethod
        def to_text(_): return text
    result=extract_data("ocr-text", templates=templates, input_module=TextReader, ai_fallback=False)
    if not result: return None
    fields={"invoice_number":"number","amount":"net"}
    allowed={"number","supplier_name","seller","site","buyer","po","location","date","currency","origin","market","taxCode","net","tax","lines"}
    out={fields.get(k,k):v for k,v in result.items() if fields.get(k,k) in allowed}
    if isinstance(out.get("date"),(date,datetime)):out["date"]=out["date"].strftime("%Y-%m-%d")
    from .models import Line
    lines=[]
    for row in out.get("lines",[]):
        row=dict(row)
        if "description" not in row and "name" in row:row["description"]=row["name"]
        lines.append({k:v for k,v in row.items() if k in Line.model_fields})
    out["lines"]=lines
    return out


def structured_extract(text, boxes, templates, tables=None):
    parsed = template_extract(text, templates) if text else None
    if parsed is not None:
        return parsed, "template"
    if tables:
        from .docling_extract import extract_invoice_from_tables
        candidate=extract_invoice_from_tables(text,tables,boxes=boxes)
        if candidate is not None:
            # Complement table rows only with explicit header facts from this
            # same reader's text/geometry. Never borrow a different engine's rows.
            from .layout_extract import extract_invoice
            header=extract_invoice(text,boxes) or {}
            fields=("number","po","date","currency","net","tax","supplier_name")
            compatible=all(candidate.get(key) in (None,"") or header.get(key) in (None,"")
                           or str(candidate[key])==str(header[key]) for key in ("number","po"))
            if compatible:
                for key in fields:
                    if candidate.get(key) in (None,"") and header.get(key) not in (None,""):
                        candidate[key]=header[key]
            from .models import Invoice
            return Invoice.model_validate(candidate).model_dump(mode="json"), "table"
    if text:
        from .layout_extract import extract_invoice
        candidate = extract_invoice(text, boxes)
        if candidate is not None:
            from .models import Invoice
            return Invoice.model_validate(candidate).model_dump(mode="json"), "layout"
    return None, "text_only"


def digital(path, with_tables=False):
    import pdfplumber
    suffix=path.suffix.lower()
    if suffix in (".txt",".csv",".json"):return path.read_text(encoding="utf-8-sig"),[]
    if suffix==".docx":
        import zipfile
        from defusedxml import ElementTree
        with zipfile.ZipFile(path) as z:
            root=ElementTree.fromstring(z.read("word/document.xml"))
        ns={"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        return "\n".join("".join(t.text or "" for t in p.findall(".//w:t",ns)) for p in root.findall(".//w:p",ns)),[]
    if suffix==".xlsx":
        from openpyxl import load_workbook
        book=load_workbook(path,read_only=True,data_only=True);chunks=[]
        for sheet in book:
            if sheet.max_row>10000 or sheet.max_column>60:raise ValueError("Invoice spreadsheet is too large")
            chunks.append(sheet.title)
            chunks.extend(" | ".join(str(x) if x is not None else "" for x in row) for row in sheet.iter_rows(values_only=True))
        book.close();return "\n".join(chunks),[]
    if suffix!=".pdf":return "",[]
    pages=[]; boxes=[];tables=[]
    with pdfplumber.open(path) as pdf:
        for n,page in enumerate(pdf.pages,1):
            pages.append(page.extract_text() or "")
            if with_tables:
                tables.extend({"page":n,"rows":rows,"source":"pdfplumber","size":[page.width,page.height]} for rows in page.extract_tables() if rows)
            for word in page.extract_words()[:4000]:
                boxes.append({"text":word["text"],"page":n,"box":[word["x0"],word["top"],word["x1"],word["bottom"]],"size":[page.width,page.height]})
    return ("\n".join(pages),boxes,tables) if with_tables else ("\n".join(pages),boxes)


def paddle(path,language):
    import pypdfium2 as pdfium
    import numpy as np
    from PIL import Image, ImageOps, ImageSequence
    reader=paddle_reader(language)
    boxes=[];line_boxes=[]
    with tempfile.TemporaryDirectory() as temp:
        pages=[]
        if path.suffix.lower()==".pdf":
            doc=pdfium.PdfDocument(path)
            for n,page in enumerate(doc):
                image=page.render(scale=2).to_pil().convert("RGB")
                target=Path(temp)/f"{n}.png";image.save(target);pages.append(target)
            doc.close()
        else:
            with Image.open(path) as original:
                for n,frame in enumerate(ImageSequence.Iterator(original)):
                    if n>=20:raise ValueError("Too many image frames")
                    target=Path(temp)/f"{n}.png"
                    ImageOps.exif_transpose(frame).convert("RGB").save(target);pages.append(target)
        for n,file in enumerate(pages,1):
            with Image.open(file) as original:
                image=ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail((2600,2600)); size=list(image.size)
                for result in reader.predict(np.array(image),return_word_box=True):
                    res=result.json
                    if isinstance(res,str):res=json.loads(res)
                    res=res.get("res",res)
                    texts=res.get("rec_texts",[]);scores=res.get("rec_scores",[]);polys=res.get("rec_polys",[])
                    for idx,value in enumerate(texts):
                        poly=polys[idx] if idx<len(polys) else []
                        box=[min(p[0] for p in poly),min(p[1] for p in poly),max(p[0] for p in poly),max(p[1] for p in poly)] if len(poly) else None
                        line_box={"text":value,"page":n,"confidence":float(scores[idx]) if idx<len(scores) else None,"box":box,"size":size}
                        line_boxes.append(line_box)
                        word_texts=res.get("text_word",[])
                        word_regions=res.get("text_word_boxes",[])
                        if idx<len(word_texts) and idx<len(word_regions) and len(word_texts[idx])==len(word_regions[idx]) and word_texts[idx]:
                            for word,region in zip(word_texts[idx],word_regions[idx]):
                                if len(region)!=4:continue
                                boxes.append({"text":word,"page":n,"confidence":float(scores[idx]) if idx<len(scores) else None,
                                    "box":[float(v) for v in region],"size":size,"geometry":"word","parent_text":value})
                            continue
                        boxes.append(line_box)
    # Keep real text-line strings for templates; word geometry serves tables
    # without adding artificial spaces inside header labels or numeric fields.
    return spatial_text(line_boxes),boxes


def docling(path,language="en"):
    from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from .docling_extract import document_payload
    cache=Path.home()/".cache"/"inv-studio"/"rapidocr"
    cache.mkdir(parents=True,exist_ok=True)
    # Native PDFs already expose their text to Docling. Running OCR over that
    # text duplicates words and makes table structure less reliable.
    has_native_text=False
    if path.suffix.lower()==".pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            has_native_text=any(len((page.extract_text() or "").strip())>=20 for page in pdf.pages)
    options=PdfPipelineOptions(do_ocr=not has_native_text,do_table_structure=True,generate_parsed_pages=True)
    options.ocr_options=RapidOcrOptions(backend="torch",lang=["ch" if language=="ch" else "iso:"+language],
        rapidocr_params={"Global.model_root_dir":cache})
    converter=DocumentConverter(format_options={InputFormat.PDF:PdfFormatOption(pipeline_options=options),
        InputFormat.IMAGE:ImageFormatOption(pipeline_options=options)})
    result=converter.convert(path)
    doc=result.document
    return document_payload(doc,result.pages)


def main():
    p=argparse.ArgumentParser();p.add_argument("--engine",choices=["invoice2data","paddleocr","docling"],required=True)
    p.add_argument("--file",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--templates",type=Path,action="append",default=[]);p.add_argument("--language",default="en")
    args=p.parse_args()
    try:
        tables=[];parser_error=None
        if args.engine=="invoice2data":
            if args.file.suffix.lower()==".pdf":text,boxes,tables=digital(args.file,with_tables=True)
            else:text,boxes=digital(args.file)
        elif args.engine=="paddleocr":text,boxes=paddle(args.file,args.language)
        else:text,boxes,tables=docling(args.file,args.language)
        if args.file.suffix.lower()==".json":
            from .models import Invoice
            parsed=Invoice.model_validate_json(text).model_dump(mode="json")
            extraction_method="template"
        else:
            try:
                parsed,extraction_method=structured_extract(text,boxes,templates_from(args.templates),tables)
            except Exception as parse_error:
                parsed=None;extraction_method="text_only";parser_error=type(parse_error).__name__
        args.output.write_text(json.dumps({"text":text[:150000],"boxes":boxes[:10000],
            "invoice":parsed,"extraction_method":extraction_method,"tables":tables,"parser_error":parser_error},default=str))
    except Exception as e:
        # No document or credential content in a user-facing error.
        args.output.write_text(json.dumps({"error":type(e).__name__,"hint":"Engine could not read this document. Check its installation, model cache and input format."}))
        raise SystemExit(1)


if __name__=="__main__":main()
