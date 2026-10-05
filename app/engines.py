import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from datetime import date
from decimal import Decimal
from .models import Invoice

ROOT=Path(__file__).resolve().parent


def capabilities():
    result=[]
    for engine,module,package in [("invoice2data","invoice2data","invoice2data"),("paddleocr","paddleocr","paddleocr"),("docling","docling","docling")]:
        installed=importlib.util.find_spec(module) is not None
        if engine=="paddleocr": installed=installed and importlib.util.find_spec("paddle") is not None
        result.append({"id":engine,"installed":installed,"version":importlib.metadata.version(package) if installed else None,
            "note":"Installed; first use may download model weights" if installed and engine!="invoice2data" else "Ready" if installed else f"Install with uv pip install -e '.[{'paddle' if engine=='paddleocr' else engine}]'"})
    return result


def quality(i:Invoice | None):
    if i is None:return 0,["No structured invoice fields were returned"]
    missing=[f for f in ("number","date","currency","net","tax") if getattr(i,f) in (None,"")]
    if not i.lines:missing.append("line items")
    for n,l in enumerate(i.lines,1):
        for f in ("qty","price","uom"):
            if getattr(l,f) in (None,""):missing.append(f"line {n} {f}")
        if not l.sku and not l.gtin:missing.append(f"line {n} item identity")
    try:date.fromisoformat(i.date or "")
    except ValueError:
        if "date" not in missing:missing.append("unambiguous invoice date")
    if i.lines and i.net is not None and all(l.qty is not None and l.price is not None for l in i.lines):
        if all(getattr(l,"net_amount",None) is not None for l in i.lines):
            net=sum((l.net_amount for l in i.lines),Decimal(0))
        else:net=sum((l.qty*l.price for l in i.lines),Decimal(0))
        if abs(net-i.net)>Decimal("0.01"):missing.append("line amounts do not reconcile with net total")
    total=6+4*len(i.lines)
    return min(0.99 if missing else 1.0,round(max(0,1-len(missing)/total),2)),missing


def local_read(engine,path,root,language="en"):
    output=root/"work"/(uuid.uuid4().hex+".json")
    cmd=[sys.executable,"-m","app.ocr_worker","--engine",engine,"--file",str(path),"--output",str(output),
         "--templates",str(ROOT/"templates"),"--templates",str(root/"templates"),"--language",language]
    # Workers receive runtime paths, not the application's API keys or provider tokens.
    allowed_env={"PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH","SSL_CERT_FILE","SSL_CERT_DIR",
                 "REQUESTS_CA_BUNDLE","TMPDIR","TMP","TEMP","XDG_CACHE_HOME","HF_HOME",
                 "HF_HUB_OFFLINE","HF_HUB_DISABLE_TELEMETRY","DO_NOT_TRACK","PADDLE_PDX_CACHE_HOME"}
    env={k:v for k,v in os.environ.items() if k in allowed_env}
    env["OMP_NUM_THREADS"]="2"
    try:
        proc=subprocess.run(cmd,capture_output=True,text=True,timeout=int(os.getenv("INV_ENGINE_TIMEOUT","240")),env=env)
        if not output.exists():raise ValueError("Reader process failed before producing output")
        result=json.loads(output.read_text())
        if proc.returncode or result.get("error"):raise ValueError(result.get("hint","Reader failed"))
        return result
    except subprocess.TimeoutExpired:
        raise ValueError("Reader timed out. Try another engine or inspect this document manually.") from None
    finally:output.unlink(missing_ok=True)


