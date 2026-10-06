import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from .models import Invoice
from .evidence import build as build_evidence
from . import scan_guard

ROOT=Path(__file__).resolve().parent
# A recovery reader can coexist with its original process briefly. Keep heavy
# local OCR within the deployment's memory budget; native and AI reads continue.
# Two concurrent OCR subprocesses are useful only when a deployment has enough
# CPU as well as memory, so operators must opt in after sizing the service.
try:_LOCAL_OCR_CONCURRENCY=int(os.getenv("INV_LOCAL_OCR_CONCURRENCY","1"))
except ValueError:_LOCAL_OCR_CONCURRENCY=1
_LOCAL_OCR_SLOTS=threading.BoundedSemaphore(max(1,min(2,_LOCAL_OCR_CONCURRENCY)))


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


def _native_reconciliation(i:Invoice,text):
    if i.net is None or not i.lines or not all(line.net_amount is not None for line in i.lines):
        return None
    printed_total=sum((line.net_amount for line in i.lines),Decimal(0))
    if abs(printed_total-i.net)<=Decimal("0.01"):
        return "printed_line_total"
    matches=re.findall(
        r"(?im)^\s*(?:discount|less(?:\s+discount)?)\s*(?:[:\-]\s*)?"
        r"(?:[A-Z]{3}\s*)?(\(?-?[0-9][0-9,]*(?:\.[0-9]{1,3})?\)?)\s*$",
        text,
    )
    if len(matches)!=1:return None
    visible=matches[0]
    negative=visible.startswith("-") or (visible.startswith("(") and visible.endswith(")"))
    try:discount=Decimal(visible.strip("()-").replace(",",""))
    except InvalidOperation:return None
    if negative:return None
    if abs(printed_total-discount-i.net)<=Decimal("0.01"):
        return "explicit_document_discount"
    return None


def native_pdf_quality(i:Invoice | None,text=""):
    """Require internally checkable facts before an embedded-text read skips OCR."""
    if i is None:return False,["no structured invoice"]
    issues=[]
    for field in ("number","currency","net","tax"):
        if getattr(i,field) in (None,""):issues.append(field)
    if not i.lines:issues.append("line items")
    for n,line in enumerate(i.lines,1):
        if not (line.sku or line.gtin):issues.append(f"line {n} item identity")
        for field in ("qty","uom","price","net_amount"):
            if getattr(line,field) in (None,""):issues.append(f"line {n} {field}")
    if i.net is not None and i.lines and all(line.net_amount is not None for line in i.lines):
        if _native_reconciliation(i,text) is None:
            issues.append("printed line amounts do not reconcile with net total or one explicit document discount")
    identityless=[x for x in issues if x.endswith(" item identity")]
    if identityless and len(identityless)<len(i.lines) and len(identityless)==len(issues) and _arithmetic_complete(i):
        # The line prints no code: keep the printed read with the code blank, flagged for review.
        return True,[f"{x.removesuffix(' item identity')} prints no item code; kept blank for review"
                     for x in identityless]
    return not issues,issues


def _arithmetic_complete(i:Invoice):
    """Every printed amount checks: qty x price per line, lines to net, line tax to tax."""
    cent=Decimal("0.01")
    for line in i.lines:
        if line.tax_amount is None:return False
        if (line.qty*line.price).quantize(cent,ROUND_HALF_UP)!=line.net_amount:return False
    return (sum((line.net_amount for line in i.lines),Decimal(0))==i.net
            and sum((line.tax_amount for line in i.lines),Decimal(0))==i.tax)


def local_read(engine,path,root,language="en",extra=()):
    output=root/"work"/(uuid.uuid4().hex+".json")
    timeout=int(os.getenv("INV_ENGINE_TIMEOUT","360" if engine=="paddleocr" else "240"))
    cmd=[sys.executable,"-m","app.ocr_worker","--engine",engine,"--file",str(path),"--output",str(output),
         "--templates",str(ROOT/"templates"),"--templates",str(root/"templates"),"--language",language,
         "--budget-seconds",str(timeout)]
    if os.getenv("INV_STUDIO_LEARN","1")!="0":
        # Learned supplier templates live in the private data directory (app.learned), never in the repo.
        cmd+=["--learned-templates",str(root/"learned"/"templates")]
    cmd+=list(extra)
    # Workers receive runtime paths, not the application's API keys or provider tokens.
    allowed_env={"PATH","HOME","LANG","LC_ALL","LD_LIBRARY_PATH","SSL_CERT_FILE","SSL_CERT_DIR",
                 "REQUESTS_CA_BUNDLE","TMPDIR","TMP","TEMP","XDG_CACHE_HOME","HF_HOME",
                 "HF_HUB_OFFLINE","HF_HUB_DISABLE_TELEMETRY","DO_NOT_TRACK","PADDLE_PDX_CACHE_HOME"}
    env={k:v for k,v in os.environ.items() if k in allowed_env}
    env["OMP_NUM_THREADS"]="2"
    try:
        proc=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,env=env)
        if not output.exists():raise ValueError("Reader process failed before producing output")
        result=json.loads(output.read_text())
        if proc.returncode or result.get("error"):raise ValueError(result.get("hint","Reader failed"))
        return result
    except subprocess.TimeoutExpired:
        raise ValueError("Reader timed out. Try another engine or inspect this document manually.") from None
    finally:output.unlink(missing_ok=True)


