"""Isolated OCR process. No API credentials are passed to this worker."""
import argparse
import json
import tempfile
from datetime import date, datetime
from pathlib import Path


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


def digital(path):
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
    pages=[]; boxes=[]
    with pdfplumber.open(path) as pdf:
        for n,page in enumerate(pdf.pages,1):
            pages.append(page.extract_text() or "")
            for word in page.extract_words()[:4000]:
                boxes.append({"text":word["text"],"page":n,"box":[word["x0"],word["top"],word["x1"],word["bottom"]],"size":[page.width,page.height]})
    return "\n".join(pages),boxes


def paddle(path,language):
    from paddleocr import PaddleOCR
    import pypdfium2 as pdfium
    import numpy as np
    from PIL import Image, ImageOps, ImageSequence
    reader=PaddleOCR(lang=language,use_doc_orientation_classify=False,
                     use_doc_unwarping=False,use_textline_orientation=False,device="cpu",enable_mkldnn=False,cpu_threads=2)
    text=[]; boxes=[]
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
                for result in reader.predict(np.array(image)):
                    res=result.json
                    if isinstance(res,str):res=json.loads(res)
                    res=res.get("res",res)
                    texts=res.get("rec_texts",[]);scores=res.get("rec_scores",[]);polys=res.get("rec_polys",[])
                    for idx,value in enumerate(texts):
                        text.append(value)
                        poly=polys[idx] if idx<len(polys) else []
                        box=[min(p[0] for p in poly),min(p[1] for p in poly),max(p[0] for p in poly),max(p[1] for p in poly)] if len(poly) else None
                        boxes.append({"text":value,"page":n,"confidence":float(scores[idx]) if idx<len(scores) else None,"box":box,"size":size})
    return "\n".join(text),boxes


def docling(path):
    from docling.document_converter import DocumentConverter
    result=DocumentConverter().convert(path)
    doc=result.document
    boxes=[]
    for item,_ in doc.iterate_items():
        for prov in getattr(item,"prov",[]):
            b=prov.bbox
            boxes.append({"text":getattr(item,"text",""),"page":prov.page_no,"box":[b.l,b.t,b.r,b.b],"coordinate_system":str(b.coord_origin)})
    return doc.export_to_markdown(),boxes


def main():
    p=argparse.ArgumentParser();p.add_argument("--engine",choices=["invoice2data","paddleocr","docling"],required=True)
    p.add_argument("--file",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    p.add_argument("--templates",type=Path,action="append",default=[]);p.add_argument("--language",default="en")
    args=p.parse_args()
    try:
        if args.engine=="invoice2data":text,boxes=digital(args.file)
        elif args.engine=="paddleocr":text,boxes=paddle(args.file,args.language)
        else:text,boxes=docling(args.file)
        if args.file.suffix.lower()==".json":
            from .models import Invoice
            parsed=Invoice.model_validate_json(text).model_dump(mode="json")
        else:parsed=template_extract(text,templates_from(args.templates)) if text else None
        args.output.write_text(json.dumps({"text":text[:150000],"boxes":boxes[:10000],"invoice":parsed},default=str))
    except Exception as e:
        # No document or credential content in a user-facing error.
        args.output.write_text(json.dumps({"error":type(e).__name__,"hint":"Engine could not read this document. Check its installation, model cache and input format."}))
        raise SystemExit(1)


if __name__=="__main__":main()