def process(path,options,store,ai_reader,progress=lambda *args:None):
    trace=[];best=None;best_score=-1;text="";boxes=[];selected="none"
    document_type_hint=None;extraction_note=None
    ai_attempted=False
    def read_ai():
        nonlocal best,best_score,selected,ai_attempted
        ai_attempted=True
        progress(options.provider,"AI is reading the invoice")
        start=time.monotonic()
        try:
            candidate,meta=ai_reader(path,text,options)
            internal_fields=("seller","site","buyer","location","origin","market","taxCode")
            ignored=[field for field in internal_fields if getattr(candidate,field) is not None]
            candidate=candidate.model_copy(update={field:None for field in internal_fields})
            if ignored:meta={**meta,"unverified_internal_fields_ignored":ignored}
            score,missing=quality(candidate)
            has_fields=bool(candidate.lines or any(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines"))
            trace.append({"engine":options.provider,"model":options.model,"status":"extracted" if has_fields else "no_fields","completeness":score,"seconds":round(time.monotonic()-start,2),"extracted_fields":sum(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines"),"line_items":len(candidate.lines),"text_characters":len(text),"method":"vision_ai","reason":"Invoice fields returned; review against the source" if has_fields else "AI returned no invoice fields; another reader or manual entry is required","usage":meta})
            # Each candidate stays intact; never blend conflicting engine values.
            if has_fields and score>=best_score:best=candidate;best_score=score;selected=f"{options.provider} / {options.model}"
            return bool(candidate.number and candidate.lines and candidate.net is not None and all(l.qty is not None and l.price is not None for l in candidate.lines))
        except Exception as e:
            trace.append({"engine":options.provider,"model":options.model,"status":"failed","reason":str(e)[:240],"seconds":round(time.monotonic()-start,2)})
            return False
        finally:
            progress(options.provider,"AI reading finished",{"trace":list(trace),"characters":len(text)})
    installed={x["id"] for x in capabilities() if x["installed"]}
    chain=["invoice2data","paddleocr","docling"] if options.engine=="auto" else ([] if options.engine=="ai" else [options.engine])
    # Text/office attachments and the local Claude bridge need native/OCR text even
    # when AI is selected explicitly. AI still runs after this preparation step.
    if options.engine=="ai":
        if options.provider=="claude_local":chain=["invoice2data","paddleocr","docling"]
        elif path.suffix.lower() in (".docx",".xlsx",".txt",".csv",".json"):chain=["invoice2data"]
    for engine in chain:
        # For scans, a configured vision reader can extract the document directly
        # before paying the CPU cost of a second local OCR pass. On failure the
        # local readers still run. Explicit local-engine choices remain local.
        if engine=="paddleocr" and path.suffix.lower() in (".pdf",".png",".jpg",".jpeg",".tif",".tiff",".webp",".bmp") and options.engine=="auto" and options.ai_fallback and options.model and options.provider!="claude_local" and not ai_attempted:
            if read_ai():
                for remaining in chain[chain.index(engine):]:
                    trace.append({"engine":remaining,"status":"skipped","reason":"The selected vision AI returned invoice fields and item values; review remaining exceptions."})
                break
        start=time.monotonic();progress(engine,"Reading document")
        if engine not in installed:
            trace.append({"engine":engine,"status":"unavailable","reason":"Optional engine is not installed"});continue
        if engine=="paddleocr" and path.suffix.lower() in (".docx",".xlsx",".txt",".csv",".json"):
            trace.append({"engine":engine,"status":"skipped","reason":"Digital office/text document; use native reading or Docling"});continue
        try:
            result=local_read(engine,path,store.root,options.language)
            candidate=Invoice.model_validate(result["invoice"]) if result.get("invoice") else None
            score,missing=quality(candidate)
            if len(result["text"])>len(text):text=result["text"];boxes=result.get("boxes",[])
            trace.append({"engine":engine,"method":result.get("extraction_method","template" if candidate else "text_only"),"status":"extracted" if candidate else "text_only","seconds":round(time.monotonic()-start,2),"completeness":score,"extracted_fields":sum(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines") if candidate else 0,"line_items":len(candidate.lines) if candidate else 0,"text_characters":len(result["text"]),"table_count":len(result.get("tables",[])),"parser_error":result.get("parser_error"),"reason":"Required extraction fields present" if not missing else "; ".join(missing)})
            if candidate is not None and score>best_score:best=candidate;best_score=score;selected=engine
            progress(engine,"Text reading finished",{"trace":list(trace),"characters":len(text)})
            # Native text already exists: another OCR pass cannot supply an
            # unknown supplier's semantic mapping. Offer AI/manual entry promptly.
            if options.engine in ("auto","ai") and engine=="invoice2data" and len(text.strip())>=250:
                po_heading=re.search(r"(?im)^\s*(?:#{1,6}\s*)?Purchase\s+Order\s*$",text)
                po_number=re.search(r"(?i)Store\s+Purchase\s+Order\s*(?:#|No|Number)",text)
                invoice_heading=re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:(?:tax|commercial|sales|supplier)\s+)?Invoice\b",text)
                if po_heading and po_number and not invoice_heading:
                    document_type_hint="possible_purchase_order"
                    extraction_note="This document is labelled Purchase Order. Check the document type before entering invoice fields; a PO does not prove an invoice or receipt."
                elif candidate is None:
                    extraction_note="Document text was read, but no supplier invoice template matched. Use a connected AI reader or enter the fields manually."
                elif missing:
                    extraction_note="Invoice fields were read from the PDF text. Review the remaining missing fields or use AI; another OCR pass cannot supply facts absent from the document."
                for remaining in chain[chain.index(engine)+1:]:
                    trace.append({"engine":remaining,"status":"skipped","reason":"Native text is already readable; a supplier mapping or selected AI is needed, not another OCR pass."})
                break
            if options.engine in ("auto","ai") and engine=="paddleocr" and candidate is not None and candidate.number and candidate.lines:
                for remaining in chain[chain.index(engine)+1:]:
                    trace.append({"engine":remaining,"status":"skipped","reason":"Invoice fields were read. Remaining exceptions go to the selected AI or manual review."})
                break
            if not missing:break
        except Exception as e:
            trace.append({"engine":engine,"status":"failed","reason":str(e)[:240],"seconds":round(time.monotonic()-start,2)})
            progress(engine,"Reader attempt finished",{"trace":list(trace),"characters":len(text)})
    _,missing=quality(best)
    if document_type_hint=="possible_purchase_order":
        trace.append({"engine":options.provider,"status":"skipped","reason":"The document is labelled Purchase Order; invoice extraction needs the supplier invoice."})
    elif not ai_attempted and (options.engine=="ai" or (options.ai_fallback and missing)):
        if not options.model:trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in AI connections"})
        else:read_ai()
    if best is not None and best.lines and best.number and document_type_hint is None:
        extraction_note=None
    return {"invoice":(best or Invoice()).model_dump(mode="json"),"text":text,"boxes":boxes,
            "trace":trace,"selected_engine":selected,"completeness":max(0,best_score),
            "document_type_hint":document_type_hint,"extraction_note":extraction_note}