def verify_scan(path,root,language="en"):
    """A local OCR pass over a scan the AI read alone: the text layer and word boxes only, as evidence.

    It runs under the same local OCR slots as any reader and never produces invoice values.
    """
    with _LOCAL_OCR_SLOTS:
        result=local_read("paddleocr",path,root,language,extra=("--text-only",))
    return result.get("boxes") or [],result.get("text") or ""


def needs_scan_evidence(result,options):
    """True when the AI read a scan alone: no usable text layer, so its evidence has no boxes yet."""
    return (str(result.get("selected_engine","")).startswith(f"{options.provider} /")
            and len(str(result.get("text") or "").strip())<40 and not result.get("boxes"))


# Fields a reader is recorded for in job.readers (internal enrichment fields are set later by the rules).
READER_HEADER_FIELDS=("number","supplier_name","buyer_name","po","date_printed","date","currency","net","tax")
READER_LINE_FIELDS=("sku","gtin","description","qty","uom","price","net_amount","tax_amount")
AMOUNT_FIELDS={"net","tax","qty","price","net_amount","tax_amount"}
AI_DISAGREEMENT="The AI read a different value here"
AI_REASONS={
    "fallback":"The local readers' result failed the basic checks (no lines, no number or net, or totals that do not reconcile); the AI filled empty fields and every disagreement is flagged",
    "gap_fill":"The local readers left gaps; the AI filled only empty fields and every disagreement is flagged",
    "cross_check":"The local readers read a complete invoice; the AI cross-check flags disagreements and fills optional empty fields",
    "skipped":"The local readers read a complete, reconciled invoice; the AI cross-check is off",
    "off":"AI fallback is off","unavailable":"Select a model in AI connections","selected":"The AI was the selected reader"}


UNRECONCILED="line amounts do not reconcile"


def _open_gaps(invoice,gaps):
    """Quality gaps less an unreconciled-lines gap when quantity x unit price sums to the net (the target
    check's basis): a line-amount column read off by a discount or tax column is not a short read."""
    if invoice is None or invoice.net is None or not invoice.lines:return gaps
    if any(l.qty is None or l.price is None for l in invoice.lines):return gaps
    # Within one unit of the net's printed precision (at least cents), so a three-decimal net must match to 0.001.
    unit=Decimal(1).scaleb(-max(2,-invoice.net.as_tuple().exponent))
    if abs(sum((l.qty*l.price for l in invoice.lines),Decimal(0))-invoice.net)>unit:return gaps
    return [x for x in gaps if not x.startswith(UNRECONCILED)]


def _basic_checks(invoice,missing):
    """The engines' result is usable when it names the invoice, has lines with a net total, and reconciles."""
    if invoice is None or not invoice.lines:return False
    if invoice.number in (None,"") or invoice.net is None:return False
    return not any(x.startswith(UNRECONCILED) for x in missing)


def _same(field,mine,theirs):
    """Two readers agree when amounts are numerically equal or text matches by digits and letters."""
    if field in AMOUNT_FIELDS:
        try:return Decimal(str(mine))==Decimal(str(theirs))
        except (InvalidOperation,ValueError):return False
    fold=lambda v:re.sub(r"[^0-9a-z]","",str(v).casefold())
    return fold(mine)==fold(theirs)


def _aligned(mine,theirs):
    """Two line reads describe the same printed row: same code, or same quantity and price, or same text."""
    for field in ("sku","gtin","description"):
        if mine.get(field) not in (None,"") and theirs.get(field) not in (None,"") and _same(field,mine[field],theirs[field]):
            return True
    return all(mine.get(f) not in (None,"") and theirs.get(f) not in (None,"") and _same(f,mine[f],theirs[f]) for f in ("qty","price"))


