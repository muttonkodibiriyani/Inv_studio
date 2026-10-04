import importlib.metadata
import importlib.util
import json
import os
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
    if i is None:return 0,["No supplier template produced structured invoice data"]
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
        net=sum((l.qty*l.price for l in i.lines),Decimal(0))
        if abs(net-i.net)>Decimal("0.01"):missing.append("line amounts do not reconcile with net total")
    total=6+4*len(i.lines)
    return round(max(0,1-len(missing)/total),2),missing


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
    installed={x["id"] for x in capabilities() if x["installed"]}
    chain=["invoice2data","paddleocr","docling"] if options.engine=="auto" else ([] if options.engine=="ai" else [options.engine])
    # Text/office attachments and the local Claude bridge need native/OCR text even
    # when AI is selected explicitly. AI still runs after this preparation step.
    if options.engine=="ai":
        if options.provider=="claude_local":chain=["invoice2data","paddleocr","docling"]
        elif path.suffix.lower() in (".docx",".xlsx",".txt",".csv",".json"):chain=["invoice2data"]
    for engine in chain:
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
            trace.append({"engine":engine,"status":"extracted" if candidate else "text_only","seconds":round(time.monotonic()-start,2),"completeness":score,"reason":"Required extraction fields present" if not missing else "; ".join(missing)})
            if candidate is not None and score>best_score:best=candidate;best_score=score;selected=engine
            if not missing:break
        except Exception as e:
            trace.append({"engine":engine,"status":"failed","reason":str(e)[:240],"seconds":round(time.monotonic()-start,2)})
    _,missing=quality(best)
    if options.engine=="ai" or (options.ai_fallback and missing):
        if not options.model:trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in AI connections"})
        else:
            progress(options.provider,"AI is reading the invoice")
            start=time.monotonic()
            try:
                candidate,meta=ai_reader(path,text,options)
                score,missing=quality(candidate)
                trace.append({"engine":options.provider,"model":options.model,"status":"extracted","completeness":score,"seconds":round(time.monotonic()-start,2),"reason":"AI requested for unread or incomplete fields","usage":meta})
                # Keep a complete candidate as a unit; never silently merge conflicting engines.
                if score>=best_score:best=candidate;best_score=score;selected=f"{options.provider} / {options.model}"
            except Exception as e:
                trace.append({"engine":options.provider,"model":options.model,"status":"failed","reason":str(e)[:240]})
    return {"invoice":(best or Invoice()).model_dump(mode="json"),"text":text,"boxes":boxes,
            "trace":trace,"selected_engine":selected,"completeness":max(0,best_score)}
