import hashlib
import io
import json
import os
import re
import shutil
import threading
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, SecretStr, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from .authentication import CloudIdentity, actor
from .engines import capabilities, process
from .excel import workbook, batch_workbook
from .extraction_draft import extraction_workbook, extraction_batch_workbook
from .drafts import ManualDraftRequest, build_draft_workbook, draft_filename, draft_public_metadata
from .deletion import DeletionError, delete_invoices
from .reference_lookup import ReferenceLookup
from .product_candidates import ProductCandidates
from .matching import enrich, key, validate
from .models import Invoice, Policy, ProcessingOptions, StrictModel
from .oauth import ChatGPTAuth
from .providers import Providers
from .references import import_references, reference_workbook
from .store import Store

ROOT=Path(__file__).resolve().parent.parent
MIME_XLSX="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD=12_000_000


class Review(StrictModel):
    invoice: Invoice
    revision: int
    confirm: bool = False


class Revision(StrictModel):
    revision: int


class ExtractionDraftRequest(Revision):
    acknowledge_unvalidated: bool = Field(default=False,strict=True)


class BatchItem(Revision):
    id: str


class Batch(StrictModel):
    jobs: list[BatchItem] = Field(min_length=1,max_length=100)


class ExtractionBatch(Batch):
    acknowledge_unvalidated: bool = Field(default=False,strict=True)


class DeleteItem(StrictModel):
    id: str = Field(min_length=1,max_length=200)
    revision: int = Field(ge=1,strict=True)


class DeleteJobsRequest(StrictModel):
    jobs: list[DeleteItem] = Field(min_length=1,max_length=100)
    confirm_permanent: bool = Field(default=False,strict=True)


class Retry(StrictModel):
    options: ProcessingOptions
    preflight_token: str


class Connection(StrictModel):
    api_key: SecretStr = Field(min_length=10,max_length=500)


class SubscriptionToken(StrictModel):
    setup_token: SecretStr = Field(min_length=20,max_length=4096)


class ChatGPTBundle(StrictModel):
    bundle: dict


class Settings(StrictModel):
    provider: str = "openai"
    model: str = Field(default="",max_length=150,pattern=r"^[A-Za-z0-9._:/-]*$")
    ai_fallback: bool = True


class Account(StrictModel):
    id: str


class SignIn(StrictModel):
    account_id: str | None = None


class FilePlan(StrictModel):
    name: str = Field(min_length=1,max_length=300)
    size: int = Field(ge=1,le=MAX_UPLOAD)


class Preflight(StrictModel):
    options: ProcessingOptions
    files: list[FilePlan] = Field(min_length=1,max_length=100)


class PreflightToken(StrictModel):
    token: str


class DemoRequest(StrictModel):
    preflight_token: str