def _disagreement(entry,source,other,other_entry):
    """Flag a reader's evidence entry with the AI's differing value and its evidence; the value stays."""
    entry=dict(entry) if entry else {"quote":"","page":None,"source":source}
    review={"reason":AI_DISAGREEMENT,"other_value":None if other is None else str(other)[:300]}
    if other_entry:
        review["other_quote"]=str(other_entry.get("quote") or "")[:300];review["other_page"]=other_entry.get("page")
    entry.setdefault("review",review)
    return entry


def _reader_map(invoice,source):
    """{field: reader} for every filled header field, and one map per line."""
    return ({f:source for f in READER_HEADER_FIELDS if invoice.get(f) not in (None,"")},
            [{f:source for f in READER_LINE_FIELDS if line.get(f) not in (None,"")} for line in invoice.get("lines") or []])


def _lines_reconcile(lines,net):
    """Every line has a net amount and they sum to the invoice net within 0.01."""
    try:
        amounts=[Decimal(str(line.get("net_amount"))) for line in lines if line.get("net_amount") not in (None,"")]
        return net not in (None,"") and len(amounts)==len(lines) and abs(sum(amounts,Decimal(0))-Decimal(str(net)))<=Decimal("0.01")
    except (InvalidOperation,ValueError):return False


def _engine_lines_open(engine):
    """The engines read no lines, or lines that do not reconcile with the net they read."""
    if not engine.get("lines"):return True
    try:invoice=Invoice.model_validate(engine)
    except ValueError:return True
    return any(x.startswith(UNRECONCILED) for x in _open_gaps(invoice,quality(invoice)[1]))


def merge_ai_fields(engine,ai,engine_evidence,ai_evidence,source,mode):
    """Fill the engines' empty fields from the AI read and flag every disagreement; never overwrite a value.

    ``engine`` and ``ai`` are invoice dicts. Lines merge pairwise when both reads have the same count and the
    pair aligns; in ``fallback``, when the engines' lines do not sum to their net, the AI's lines stand in if the
    counts differ and the AI's lines sum to the engines' net; otherwise the local lines are kept. Returns (invoice, evidence, header readers, line readers, notes).
    """
    merged=dict(engine)
    evidence={"header":dict((engine_evidence or {}).get("header") or {}),"lines":[dict(x) for x in ((engine_evidence or {}).get("lines") or [])]}
    ai_header=(ai_evidence or {}).get("header") or {};ai_lines_ev=(ai_evidence or {}).get("lines") or []
    header_readers,line_readers=_reader_map(engine,source);notes=[]
    for field in READER_HEADER_FIELDS:
        mine,theirs=engine.get(field),ai.get(field)
        if theirs in (None,""):continue
        if mine in (None,""):
            merged[field]=theirs;header_readers[field]="ai"
            if ai_header.get(field):evidence["header"][field]=ai_header[field]
        elif not _same(field,mine,theirs):
            evidence["header"][field]=_disagreement(evidence["header"].get(field),source,theirs,ai_header.get(field))
    engine_lines=list(engine.get("lines") or []);ai_lines=list(ai.get("lines") or [])
    while len(evidence["lines"])<len(engine_lines):evidence["lines"].append({})
    # The AI lines stand in only for local lines that do not reconcile (a fallback for a missing number keeps
    # reconciled lines), and only against the engines' own net, never one the AI filled in this merge.
    stand_in=(mode=="fallback" and _engine_lines_open(engine) and len(ai_lines)!=len(engine_lines)
              and _lines_reconcile(ai_lines,engine.get("net")))
    if ai_lines and (not engine_lines or stand_in):
        merged["lines"]=ai_lines;evidence["lines"]=[dict(x) for x in ai_lines_ev]
        line_readers=_reader_map(ai,"ai")[1]
        notes.append(f"The AI read {len(ai_lines)} lines where the local readers read {len(engine_lines)}"
                     +("; the local lines do not sum to the net, so the AI lines are shown" if engine_lines else ""))
    elif ai_lines and len(ai_lines)!=len(engine_lines):
        notes.append(f"The AI read {len(ai_lines)} lines where the local readers read {len(engine_lines)}; the local lines are kept"
                     +("; the AI lines do not sum to the local net either" if mode=="fallback" and _engine_lines_open(engine)
                       else ""))
    elif ai_lines:
        merged["lines"]=[]
        for n,(mine_line,their_line) in enumerate(zip(engine_lines,ai_lines)):
            line=dict(mine_line);their_ev=ai_lines_ev[n] if n<len(ai_lines_ev) else {}
            aligned=_aligned(mine_line,their_line)
            if not aligned:notes.append(f"Line {n+1}: the AI line does not align with the local line; nothing filled")
            for field in READER_LINE_FIELDS:
                mine,theirs=mine_line.get(field),their_line.get(field)
                if theirs in (None,""):continue
                if mine in (None,""):
                    if not aligned:continue
                    # A printed barcode that failed its check digit is never replaced by the AI's digits.
                    if field=="gtin" and mine_line.get("barcode_unchecked"):continue
                    line[field]=theirs;line_readers[n][field]="ai"
                    if their_ev.get(field):evidence["lines"][n][field]=their_ev[field]
                elif field!="description" and not _same(field,mine,theirs):
                    evidence["lines"][n][field]=_disagreement(evidence["lines"][n].get(field),source,theirs,their_ev.get(field))
            merged["lines"].append(line)
    return merged,evidence,header_readers,line_readers,notes