def create_app(data_dir=None):
    app=FastAPI(title="Inv Studio",version="0.1.0")
    identity=CloudIdentity()
    store_type=Store
    if identity.cloud:
        from .cloud_store import PostgresStore
        store_type=PostgresStore
    store=store_type(Path(data_dir or os.getenv("INV_STUDIO_DATA",ROOT/".data")))
    if identity.cloud:store.sync_templates()
    auth=ChatGPTAuth(store);providers=Providers(store,auth)
    lookup=ReferenceLookup(store)
    products=ProductCandidates(lookup)
    pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="invoice")
    slots=threading.BoundedSemaphore(20)
    plans={};plans_lock=threading.RLock()
    app.state.store=store;app.state.providers=providers;app.state.auth=auth;app.state.pool=pool
    allowed_origins={x.strip() for x in os.getenv("INV_STUDIO_ALLOWED_ORIGINS","").split(",") if x.strip()}
    hosts=["127.0.0.1","localhost","testserver"]
    if identity.cloud:hosts += ["*.run.app"]+[urlparse(x).netloc for x in allowed_origins]
    app.state.identity=identity
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)
    # Recovery must inspect all persisted jobs, not the 200-entry UI history.
    with store.connection(True) as connection:
        for row in connection.execute("SELECT id,payload FROM jobs"):
            job=json.loads(row["payload"])
            if job["status"] in ("processing","queued"):
                job.update(status="error",error="The server restarted during processing. Retry this invoice.")
                store.job(job["id"],job,connection)

    @app.middleware("http")
    async def local_guard(request,call_next):
        claims=None
        if identity.cloud and request.url.path.startswith("/api/") and request.url.path!="/api/public-config":
            try:claims=await run_in_threadpool(identity.verify,request.headers.get("authorization"))
            except PermissionError as exc:return JSONResponse({"detail":str(exc)},403)
            except ValueError as exc:return JSONResponse({"detail":str(exc)},401)
            request.state.identity=claims
        if request.method not in ("GET","HEAD","OPTIONS"):
            if request.headers.get("x-studio-request")!="1":return JSONResponse({"detail":"Missing request protection header"},403)
            origin=request.headers.get("origin")
            if origin and urlparse(origin).netloc!=request.headers.get("host") and origin not in allowed_origins:
                return JSONResponse({"detail":"Cross-origin requests are not accepted"},403)
            try:size=int(request.headers.get("content-length","0"))
            except ValueError:return JSONResponse({"detail":"Invalid content length"},400)
            if size>MAX_UPLOAD+200000:return JSONResponse({"detail":"Maximum upload size is 12 MB"},413)
        actor_token=actor.set(claims["uid"] if claims else "local-operator")
        try:response=await call_next(request)
        finally:actor.reset(actor_token)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["Cache-Control"]="no-store"
        response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; frame-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if identity.cloud:
            auth_domain=os.getenv("INV_STUDIO_FIREBASE_AUTH_DOMAIN","")
            response.headers["Content-Security-Policy"]=("default-src 'self'; script-src 'self' https://apis.google.com; style-src 'self'; img-src 'self' data: blob: https://*.googleusercontent.com; frame-src 'self' blob: https://"+auth_domain+"; connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://www.googleapis.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def invalid_request(_,exc):
        # Pydantic's default error includes the submitted input. Never echo a key.
        return JSONResponse({"detail":"Invalid input: "+"; ".join(".".join(map(str,e["loc"]))+": "+e["msg"] for e in exc.errors())},422)

    @app.exception_handler(ValueError)
    async def bad_value(_,exc):return JSONResponse({"detail":str(exc)[:400]},400)

    def references(c=None):return store.get("references",{},c)
    def policy(c=None):return Policy.model_validate(store.get("policy",{},c))
    def rule_signature(opts):
        return hashlib.sha256(json.dumps({"options":opts.model_dump(),"references":references().get("version"),"policy":policy().model_dump(mode="json")},sort_keys=True).encode()).hexdigest()
    def require_plan(token,opts,filename,size):
        with plans_lock:
            plan=plans.get(token)
            if not plan or plan["expires"]<time.time() or not plan["confirmed"]:
                raise HTTPException(409,"Review and confirm the processing rules before running this invoice")
            if plan["signature"]!=rule_signature(opts):raise HTTPException(409,"Rules or references changed. Review the processing plan again.")
            match=next((x for x in plan["files"] if x["name"]==filename and x["size"]==size),None)
            if not match:raise HTTPException(409,"This file is not in the confirmed processing plan")
            return plan,match
    def job_or_404(jid,c=None):
        j=store.job(jid,c=c)
        if not j:raise HTTPException(404,"Invoice not found")
        return j
    def assert_editable(j):
        if j.get("export_id"):raise HTTPException(409,"This invoice was exported. Download its existing export.")
        if j["status"] in ("queued","processing"):raise HTTPException(409,"Wait for extraction to finish")
    def evaluate(j,c=None,validation_context=None):
        inv=Invoice.model_validate(j["invoice"])
        if validation_context is None:
            refs=references(c);rules=policy(c)
            if c:led=store.ledger(c)
            else:
                with store.connection() as conn:led=store.ledger(conn)
        else:refs,rules,led=validation_context
        if j.get("export_id"):led=[x for x in led if x.get("job_id")!=j["id"]]
        j["validation"]=validate(inv,refs,rules,led,j.get("reviewed",False))
        if not j.get("export_id") and j["status"] not in ("queued","processing","error"):
            j["status"]="ready" if j["validation"]["ready"] else "review"
        return j

    def run(jid,opts):
        try:
            with plans_lock:
                job=job_or_404(jid)
                if job.get("confirmed_signature") != rule_signature(opts):
                    raise ValueError("Confirmed rules changed")
                job.update(status="processing",error=None);store.job(jid,job)
            def progress(engine,message,details=None):
                j=job_or_404(jid)
                previous=j.get("progress") or {}
                j["progress"]={"engine":engine,"message":message,
                    "started_at":previous.get("started_at") if previous.get("engine")==engine else datetime.now(timezone.utc).isoformat()}
                if details:
                    j["trace"]=details.get("trace",j.get("trace",[]))
                    j["progress"]["characters"]=details.get("characters",0)
                store.job(jid,j)
            if identity.cloud:store.ensure_blob(Path(job["path"]))
            result=process(Path(job["path"]),opts,store,providers.extract,progress)
            with plans_lock,store.connection(True) as c:
                job=job_or_404(jid,c)
                unchanged=job.get("confirmed_signature")==rule_signature(opts)
                raw=Invoice.model_validate(result["invoice"])
                header_fields=sum(getattr(raw,name) not in (None,"") for name in Invoice.model_fields if name!="lines")
                extracted_lines=sum(any(value not in (None,"") for value in line.model_dump().values()) for line in raw.lines)
                outcome="fields_extracted" if header_fields or extracted_lines else "text_read" if result.get("text","").strip() else "extraction_failed"
                inv,provenance=enrich(raw,references(c)) if unchanged else (raw,[])
                job.update(result);job.update(invoice=inv.model_dump(mode="json"),provenance=provenance,
                            extraction_status=outcome,
                            status="review" if unchanged else "error",reviewed=False,progress=None,revision=job["revision"]+1,
                            error=None if unchanged else "Rules or references changed during extraction. Review a new processing plan and retry.")
                evaluate(job,c);store.job(jid,job,c)
                store.audit(outcome,{"job_id":jid,"engine":job["selected_engine"],"trace":job["trace"],
                            "header_fields":header_fields,"line_items":extracted_lines,
                            "text_characters":len(result.get("text","")),"approved":False,
                            "confirmed_rules_unchanged":unchanged},c)
        except Exception as exc:
            job=job_or_404(jid);job.update(status="error",error=f"Processing failed ({type(exc).__name__}). Retry with another engine or review the document.",progress=None)
            store.job(jid,job)
            store.audit("extraction_failed",{"job_id":jid,"error_type":type(exc).__name__,"approved":False})
        finally:slots.release()

    def submit(content,filename,opts,token):
        if not slots.acquire(blocking=False):raise HTTPException(429,"Processing queue is full. Wait for an invoice to finish.")
        try:
            suffix=Path(filename).suffix.lower()
            if suffix not in (".pdf",".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff",".docx",".xlsx",".csv",".txt",".json"):
                raise ValueError("Supported: PDF, PNG, JPEG, WebP, BMP, TIFF, DOCX, XLSX, CSV, TXT and invoice JSON")
            if not content or len(content)>MAX_UPLOAD:raise ValueError("Invoice must be between 1 byte and 12 MB")
            if suffix==".pdf":
                import pypdfium2 as pdfium
                if not content.startswith(b"%PDF-"):raise ValueError("File is not a PDF")
                try:
                    pdf=pdfium.PdfDocument(content);count=len(pdf);pdf.close()
                except Exception:raise ValueError("PDF is damaged or encrypted") from None
                if not 1<=count<=20:raise ValueError("Use invoices with 1 to 20 pages")
            elif suffix in (".png",".jpg",".jpeg",".webp",".bmp",".tif",".tiff"):
                from PIL import Image
                try:
                    with Image.open(io.BytesIO(content)) as image:
                        if image.format not in ("PNG","JPEG","WEBP","BMP","TIFF") or image.width*image.height>25_000_000 or getattr(image,"n_frames",1)>20:raise ValueError()
                        image.verify()
                except Exception:raise ValueError("Image is invalid or exceeds 25 megapixels / 20 frames") from None
            elif suffix in (".docx",".xlsx"):
                import zipfile
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        if sum(x.file_size for x in archive.infolist())>30_000_000:raise ValueError()
                        marker="word/document.xml" if suffix==".docx" else "xl/workbook.xml"
                        if marker not in archive.namelist():raise ValueError()
                except Exception:raise ValueError("Office document is invalid or expands beyond 30 MB") from None
            else:
                try:content.decode("utf-8-sig")
                except UnicodeDecodeError:raise ValueError("Text/CSV/JSON invoices must use UTF-8 encoding") from None
            jid=uuid.uuid4().hex;path=store.root/"uploads"/(jid+suffix);path.write_bytes(content);os.chmod(path,0o600)
            job={"id":jid,"filename":Path(filename).name[:200],"size":len(content),"path":str(path),"sha256":hashlib.sha256(content).hexdigest(),
                 "created_at":datetime.now(timezone.utc).isoformat(),"status":"queued","options":opts.model_dump(),
                 "invoice":Invoice().model_dump(mode="json"),"revision":1,"reviewed":False,"trace":[],"provenance":[],"completeness":0,"selected_engine":"pending","validation":{"ready":False,"issues":[],"matches":[]}}
            with plans_lock:
                try:
                    plan,entry=require_plan(token,opts,filename,len(content))
                    if identity.cloud:store.persist_blob(path)
                    job["confirmed_signature"]=plan["signature"]
                    store.job(jid,job);store.audit("uploaded",{"job_id":jid,"filename":job["filename"],"sha256":job["sha256"]})
                    pool.submit(copy_context().run,run,jid,opts)
                    plan["files"].remove(entry)
                except Exception:
                    if store.job(jid):
                        job.update(status="error",error="Could not queue this invoice. Review the plan and retry.");store.job(jid,job)
                    else:path.unlink(missing_ok=True)
                    raise
            return public(job)
        except Exception:slots.release();raise

    def public(j,full=True):
        j={k:v for k,v in j.items() if k!="path"}
        if not full:
            j.pop("text",None);j.pop("boxes",None)
        return j

    @app.get("/api/public-config")
    def public_config():return identity.config()

    @app.get("/api/session")
    def user_session(request:Request):return getattr(request.state,"identity",{"email":"local operator","uid":"local-operator"})

    @app.get("/api/state")
    def state():
        vertex_configured=bool(os.getenv("VERTEX_PROJECT_ID"))
        with store.connection() as c:
            accounts=store.get("chatgpt_accounts",[],c)
            active_account=store.get("chatgpt_active",c=c)
            settings=store.get("settings",Settings().model_dump(),c)
            if vertex_configured and not settings.get("model"):
                settings={**settings,"provider":"vertex","model":os.getenv("VERTEX_MODEL","gemini-3.7-flash")}
            connections={"openai":bool(store.secret("openai",c=c)),"anthropic":bool(store.secret("anthropic",c=c)),
                "chatgpt":any(a["id"]==active_account and a["connected"] for a in accounts),
                "claude_local":providers.claude_subscription.connected(c=c) and bool(shutil.which("claude")),"vertex":vertex_configured}
            jobs=store.jobs(c)
            refs=references(c);rules=policy(c);ledger=store.ledger(c)
            exports=[{"id":r["id"],"job_id":r["job_id"],**{k:v for k,v in json.loads(r["payload"]).items() if k in ("number","created_at")}} for r in c.execute("SELECT id,job_id,payload FROM exports ORDER BY rowid DESC")]
        return {"engines":capabilities(),"connections":connections,
                "accounts":accounts,"active_account":active_account,"settings":settings,
                "policy":rules.model_dump(mode="json"),"references":{"version":refs.get("version"),"imported_at":refs.get("imported_at"),"counts":{k:len(refs.get(k,[])) for k in ("sites","routes","items","orders","receipts","taxRules")}} if refs else None,
                "jobs":[public(evaluate(j,validation_context=(refs,rules,ledger)),False) for j in jobs],"exports":exports,
                "samples":{"demo":{"name":"SYNTHETIC-demo-invoice.pdf","size":(ROOT/"samples/invoice.pdf").stat().st_size}}}

    @app.post("/api/preflight")
    def preflight(body:Preflight):
        warnings=[];blocking=[];opts=body.options
        if opts.engine=="invoice2data":warnings.append("invoice2data reads native text and supplier templates. Scanned PDFs and images use PaddleOCR locally as the input reader; the trace names both components.")
        if opts.engine in ("auto","invoice2data","paddleocr"):
            warnings.append("Short scanned PDFs with unread quantities or prices may receive one higher-resolution local OCR pass, within a six-minute reader limit. It is retained only when consistency checks improve; all values still require review. Local OCR files wait their turn to keep the workspace responsive.")
        if not references():warnings.append("No reference files loaded. Extraction can run, but Excel export stays on hold until references are imported and checked.")
        if opts.ai_fallback or opts.engine=="ai":
            if not opts.model:warnings.append("No AI model selected. If local reading fails, this batch will wait for an AI connection or manual review.")
            warnings.append("AI fallback sends the invoice document or its extracted text to the selected provider. Provider usage limits and charges may apply.")
        warnings.append("Each file must contain one invoice. For a combined PDF containing several invoices, split it into separate files first.")
        warnings.append("Missing identity, price, quantity, receipt or tax evidence cannot be guessed. Exceptions require review.")
        token=secrets.token_urlsafe(32)
        with plans_lock:
            expired=[k for k,v in plans.items() if v["expires"]<time.time()]
            for k in expired:plans.pop(k)
            if len(plans)>200:raise HTTPException(429,"Too many pending plans; try again later")
            plans[token]={"expires":time.time()+600,"signature":rule_signature(opts),"confirmed":False,"files":[x.model_dump() for x in body.files]}
            summary={**opts.model_dump(),"reference_version":references().get("version"),"policy":policy().model_dump(mode="json"),"file_count":len(body.files)}
        return {"token":token,"summary":summary,"warnings":warnings,"blocking":blocking}

    @app.post("/api/preflight/confirm")
    def confirm_plan(body:PreflightToken):
        with plans_lock:
            plan=plans.get(body.token)
            if not plan or plan["expires"]<time.time():raise HTTPException(409,"Processing plan expired. Review it again.")
            plan["confirmed"]=True
        store.audit("processing_plan_confirmed",{"signature":plan["signature"],"files":plan["files"]})
        return {"confirmed":True}

    @app.post("/api/invoices")
    async def upload(file:UploadFile=File(...),options:str=Form("{}"),preflight_token:str=Form("")):
        content=await file.read(MAX_UPLOAD+1);name=file.filename or "invoice.pdf";opts=ProcessingOptions.model_validate_json(options)
        return submit(content,name,opts,preflight_token)

    @app.post("/api/jobs/delete")
    def delete_jobs(body:DeleteJobsRequest):
        if not body.confirm_permanent:
            raise HTTPException(400,"Confirm permanent invoice deletion")
        if len({item.id for item in body.jobs})!=len(body.jobs):
            raise ValueError("Select each invoice only once")
        try:
            with plans_lock,store.connection(True) as connection:
                return delete_invoices(store,body.jobs,connection)
        except DeletionError as error:
            raise HTTPException(error.status_code,error.detail) from None

    @app.get("/api/jobs/{jid}")
    def get_job(jid:str):return public(evaluate(job_or_404(jid)))

    @app.get("/api/jobs/{jid}/document")
    def document(jid:str):
        j=job_or_404(jid)
        if identity.cloud:store.ensure_blob(Path(j["path"]))
        return FileResponse(j["path"],filename=j["filename"],content_disposition_type="inline")

    @app.post("/api/jobs/{jid}/extraction-draft")
    def download_extraction(jid:str,body:ExtractionDraftRequest):
        if not body.acknowledge_unvalidated:raise HTTPException(400,"Acknowledge that this is an unvalidated review copy")
        j=job_or_404(jid)
        if j["status"] in ("queued","processing"):raise HTTPException(409,"Wait for extraction to finish")
        if j["revision"]!=body.revision:raise HTTPException(409,"Invoice changed. Refresh before downloading.")
        invoice=Invoice.model_validate(j["invoice"])
        if not invoice.number and not invoice.lines:raise HTTPException(409,"No extracted invoice fields are available")
        content=extraction_workbook(invoice,j["filename"],j["revision"])
        store.audit("extraction_draft_downloaded",{"job_id":jid,"revision":j["revision"],"approved":False,"line_items":len(invoice.lines)})
        return Response(content,media_type=MIME_XLSX,headers={"Content-Disposition":f'attachment; filename="EXTRACTION_REVIEW_ONLY_{jid[:8]}.xlsx"'})

    @app.post("/api/jobs/{jid}/review")
    def review(jid:str,body:Review):
        with store.connection(True) as c:
            j=job_or_404(jid,c);assert_editable(j)
            if j["revision"]!=body.revision:raise HTTPException(409,"Invoice changed. Refresh before saving.")
            inv,provenance=enrich(body.invoice,references(c))
            before=j["invoice"]
            j.update(invoice=inv.model_dump(mode="json"),reviewed=body.confirm,status="review",revision=j["revision"]+1)
            # Keep original derivation evidence, add new derivations and record edits separately.
            j["provenance"]+=provenance
            evaluate(j,c);store.job(jid,j,c)
            store.audit("reviewed" if body.confirm else "edited",{"job_id":jid,"before":before,"after":j["invoice"],"revision":j["revision"],"reference_version":references(c).get("version")},c)
        return public(j)

    @app.post("/api/jobs/{jid}/retry")
    def retry(jid:str,body:Retry):
        j=job_or_404(jid);assert_editable(j)
        if not slots.acquire(blocking=False):raise HTTPException(429,"Processing queue is full")
        try:
            with plans_lock:
                plan,entry=require_plan(body.preflight_token,body.options,j["filename"],j.get("size",Path(j["path"]).stat().st_size))
                with store.connection(True) as c:
                    j=job_or_404(jid,c);assert_editable(j)
                    j.update(status="queued",reviewed=False,revision=j["revision"]+1,options=body.options.model_dump(),confirmed_signature=plan["signature"])
                    store.job(jid,j,c);store.audit("retry",{"job_id":jid,"options":body.options.model_dump()},c)
                try:pool.submit(copy_context().run,run,jid,body.options)
                except Exception:
                    j.update(status="error",error="Could not queue this invoice. Review the plan and retry.");store.job(jid,j)
                    raise HTTPException(503,"Processing queue is unavailable") from None
                plan["files"].remove(entry)
            return public(j)
        except Exception:slots.release();raise

    @app.post("/api/jobs/{jid}/export")
    def export(jid:str,body:Revision):
        with store.connection(True) as c:
            j=job_or_404(jid,c)
            if j.get("export_id"):return {"id":j["export_id"],"url":"/api/exports/"+j["export_id"]}
            assert_editable(j)
            if j["revision"]!=body.revision:raise HTTPException(409,"Invoice changed. Refresh before exporting.")
            evaluate(j,c)
            if not j["validation"]["ready"]:raise HTTPException(409,"Invoice is held. Resolve every validation issue and confirm evidence review.")
            inv=Invoice.model_validate(j["invoice"]);content=workbook(inv,j["validation"]);eid=uuid.uuid4().hex
            receipt={"id":eid,"job_id":jid,"invoice_key":key(inv),"number":inv.number,"created_at":datetime.now(timezone.utc).isoformat(),
                     "reference_version":references(c).get("version"),"policy":policy(c).model_dump(mode="json"),
                     "allocations":[m["allocation"] for m in j["validation"]["matches"]]}
            c.execute("INSERT INTO exports VALUES (?,?,?,?,?)",(eid,key(inv),jid,json.dumps(receipt),content))
            j.update(status="exported",export_id=eid,revision=j["revision"]+1);store.job(jid,j,c);store.audit("exported",receipt,c)
        return {"id":eid,"url":"/api/exports/"+eid}

    @app.get("/api/exports/{eid}")
    def download(eid:str):
        with store.connection() as c:r=c.execute("SELECT workbook FROM exports WHERE id=?",(eid,)).fetchone()
        if not r:raise HTTPException(404,"Export not found")
        return Response(bytes(r[0]),media_type=MIME_XLSX,headers={"Content-Disposition":f'attachment; filename="Merch_Inv_{eid[:8]}.xlsx"'})

    @app.post("/api/exports/extraction-batch")
    def download_extraction_batch(body:ExtractionBatch):
        if not body.acknowledge_unvalidated:raise HTTPException(400,"Acknowledge that this is an unvalidated review copy")
        if len({x.id for x in body.jobs})!=len(body.jobs):raise ValueError("Select each invoice only once")
        entries=[];snapshots=[]
        with store.connection() as c:
            for request in body.jobs:
                j=job_or_404(request.id,c)
                if j["status"] in ("queued","processing"):raise HTTPException(409,"Wait for selected invoices to finish processing")
                if j["revision"]!=request.revision:raise HTTPException(409,"A selected invoice changed. Refresh before downloading.")
                invoice=Invoice.model_validate(j["invoice"])
                if not invoice.number and not invoice.lines:raise HTTPException(409,"A selected invoice has no extracted fields. Complete it before downloading.")
                entries.append((invoice,j["filename"],j["revision"]))
                snapshots.append({"job_id":j["id"],"revision":j["revision"],"transaction_number":len(entries),"line_items":len(invoice.lines)})
        content=extraction_batch_workbook(entries)
        store.audit("extraction_batch_downloaded",{"invoices":snapshots,"approved":False,"count":len(entries)})
        return Response(content,media_type=MIME_XLSX,headers={"Content-Disposition":'attachment; filename="EXTRACTION_REVIEW_ONLY_BATCH.xlsx"'})

    @app.post("/api/exports/batch")
    def export_batch(body:Batch):
        if len({x.id for x in body.jobs})!=len(body.jobs):raise ValueError("Select each invoice only once")
        with store.connection(True) as c:
            selected=[];entries=[];ledger=store.ledger(c);batch_id=uuid.uuid4().hex
            for index,request in enumerate(body.jobs,1):
                j=job_or_404(request.id,c);assert_editable(j)
                if j["revision"]!=request.revision:raise HTTPException(409,f"Invoice {j['filename']} changed. Refresh before exporting.")
                inv=Invoice.model_validate(j["invoice"])
                result=validate(inv,references(c),policy(c),ledger,j.get("reviewed",False))
                if not result["ready"]:raise HTTPException(409,f"Invoice {j['filename']} is held: {result['issues'][0]['message']}")
                receipt={"id":uuid.uuid4().hex,"job_id":j["id"],"batch_id":batch_id,"transaction_number":index,
                         "invoice_key":key(inv),"number":inv.number,"created_at":datetime.now(timezone.utc).isoformat(),
                         "reference_version":references(c).get("version"),"policy":policy(c).model_dump(mode="json"),
                         "allocations":[m["allocation"] for m in result["matches"]]}
                ledger.append(receipt);entries.append((inv,result));selected.append((j,receipt))
            content=batch_workbook(entries)
            c.execute("INSERT INTO batches VALUES (?,?,?)",(batch_id,json.dumps([r for _,r in selected]),content))
            for j,receipt in selected:
                c.execute("INSERT INTO exports VALUES (?,?,?,?,?)",(receipt["id"],receipt["invoice_key"],j["id"],json.dumps(receipt),content))
                j.update(status="exported",export_id=receipt["id"],batch_id=batch_id,transaction_number=receipt["transaction_number"],revision=j["revision"]+1)
                store.job(j["id"],j,c);store.audit("exported",receipt,c)
        return {"id":batch_id,"url":"/api/batches/"+batch_id,"count":len(selected)}

    @app.get("/api/batches/{bid}")
    def download_batch(bid:str):
        with store.connection() as c:r=c.execute("SELECT workbook FROM batches WHERE id=?",(bid,)).fetchone()
        if not r:raise HTTPException(404,"Batch not found")
        return Response(bytes(r[0]),media_type=MIME_XLSX,headers={"Content-Disposition":f'attachment; filename="Merch_Inv_Batch_{bid[:8]}.xlsx"'})

    @app.get("/api/jobs/{jid}/audit")
    def audit(jid:str):
        job_or_404(jid)
        with store.connection() as c:
            return [{"event":r["event"],"at":r["at"],"payload":json.loads(r["payload"])} for r in c.execute("SELECT * FROM audit ORDER BY id") if json.loads(r["payload"]).get("job_id")==jid]

    def save_refs(refs):
        with plans_lock,store.connection(True) as c:
            # Prior exports remain in the ledger. Baseline invoiced must exclude this app's exports.
            store.set("references",refs,c)
            for r in c.execute("SELECT id,payload FROM jobs").fetchall():
                j=json.loads(r["payload"])
                if not j.get("export_id"):
                    j.update(reviewed=False,revision=j["revision"]+1)
                    store.job(r["id"],j,c)
            store.audit("reference_import",{"version":refs["version"]},c)
        return {"version":refs["version"],"detail":"References imported. Unexported invoices require a new review."}

    @app.post("/api/references")
    async def reference_upload(file:UploadFile=File(...)):
        content=await file.read(MAX_UPLOAD+1)
        if len(content)>MAX_UPLOAD:raise HTTPException(413,"Reference file is too large")
        return save_refs(import_references(content,file.filename or ""))

    @app.post("/api/references/demo")
    def reference_demo():return save_refs(import_references((ROOT/"samples/reference.json").read_bytes(),"reference.json"))

    @app.post("/api/demo")
    def demo(body:DemoRequest):
        content=(ROOT/"samples/invoice.pdf").read_bytes();name="SYNTHETIC-demo-invoice.pdf";opts=ProcessingOptions()
        return submit(content,name,opts,body.preflight_token)

    @app.get("/api/samples/reference.xlsx")
    def sample_reference():return Response(reference_workbook(json.loads((ROOT/"samples/reference.json").read_text())),media_type=MIME_XLSX,headers={"Content-Disposition":'attachment; filename="Reference_Template.xlsx"'})

    @app.get("/api/samples/invoice.pdf")
    def sample_invoice():return FileResponse(ROOT/"samples/invoice.pdf",filename="Synthetic_Invoice.pdf")

    @app.get("/api/samples/template.yml")
    def sample_template():return FileResponse(ROOT/"app/templates/demo.yml",filename="supplier-template.yml")

    @app.post("/api/templates")
    async def upload_template(file:UploadFile=File(...)):
        content=await file.read(65537)
        if len(content)>65536:raise ValueError("Template limit is 64 KB")
        data=yaml.safe_load(content)
        if not isinstance(data,dict) or not isinstance(data.get("keywords"),list) or not isinstance(data.get("fields"),dict):raise ValueError("Template needs keywords and fields")
        if not data["keywords"] or any(not isinstance(x,str) for x in data["keywords"]):raise ValueError("Template keywords must be non-empty strings")
        # invoice2data templates are declarative, and parsing runs in a timed subprocess.
        allowed=set(Invoice.model_fields)|{"invoice_number","amount"}
        if set(data["fields"])-allowed:raise ValueError("Template includes unsupported invoice fields")
        name=hashlib.sha256(content).hexdigest()[:20]+".yml"
        (store.root/"templates"/name).write_text(yaml.safe_dump(data))
        if identity.cloud:store.persist_blob(store.root/"templates"/name)
        store.audit("template_import",{"name":name})
        return {"name":name,"detail":"Supplier template registered. Test it on representative invoices."}

    @app.post("/api/policy")
    def save_policy(body:Policy):
        if any(not re.fullmatch(r"[A-Z]{3}",k) or not 0<=v<=4 for k,v in body.currency_decimals.items()):raise ValueError("Currency decimal mapping must use ISO-style codes and 0–4 places")
        with plans_lock,store.connection(True) as c:
            store.set("policy",body.model_dump(mode="json"),c)
            for row in c.execute("SELECT id,payload FROM jobs").fetchall():
                j=json.loads(row["payload"])
                if not j.get("export_id"):j.update(reviewed=False,revision=j["revision"]+1);store.job(row["id"],j,c)
            store.audit("policy_changed",body.model_dump(mode="json"),c)
        return body

    @app.post("/api/connections/{provider}")
    def connect(provider:str,body:Connection):
        if provider not in ("openai","anthropic"):raise ValueError("Use API keys only for OpenAI or Anthropic API connections")
        key=body.api_key.get_secret_value().strip()
        verified=providers.verify_key(provider,key)
        store.secret(provider,key);return {"connected":True,"verified":verified}

    @app.post("/api/subscriptions/claude/import")
    def import_claude_subscription(body:SubscriptionToken):
        result=providers.claude_subscription.connect(body.setup_token.get_secret_value())
        store.audit("subscription_connected",{"provider":"claude_local"})
        return result

    @app.delete("/api/subscriptions/claude")
    def disconnect_claude_subscription():
        providers.claude_subscription.disconnect()
        store.audit("subscription_disconnected",{"provider":"claude_local"})
        return {"connected":False}

    @app.post("/api/chatgpt/export")
    def export_chatgpt_bundle(body:Account):
        if identity.cloud:raise HTTPException(403,"Export a subscription registration from your local Invoice Studio installation")
        bundle=auth.export_bundle(body.id)
        return Response(json.dumps(bundle),media_type="application/json",headers={"Cache-Control":"no-store","Pragma":"no-cache","Content-Disposition":'attachment; filename="invoice-studio-chatgpt-credentials.json"'})

    @app.post("/api/chatgpt/import")
    def import_chatgpt_bundle(body:ChatGPTBundle):
        if not identity.cloud:raise HTTPException(403,"Credential transfer is for your authenticated self-hosted workspace")
        account=auth.import_bundle(body.bundle)
        store.audit("subscription_connected",{"provider":"chatgpt"})
        return account

    @app.get("/api/reference-lookup/summary")
    def lookup_summary():return lookup.summary()

    @app.get("/api/reference-lookup/search")
    def lookup_search(kind:str,q:str,cursor:str="",limit:int=25,site:str="",supplier:str="",po:str=""):
        return lookup.search(kind,q,cursor,limit,site=site,supplier=supplier,po=po)

    @app.get("/api/reference-lookup/products")
    def product_candidates(q:str,limit:int=25,site:str="",supplier:str="",po:str="",invoice_po:str="",price:str="",qty:str="",currency:str="",uom:str=""):
        return products.search(q,limit=limit,site=site,supplier=supplier,po=po,invoice_po=invoice_po,price=price,qty=qty,currency=currency,uom=uom)

    @app.post("/api/jobs/{jid}/draft")
    def manual_draft(jid:str,body:ManualDraftRequest):
        job_or_404(jid)
        content=build_draft_workbook(body)
        metadata=draft_public_metadata(body,job_id=jid)
        metadata["workbook_sha256"]=hashlib.sha256(content).hexdigest()
        store.audit("manual_draft_downloaded",metadata)
        return Response(content,media_type=MIME_XLSX,headers={"Content-Disposition":f'attachment; filename="{draft_filename(body)}"',"Cache-Control":"no-store"})

    @app.delete("/api/connections/{provider}")
    def disconnect(provider:str):
        if provider not in ("openai","anthropic"):raise ValueError("Unknown API connection")
        store.secret(provider,delete=True);return {"connected":False}

    @app.get("/api/connections/{provider}/models")
    def model_list(provider:str):
        if provider not in ("openai","anthropic","chatgpt","claude_local","vertex"):raise ValueError("Unknown provider")
        try:return {"models":providers.models(provider)}
        except ValueError:raise
        except Exception:raise ValueError("Could not retrieve models. Check the connection and try again.") from None

    @app.post("/api/settings")
    def save_settings(body:Settings):
        if body.provider not in ("openai","anthropic","chatgpt","claude_local","vertex"):raise ValueError("Unknown provider")
        store.set("settings",body.model_dump());return body

    @app.post("/api/chatgpt/start")
    def sign_in(body:SignIn,request:Request):
        if identity.cloud:raise ValueError("Use ChatGPT subscription sign-in from the local application")
        port=request.url.port or 80
        return {"url":auth.start(port,body.account_id)}

    @app.get("/auth/callback")
    def callback(request:Request):
        if identity.cloud:raise HTTPException(404,"Local subscription callback is not enabled in cloud mode")
        try:auth.finish(dict(request.query_params));return RedirectResponse("/?connected=chatgpt")
        except Exception:return RedirectResponse("/?"+urlencode({"connection_error":"ChatGPT sign-in could not be verified. Start again from AI connections."}))

    @app.post("/api/chatgpt/select")
    def select_account(body:Account):
        if not any(a["id"]==body.id and a["connected"] for a in auth.accounts()):raise ValueError("Connect this account before selecting it")
        store.set("chatgpt_active",body.id);return {"id":body.id}

    @app.delete("/api/chatgpt/{aid}")
    def remove_account(aid:str):return {"detail":auth.disconnect(aid)}

    @app.get("/health")
    def health():return {"status":"ok","version":"0.1.0"}

    @app.get("/")
    def home():return FileResponse(ROOT/"app/static/index.html")

    app.mount("/static",StaticFiles(directory=ROOT/"app/static"),name="static")
    return app


app=create_app()