def _review(entry,line,source,reason,other):
    """An evidence entry carrying this reader's review reason; the value it names is the reader's, not the invoice's."""
    entry=dict(entry) if entry else {"quote":"","page":(line or {}).get("page"),"source":source}
    entry["review"]={"reason":reason,"other_value":None if other in (None,"") else str(other)[:300]}
    return entry


def guard_lines(invoice,evidence,line_readers,source,scan,ocr_boxes):
    """Barcodes as checked digits only; on a scan an AI barcode stands only where the page OCR prints it.

    A local barcode that fails the check digit moves to barcode_unchecked; an incomplete one is cleared. An AI
    barcode is cleared when it fails the check digit or, on a scan, when the same page's OCR does not print the
    same complete digit run as often as lines claim it. A cleared value survives only as evidence.
    """
    lines=invoice.get("lines") or []
    rows=evidence.setdefault("lines",[])
    while len(rows)<len(lines):rows.append({})
    while len(line_readers)<len(lines):line_readers.append({})
    claims=[]
    for n,line in enumerate(lines):
        printed=line.get("gtin")
        if printed in (None,""):
            # A reader that already moved a failed-check code aside still owes the review flag.
            if line.get("barcode_unchecked") and not (rows[n].get("gtin") or {}).get("review"):
                rows[n]["gtin"]=_review(rows[n].get("gtin"),line,source,scan_guard.MISPRINT,line["barcode_unchecked"])
            continue
        gtin,unchecked,reason=scan_guard.classify_barcode(printed)
        from_ai=line_readers[n].get("gtin")=="ai"
        line["gtin"]=gtin
        if not from_ai and unchecked:line["barcode_unchecked"]=unchecked
        if gtin is None:
            line_readers[n].pop("gtin",None)
            rows[n]["gtin"]=_review(rows[n].get("gtin"),line,"ai" if from_ai else source,
                                    scan_guard.UNREADABLE if from_ai and unchecked else reason,printed)
        elif from_ai and scan:
            claims.append((n,line.get("page"),gtin))
    if claims:
        confirmed=scan_guard.corroborate(claims,scan_guard.page_occurrences(ocr_boxes))
        for n,_,digits in claims:
            if n in confirmed:continue
            lines[n]["gtin"]=None;line_readers[n].pop("gtin",None)
            rows[n]["gtin"]=_review(rows[n].get("gtin"),lines[n],"ai",scan_guard.UNCONFIRMED,digits)
    return invoice,evidence


def guard_header(invoice,evidence,header_readers,source,text):
    """PO only from a PO label, never a P.O. Box; an absent VAT is flagged; inclusive VAT lines are one flag."""
    header=evidence.setdefault("header",{})
    po=invoice.get("po")
    if po not in (None,"") and scan_guard.po_from_box(po,[(header.get("po") or {}).get("quote")],text):
        invoice["po"]=None;header_readers.pop("po",None)
        header["po"]=_review(header.get("po"),None,source,"a P.O. Box in an address is not a PO number",po)
    if invoice.get("tax") in (None,""):
        if not (header.get("tax") or {}).get("review"):
            header["tax"]=_review(header.get("tax"),None,source,scan_guard.missing_tax_reason(text),None)
            header["tax"]["review"]["code"]=scan_guard.missing_tax_code(text)
    else:
        count=scan_guard.inclusive_vat_lines(invoice.get("lines"))
        if count and not (header.get("tax") or {}).get("review"):
            header["tax"]=_review(header.get("tax"),None,source,f"{count} lines: {scan_guard.INCLUSIVE_VAT}",None)
    return invoice,evidence


def _absent_fields(evidence):
    """Job-level read-time facts for empty header fields: absent_fields (not printed) and each field's reason_code."""
    codes={field:entry["review"]["code"] for field,entry in ((evidence or {}).get("header") or {}).items()
           if isinstance(entry,dict) and (entry.get("review") or {}).get("code")}
    return {"absent_fields":[f for f,c in codes.items() if c==scan_guard.TAX_ABSENT_CODE],"reason_codes":codes}


def process(path,options,store,ai_reader,progress=lambda *args:None):
    trace=[];best=None;best_score=-1;text="";boxes=[];selected="none";best_evidence={};best_source="native"
    document_type_hint=None;extraction_note=None;native_review=[]
    ai_attempted=False;invoice2data_ocr=False;ai_calls=0;ai_status="off";ai_notes=[];readers_maps=None
    # A scan (no usable text layer) is told by the PDF's own text; its OCR boxes corroborate AI barcodes.
    native_chars=None;ocr_boxes=[];scan=False;ai_reason=None
    # The text and boxes of the selected local read: the target check re-checks its values against them.
    selected_page=None
    cross_check=bool(getattr(options,"ai_cross_check",False)) or os.getenv("INV_STUDIO_AI_CROSS_CHECK","0")=="1"
    def call_ai(role):
        """One AI read: (candidate, its own header evidence, completeness) with a trace entry, or None."""
        nonlocal ai_attempted,ai_calls
        ai_attempted=True;ai_calls+=1
        progress(options.provider,"AI is reading the invoice")
        start=time.monotonic()
        try:
            candidate,meta=ai_reader(path,text,options)
            meta=dict(meta or {});ai_evidence=meta.pop("evidence",None) or {}
            internal_fields=("seller","site","buyer","location","origin","market","taxCode")
            ignored=[field for field in internal_fields if getattr(candidate,field) is not None]
            candidate=candidate.model_copy(update={field:None for field in internal_fields})
            candidate=candidate.model_copy(update={"lines":[line.model_copy(update={"item_id":None}) for line in candidate.lines]})
            if ignored:meta={**meta,"unverified_internal_fields_ignored":ignored}
            score,missing=quality(candidate)
            has_fields=bool(candidate.lines or any(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines"))
            trace.append({"engine":options.provider,"model":options.model,"status":"extracted" if has_fields else "no_fields","completeness":score,"seconds":round(time.monotonic()-start,2),"extracted_fields":sum(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines"),"line_items":len(candidate.lines),"text_characters":len(text),"method":"vision_ai","role":role,"reason":"Invoice fields returned; review against the source" if has_fields else "AI returned no invoice fields; another reader or manual entry is required","usage":meta})
            return (candidate,ai_evidence,score) if has_fields else None
        except Exception as e:
            trace.append({"engine":options.provider,"model":options.model,"status":"failed","role":role,"reason":str(e)[:240],"seconds":round(time.monotonic()-start,2)})
            return None
        finally:
            progress(options.provider,"AI reading finished",{"trace":list(trace),"characters":len(text)})
    def read_ai():
        """The AI as the selected reader: its whole candidate, kept when it reads strictly more than a local pass."""
        nonlocal best,best_score,selected,best_evidence,best_source,ai_status,selected_page
        read=call_ai("selected")
        if read is None:
            ai_status="failed";return
        candidate,ai_evidence,score=read;ai_status="selected"
        # A local read is kept on a completeness tie; the AI must read strictly more.
        if score>best_score:
            best=candidate;best_score=score;selected=f"{options.provider} / {options.model}";best_source="ai";selected_page=None
            best_evidence=build_evidence(candidate.model_dump(mode="json"),boxes,"ai",header=ai_evidence)
    def merge_ai(mode):
        """The AI after the local engines: fills empty fields, flags disagreements, never overwrites."""
        nonlocal best,best_score,selected,best_evidence,best_source,ai_status,ai_notes,readers_maps,ai_reason
        read=call_ai(mode)
        if read is None:
            ai_status="failed";return
        candidate,ai_evidence,score=read
        ai_dict=candidate.model_dump(mode="json");ai_ev=build_evidence(ai_dict,boxes,"ai",header=ai_evidence)
        ai_status=mode
        local=best.model_dump(mode="json") if best is not None else None
        weak=scan_guard.weak_read(local) if scan else []
        if weak:
            # A weak local read of a scan: the AI read from the page image stands in when it passes its own
            # checks; when it does not either, nothing is filled and the invoice is not read.
            failed=scan_guard.ai_self_check(ai_dict)
            if failed:
                ai_status="not_read"
                ai_reason=("Not read: the local OCR read is weak ("+"; ".join(weak)+") and the AI read fails its checks ("
                           +"; ".join(failed)+"). Nothing was filled; enter the invoice manually.")
                return
            merged=dict(local or Invoice().model_dump(mode="json"))
            header_readers=_reader_map(local,best_source)[0] if local else {}
            evidence={"header":dict((best_evidence or {}).get("header") or {}) if local else {},"lines":[]}
            replaced=[]
            ai_header=ai_ev.get("header") or {}
            for field in READER_HEADER_FIELDS:
                mine,theirs=merged.get(field),ai_dict.get(field)
                # A money total stands in only with the quote it was read from: no derived net or tax, no 0 for unprinted.
                if field in ("net","tax") and not str((ai_header.get(field) or {}).get("quote") or "").strip():continue
                if theirs in (None,"") or (mine not in (None,"") and _same(field,mine,theirs)):continue
                merged[field]=theirs;header_readers[field]="ai";replaced.append(field)
                evidence["header"][field]={**(ai_header.get(field) or {"quote":"","page":None}),
                                           "source":"ai","origin":scan_guard.AI_PAGE_IMAGE}
            merged["lines"]=ai_dict.get("lines") or []
            evidence["lines"]=[{k:{**v,"origin":scan_guard.AI_PAGE_IMAGE} for k,v in row.items()}
                               for row in ai_ev.get("lines") or []]
            best=Invoice.model_validate(merged);best_score=quality(best)[0];best_evidence=evidence
            selected=f"{options.provider} / {options.model}"
            readers_maps=(header_readers,_reader_map(ai_dict,"ai")[1])
            ai_notes=[f"The local OCR read is weak ({'; '.join(weak)}); the AI read from the page image passed its checks, "
                      f"so its {len(merged['lines'])} lines and {len(replaced)} header fields replace the local read"]
            return
        if best is None:
            best=candidate;best_score=score;selected=f"{options.provider} / {options.model}";best_source="ai";best_evidence=ai_ev
            return
        merged,evidence,header_readers,line_readers,notes=merge_ai_fields(best.model_dump(mode="json"),ai_dict,best_evidence,ai_ev,best_source,mode)
        best=Invoice.model_validate(merged);best_score=quality(best)[0];best_evidence=evidence
        readers_maps=(header_readers,line_readers);ai_notes=notes
    installed={x["id"] for x in capabilities() if x["installed"]}
    chain=["invoice2data","paddleocr","docling"] if options.engine=="auto" else ([] if options.engine=="ai" else [options.engine])
    native_preflight_engine=None
    if (path.suffix.lower()==".pdf" and options.engine in ("paddleocr","docling")
            and getattr(options,"prefer_native_text",True)):
        native_preflight_engine=options.engine
        chain=["native_pdf_text",options.engine]
    if options.engine=="invoice2data" and path.suffix.lower() in (".png",".jpg",".jpeg",".tif",".tiff",".webp",".bmp"):
        invoice2data_ocr=True;chain=["paddleocr"]
        trace.append({"engine":"invoice2data","status":"needs_ocr","reason":"This image needs local OCR input. PaddleOCR will read it before invoice2data templates and structured parsing."})
    # Text/office attachments and the local Claude bridge need native/OCR text even
    # when AI is selected explicitly. AI still runs after this preparation step.
    if options.engine=="ai":
        if options.provider=="claude_local":chain=["invoice2data","paddleocr","docling"]
        elif path.suffix.lower() in (".docx",".xlsx",".txt",".csv",".json"):chain=["invoice2data"]
    for engine in chain:
        reader_engine="invoice2data" if engine=="native_pdf_text" else engine
        # Local engines first on every invoice: a scan goes through the local OCR readers before the AI
        # sees it. The AI stage after the chain fills gaps and flags disagreements; it never runs ahead.
        start=time.monotonic();queue_seconds=None
        progress(engine,"Checking embedded PDF text" if engine=="native_pdf_text" else "Reading document")
        if reader_engine not in installed:
            trace.append({"engine":engine,"status":"unavailable","reason":"Optional engine is not installed"});continue
        if engine=="paddleocr" and path.suffix.lower() in (".docx",".xlsx",".txt",".csv",".json"):
            trace.append({"engine":engine,"status":"skipped","reason":"Digital office/text document; use native reading or Docling"});continue
        try:
            if engine in ("paddleocr","docling"):
                queue_started=time.monotonic()
                progress(engine,"Waiting for the local OCR reader")
                with _LOCAL_OCR_SLOTS:
                    queue_seconds=time.monotonic()-queue_started
                    start=time.monotonic()
                    progress(engine,"Reading document")
                    result=local_read(reader_engine,path,store.root,options.language)
            else:
                result=local_read(reader_engine,path,store.root,options.language)
            candidate=Invoice.model_validate(result["invoice"]) if result.get("invoice") else None
            score,missing=quality(candidate)
            if len(result["text"])>len(text):text=result["text"];boxes=result.get("boxes",[])
            native_suitable,native_issues=(native_pdf_quality(candidate,result["text"])
                if engine=="native_pdf_text" else (False,[]))
            native_reconciliation=(_native_reconciliation(candidate,result["text"])
                if native_suitable else None)
            if engine=="invoice2data" and options.engine=="auto" and path.suffix.lower()==".pdf":
                # The auto chain reads the PDF's own text here; the same gate applies to an unprinted code.
                kept,notes=native_pdf_quality(candidate,result["text"])
                if kept and notes:
                    native_review=notes;missing=[x for x in missing if not x.endswith(" item identity")]
            reason=("Embedded PDF text has complete line facts reconciled through the document's explicit discount"
                if native_reconciliation=="explicit_document_discount" else
                "Embedded PDF text passes every arithmetic check; "+"; ".join(native_issues)
                if native_suitable and native_issues else
                "Embedded PDF text has complete, reconciled line facts"
                if native_suitable else "; ".join(native_issues)
                if engine=="native_pdf_text" else
                "Embedded PDF text passes every arithmetic check; "+"; ".join(native_review)
                if engine=="invoice2data" and native_review else
                "Required extraction fields present" if not missing else "; ".join(missing))
            trace_entry={"engine":engine,"method":result.get("extraction_method","template" if candidate else "text_only"),"status":"extracted" if candidate else "text_only","seconds":round(time.monotonic()-start,2),"completeness":score,"extracted_fields":sum(v not in (None,"") for k,v in candidate.model_dump().items() if k!="lines") if candidate else 0,"line_items":len(candidate.lines) if candidate else 0,"text_characters":len(result["text"]),"table_count":len(result.get("tables",[])),"parser_error":result.get("parser_error"),"reason":reason}
            if queue_seconds is not None:trace_entry["queue_seconds"]=round(queue_seconds,2)
            if engine in ("paddleocr","docling"):
                trace_entry["ocr_status"]="ok";trace_entry["tokens_per_page"]=scan_guard.tokens_per_page(result.get("boxes"))
                if not ocr_boxes and result.get("boxes"):ocr_boxes=result["boxes"]
            elif engine in ("invoice2data","native_pdf_text") and not invoice2data_ocr and native_chars is None:
                native_chars=len(str(result.get("text") or "").strip())
            trace.append(trace_entry)
            if result.get("recovery"):
                trace[-1]["recovery"]=result["recovery"]
            if candidate is not None and (score>best_score or (
                    score==best_score and engine==native_preflight_engine)):
                best=candidate;best_score=score
                selected=("native PDF text" if engine=="native_pdf_text" else
                    "invoice2data + PaddleOCR" if invoice2data_ocr and engine=="paddleocr" else engine)
                best_source="ocr" if engine=="paddleocr" else "native"
                best_evidence=build_evidence(result["invoice"],result.get("boxes",[]),best_source)
                selected_page=(result["text"],result.get("boxes",[]))
            progress(engine,"Text reading finished",{"trace":list(trace),"characters":len(text)})
            if engine=="native_pdf_text":
                if native_suitable:
                    native_review=native_issues
                    trace.append({"engine":native_preflight_engine,"status":"skipped",
                        "reason":"Embedded PDF text passed the structured quality checks; the selected OCR reader did not run. Disable native-text preference to force OCR."})
                    break
                continue
            if options.engine=="invoice2data" and engine=="invoice2data" and path.suffix.lower()==".pdf" and len(result["text"].strip())<40 and not (candidate and candidate.lines):
                invoice2data_ocr=True;chain.append("paddleocr")
                trace.append({"engine":"invoice2data","status":"needs_ocr","reason":"This PDF has no usable text layer. PaddleOCR will provide local OCR input for invoice2data templates and structured parsing."})
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
            # Lines that do not sum to the net (a table read short of the printed rows) are not a stopping
            # point: the next local reader still runs and the existing score keeps the better read.
            unreconciled=any(x.startswith(UNRECONCILED) for x in _open_gaps(candidate,missing))
            if options.engine in ("auto","ai") and engine=="paddleocr" and candidate is not None and candidate.number and candidate.lines and not unreconciled:
                for remaining in chain[chain.index(engine)+1:]:
                    trace.append({"engine":remaining,"status":"skipped","reason":"Invoice fields were read. Remaining exceptions go to the selected AI or manual review."})
                break
            if not missing:break
        except Exception as e:
            trace.append({"engine":engine,"status":"failed","reason":str(e)[:240],"seconds":round(time.monotonic()-start,2),"queue_seconds":round(queue_seconds,2) if queue_seconds is not None else None})
            if engine in ("paddleocr","docling"):trace[-1]["ocr_status"]="timed_out" if "timed out" in str(e) else "failed"
            progress(engine,"Reader attempt finished",{"trace":list(trace),"characters":len(text)})
    def gaps_of(invoice):
        _,gaps=quality(invoice)
        if native_review and selected in ("native PDF text","invoice2data"):
            # The gate already checked every amount; an unprinted code is for review, not for the AI to supply.
            gaps=[x for x in gaps if not x.endswith(" item identity")]
        return _open_gaps(invoice,gaps)
    scan=scan_guard.is_scan(native_chars,path)
    if best is not None:
        # The local read's barcodes are checked before the AI sees the gaps: a misprint is not a gap to fill.
        local=best.model_dump(mode="json");local_readers=_reader_map(local,best_source)[1]
        local,best_evidence=guard_lines(local,dict(best_evidence or {}),local_readers,best_source,scan,ocr_boxes)
        best=Invoice.model_validate(local);best_score=quality(best)[0]
    missing=gaps_of(best);ai_reason=None
    # What the engines' result asks of the AI, recorded whether or not the AI runs, so AI calls per invoice
    # can be counted on an AI-off run: fallback when the engines could not read, gap fill when they left gaps,
    # cross-check (an extra call on a complete invoice) only when the owner switched it on.
    need=None if document_type_hint=="possible_purchase_order" else (
        "fallback" if not _basic_checks(best,missing) else "gap_fill" if missing else "cross_check")
    if document_type_hint=="possible_purchase_order":
        trace.append({"engine":options.provider,"status":"skipped","reason":"The document is labelled Purchase Order; invoice extraction needs the supplier invoice."})
        ai_status,ai_reason="skipped",trace[-1]["reason"]
    elif options.engine=="ai":
        if not options.model:
            trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in AI connections"});ai_status="unavailable"
        elif not ai_attempted:read_ai()
    elif options.ai_fallback:
        if not options.model:
            if missing:trace.append({"engine":options.provider,"status":"needs_connection","reason":"Select a model in AI connections"})
            ai_status="unavailable" if missing else "skipped"
            if not missing:ai_reason="The local readers read a complete, reconciled invoice; no model is selected"
        elif need=="cross_check" and not cross_check:ai_status="skipped"
        else:merge_ai(need)
    if ai_status=="failed":ai_reason=str(trace[-1].get("reason") or "The AI read failed")[:240]
    if ai_status=="not_read":extraction_note=ai_reason
    if best is not None:
        final=best.model_dump(mode="json")
        header_map,line_map=readers_maps or _reader_map(final,best_source)
        final,best_evidence=guard_lines(final,dict(best_evidence or {}),line_map,best_source,scan,ocr_boxes)
        final,best_evidence=guard_header(final,best_evidence,header_map,best_source,text)
        best=Invoice.model_validate(final);best_score=quality(best)[0];readers_maps=(header_map,line_map)
    ai_record={"status":ai_status,"reason":ai_reason or AI_REASONS.get(ai_status,""),"calls":ai_calls,"need":need}
    if ai_notes:ai_record["notes"]=ai_notes
    invoice=(best or Invoice()).model_dump(mode="json")
    header_readers,line_readers=readers_maps or _reader_map(invoice,best_source if best is not None else "native")
    readers={"ai":ai_record,"header":header_readers,"lines":line_readers,"gaps":gaps_of(best)}
    if best is not None and best.lines and best.number and document_type_hint is None and ai_status!="not_read":
        extraction_note=None
    if native_review and selected in ("native PDF text","invoice2data"):
        extraction_note="Read from the PDF text; every printed amount checks. Review: "+"; ".join(native_review)+"."
    if selected_page is not None:
        # A later reader that lost on score (e.g. Docling after an unreconciled PaddleOCR read) must not
        # replace the selected reader's text and boxes.
        text,boxes=selected_page
    return {"invoice":invoice,"text":text,"boxes":boxes,
            "trace":trace,"selected_engine":selected,"completeness":max(0,best_score),
            "document_type_hint":document_type_hint,"extraction_note":extraction_note,
            "evidence":best_evidence if best is not None else {},"readers":readers,**_absent_fields(best_evidence if best is not None else {})}
