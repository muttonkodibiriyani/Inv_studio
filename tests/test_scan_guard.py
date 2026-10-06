"""Scanned invoices: weak local reads, the AI page-image stand-in, checked and corroborated barcodes. Synthetic data only."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import engines, scan_guard
from app.models import Invoice, ai_schema, parse_ai_output


def with_check(body):
    """A synthetic code: ``body`` plus its GS1 mod-10 check digit."""
    total=sum(int(d)*(3 if n%2==0 else 1) for n,d in enumerate(reversed(body)))
    return body+str((10-total%10)%10)


def wrong_check(code):
    return code[:-1]+str((int(code[-1])+1)%10)


CODE=with_check("400000000001")       # 13 digits, valid
OTHER=with_check("400000000002")
NEAR=with_check("400000000011")       # differs from CODE by one body digit, still valid


@pytest.mark.parametrize("body",["1234567","12345678901","123456789012","1234567890123"])
def test_check_digit_at_each_gtin_length(body):
    good=with_check(body)
    assert len(good) in scan_guard.GTIN_LENGTHS
    assert scan_guard.gtin_valid(good)
    assert not scan_guard.gtin_valid(wrong_check(good))


def test_barcode_is_stored_as_digits_and_sorted_by_its_check_digit():
    spaced=f"EAN: {CODE[:1]} {CODE[1:7]}-{CODE[7:]}"
    assert scan_guard.classify_barcode(spaced)==(CODE,None,None)
    assert scan_guard.classify_barcode("UPC"+wrong_check(CODE))==(None,wrong_check(CODE),scan_guard.MISPRINT)
    assert scan_guard.classify_barcode(CODE[:6]+"?")==(None,None,scan_guard.UNREADABLE)
    assert scan_guard.classify_barcode(CODE[:6])==(None,None,scan_guard.UNREADABLE)
    assert scan_guard.classify_barcode(None)==(None,None,None)


def test_a_pdf_without_a_text_layer_is_a_scan():
    assert scan_guard.is_scan(2,Path("x.pdf"))
    assert not scan_guard.is_scan(500,Path("x.pdf"))
    assert not scan_guard.is_scan(None,Path("x.pdf"))
    assert scan_guard.is_scan(None,Path("x.png"))


def box(text,page=1):
    return {"text":text,"page":page,"geometry":"word"}


def test_page_digit_runs_are_whole_standalone_tokens():
    found=scan_guard.page_occurrences([box("EAN:"+CODE),box("9"+OTHER),box(CODE[:6]),box(CODE[6:]),
                                       box(f"{NEAR[:7]} {NEAR[7:]}",page=2)])
    assert found[1][CODE]==1
    assert found[1][OTHER]==0        # only inside a longer run
    assert found[2][NEAR]==1         # print separators inside the token are joined
    assert found[1][CODE[:6]+CODE[6:]]==1   # two tokens never join: the count is the EAN token alone


def test_count_limit_all_claimants():
    one={1:scan_guard.page_occurrences([box(CODE)])[1]}
    two={1:scan_guard.page_occurrences([box(CODE),box(CODE)])[1]}
    assert scan_guard.corroborate([(0,1,CODE),(1,1,CODE)],one)==set()
    assert scan_guard.corroborate([(0,1,CODE),(1,1,CODE)],two)=={0,1}


def test_page_none_is_document_wide_and_shares_the_budget():
    occ=scan_guard.page_occurrences([box(CODE,page=2)])
    assert scan_guard.corroborate([(0,None,CODE)],occ)=={0}
    assert scan_guard.corroborate([(0,1,CODE)],occ)==set()          # printed on page 2, claimed on page 1
    assert scan_guard.corroborate([(0,2,CODE),(1,None,CODE)],occ)==set()
    occ=scan_guard.page_occurrences([box(CODE,page=2),box(CODE,page=3)])
    assert scan_guard.corroborate([(0,2,CODE),(1,None,CODE)],occ)=={0,1}


def test_self_check_uses_printed_line_nets_not_quantity_times_price():
    # 3 x 3.333 is 9.999, printed 10.00 per line: the rounding differs, the printed nets sum to the net.
    lines=[{"qty":"3","price":"3.333","net_amount":"10.00"},{"qty":"1","price":"5.00","net_amount":"5.00"}]
    assert scan_guard.ai_self_check({"number":"S-1","date":"2026-01-15","net":"15.00","lines":lines})==[]
    lines[1]["net_amount"]=None
    assert "printed line nets do not sum to its net" in scan_guard.ai_self_check(
        {"number":"S-1","date":"2026-01-15","net":"15.00","lines":lines})


def test_weak_read_reasons():
    assert scan_guard.weak_read({"number":"S-1","date":"2026-01-15","net":"5.00",
                                 "lines":[{"sku":"A","net_amount":"5.00"}]})==[]
    weak=scan_guard.weak_read({"net":"5.00","lines":[{"barcode_unchecked":wrong_check(CODE),"net_amount":"5.00"}]})
    assert "no line has an item code" in weak and "fewer than 3 of number, date, PO, net and tax" in weak
    assert "no net total" in scan_guard.weak_read({"number":"S-1","lines":[]})


def test_inclusive_vat_is_counted_once_per_line():
    inclusive=[{"net_amount":"105.00","tax_amount":"5.00"},{"net_amount":"210.00","tax_amount":"10.00"}]
    assert scan_guard.inclusive_vat_lines(inclusive)==2
    assert scan_guard.inclusive_vat_lines([{"net_amount":"100.00","tax_amount":"5.00"}])==0


def test_ai_schema_has_no_barcode_unchecked_and_parse_drops_it():
    assert "barcode_unchecked" not in ai_schema()["properties"]["lines"]["items"]["properties"]
    invoice,_=parse_ai_output({"lines":[{"gtin":CODE,"barcode_unchecked":"1"}]})
    assert invoice.lines[0].barcode_unchecked is None


def test_ai_never_fills_a_gtin_where_the_print_failed_its_check_digit():
    engine={"number":"N-1","net":"5.00","lines":[{"barcode_unchecked":wrong_check(CODE),"qty":"1","price":"5.00"}]}
    ai={"number":"N-1","net":"5.00","lines":[{"gtin":NEAR,"qty":"1","price":"5.00"}]}
    merged,_,_,lines,_=engines.merge_ai_fields(engine,ai,{},{},"ocr","gap_fill")
    assert merged["lines"][0].get("gtin") is None and "gtin" not in lines[0]


# ---- process(): a scanned PDF through the local readers and the AI ----

JUNK=[{"sku":"X1","description":"smudge","qty":"1"},{"sku":"X2","description":"smudge"}]


def ai_scan_read(lines=None,**header):
    lines=lines if lines is not None else [
        {"gtin":CODE,"description":"Widget","qty":"2","price":"5.00","net_amount":"10.00","page":1},
        {"gtin":OTHER,"description":"Gadget","qty":"1","price":"5.00","net_amount":"5.00","page":1}]
    return {"number":"SCAN-1","date":"2026-01-15","po":"PO-1","net":"15.00","tax":"0.75","lines":lines,**header}


def run_scan(monkeypatch,tmp_path,ai_invoice,local=None,ocr_tokens=(),paddle_error=None,ai_evidence=None):
    calls=[]
    monkeypatch.setattr(engines,"capabilities",lambda:[{"id":x,"installed":True} for x in ("invoice2data","paddleocr","docling")])
    boxes=[box(t) for t in ocr_tokens]

    def read(engine,*args,**kwargs):
        calls.append(engine)
        if engine=="invoice2data":return {"text":"  ","boxes":[],"invoice":None}
        if engine=="paddleocr":
            if paddle_error:raise ValueError(paddle_error)
            return {"text":"scan text "*10,"boxes":boxes,"invoice":local}
        raise ValueError("Reader timed out. Try another engine or inspect this document manually.")
    monkeypatch.setattr(engines,"local_read",read)

    def ai_reader(*args):
        calls.append("ai")
        quoted={"net":{"quote":"Net "+str(ai_invoice.get("net")),"page":1},
                "tax":{"quote":"VAT "+str(ai_invoice.get("tax")),"page":1}}
        return Invoice.model_validate(ai_invoice),{"evidence":{**quoted,**(ai_evidence or {})}}
    opts=SimpleNamespace(engine="auto",provider="vertex",model="gemini-test",ai_fallback=True,language="en",
                         prefer_native_text=True)
    return engines.process(tmp_path/"scan.pdf",opts,SimpleNamespace(root=tmp_path),ai_reader),calls


def test_weak_local_scan_read_is_replaced_by_a_checked_ai_read(monkeypatch,tmp_path):
    local={"number":"LOCAL-1","net":"99.00","lines":JUNK}
    result,calls=run_scan(monkeypatch,tmp_path,ai_scan_read(),local=local,ocr_tokens=[CODE,OTHER],
                          ai_evidence={"number":{"quote":"Invoice SCAN-1","page":1}})
    assert calls[0]=="invoice2data" and calls[-1]=="ai"
    invoice=result["invoice"]
    assert invoice["number"]=="SCAN-1" and invoice["net"]=="15.00" and len(invoice["lines"])==2
    assert [line["gtin"] for line in invoice["lines"]]==[CODE,OTHER]
    assert result["readers"]["header"]["number"]=="ai" and result["readers"]["lines"][0]["gtin"]=="ai"
    assert result["evidence"]["header"]["number"]["origin"]==scan_guard.AI_PAGE_IMAGE
    # The OCR boxes stay with the result: no later verify_scan pass is needed.
    assert result["boxes"] and not engines.needs_scan_evidence(result,SimpleNamespace(provider="vertex"))
    paddle=next(t for t in result["trace"] if t["engine"]=="paddleocr")
    assert paddle["ocr_status"]=="ok" and paddle["tokens_per_page"]=={1:2}


def test_both_reads_weak_is_not_read_and_fills_nothing(monkeypatch,tmp_path):
    local={"number":"LOCAL-1","net":"99.00","lines":JUNK}
    ai=ai_scan_read();ai["lines"][1]["net_amount"]=None
    result,_=run_scan(monkeypatch,tmp_path,ai,local=local,ocr_tokens=[CODE,OTHER])
    assert result["readers"]["ai"]["status"]=="not_read"
    assert result["readers"]["ai"]["reason"].startswith("Not read:")
    assert result["extraction_note"]==result["readers"]["ai"]["reason"]
    assert result["invoice"]["number"]=="LOCAL-1" and result["invoice"]["po"] is None
    assert [line["sku"] for line in result["invoice"]["lines"]]==["X1","X2"]
    assert "ai" not in result["readers"]["header"].values()


def test_ai_barcode_without_ocr_corroboration_is_cleared_to_evidence(monkeypatch,tmp_path):
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(),ocr_tokens=[OTHER])
    first,second=result["invoice"]["lines"]
    assert first["gtin"] is None and second["gtin"]==OTHER
    review=result["evidence"]["lines"][0]["gtin"]["review"]
    assert review=={"reason":scan_guard.UNCONFIRMED,"other_value":CODE}
    assert "gtin" not in result["readers"]["lines"][0]


def test_ai_barcode_on_a_scan_with_failed_ocr_is_never_filled(monkeypatch,tmp_path):
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(),paddle_error="Reader timed out. Try another engine.")
    assert [line["gtin"] for line in result["invoice"]["lines"]]==[None,None]
    status={t["engine"]:t.get("ocr_status") for t in result["trace"] if t["engine"] in ("paddleocr","docling")}
    assert status=={"paddleocr":"timed_out","docling":"timed_out"}


def test_covered_barcode_known_to_the_ai_is_not_filled(monkeypatch,tmp_path):
    # The print shows the first half only; the AI answers with a complete, valid 'known product' code.
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(),ocr_tokens=[CODE[:7],OTHER])
    assert result["invoice"]["lines"][0]["gtin"] is None
    partial=ai_scan_read();partial["lines"][0]["gtin"]=CODE[:7]
    result,_=run_scan(monkeypatch,tmp_path,partial,ocr_tokens=[CODE[:7],OTHER])
    assert result["invoice"]["lines"][0]["gtin"] is None
    assert result["evidence"]["lines"][0]["gtin"]["review"]["reason"]==scan_guard.UNREADABLE


def test_substring_only_occurrence_does_not_corroborate(monkeypatch,tmp_path):
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(),ocr_tokens=["7"+CODE,OTHER])
    assert result["invoice"]["lines"][0]["gtin"] is None


def test_repeated_barcode_claims_need_as_many_printed_occurrences(monkeypatch,tmp_path):
    ai=ai_scan_read();ai["lines"][1]["gtin"]=CODE
    result,_=run_scan(monkeypatch,tmp_path,ai,ocr_tokens=[CODE])
    assert [line["gtin"] for line in result["invoice"]["lines"]]==[None,None]
    result,_=run_scan(monkeypatch,tmp_path,ai,ocr_tokens=[CODE,CODE])
    assert [line["gtin"] for line in result["invoice"]["lines"]]==[CODE,CODE]


def test_local_misprinted_barcode_goes_to_barcode_unchecked(monkeypatch,tmp_path):
    local={"number":"LOCAL-1","date":"2026-01-15","net":"5.00","tax":"0.25",
           "lines":[{"sku":"A","gtin":"EAN "+wrong_check(CODE),"qty":"1","price":"5.00","net_amount":"5.00"}]}
    result,calls=run_scan(monkeypatch,tmp_path,ai_scan_read(),local=local,ocr_tokens=[wrong_check(CODE)])
    line=result["invoice"]["lines"][0]
    assert line["gtin"] is None and line["barcode_unchecked"]==wrong_check(CODE)
    assert result["evidence"]["lines"][0]["gtin"]["review"]["reason"]==scan_guard.MISPRINT



def test_a_misprint_the_reader_already_set_aside_is_still_flagged(monkeypatch,tmp_path):
    local={"number":"LOCAL-1","date":"2026-01-15","net":"5.00","tax":"0.25",
           "lines":[{"sku":"A","barcode_unchecked":wrong_check(CODE),"qty":"1","price":"5.00","net_amount":"5.00"}]}
    result,calls=run_scan(monkeypatch,tmp_path,ai_scan_read(),local=local,ocr_tokens=[wrong_check(CODE)])
    line=result["invoice"]["lines"][0]
    assert line["gtin"] is None and line["barcode_unchecked"]==wrong_check(CODE)
    review=result["evidence"]["lines"][0]["gtin"]["review"]
    assert review["reason"]==scan_guard.MISPRINT and review["other_value"]==wrong_check(CODE)

def test_po_is_never_read_from_a_po_box(monkeypatch,tmp_path):
    ai=ai_scan_read(po="4521")
    result,_=run_scan(monkeypatch,tmp_path,ai,ocr_tokens=[CODE,OTHER],
                      ai_evidence={"po":{"quote":"P.O. Box 4521","page":1}})
    assert result["invoice"]["po"] is None and "po" not in result["readers"]["header"]
    assert result["evidence"]["header"]["po"]["review"]["other_value"]=="4521"


def test_zero_vat_is_kept_and_missing_vat_is_flagged(monkeypatch,tmp_path):
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(tax="0"),ocr_tokens=[CODE,OTHER])
    assert result["invoice"]["tax"]=="0" and "review" not in result["evidence"]["header"].get("tax",{})
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(tax=None),ocr_tokens=[CODE,OTHER])
    assert result["invoice"]["tax"] is None
    assert result["evidence"]["header"]["tax"]["review"]["reason"]==scan_guard.NO_TAX
    assert result["evidence"]["header"]["tax"]["review"]["code"]==scan_guard.TAX_ABSENT_CODE
    assert result["absent_fields"]==["tax"] and result["reason_codes"]=={"tax":"not_printed"}


@pytest.mark.parametrize("text,reason",[
    ("TAX INVOICE\nInvoice No S-1\nTotal 15.00","no tax total printed on the invoice"),
    ("Tax Invoice\nTRN 100000000000003\nVAT Reg No 1000000\nTotal 15.00","no tax total printed on the invoice"),
    ("Sub total 15.00\nVAT 5% 0.75\nTotal 15.75","a tax label is printed but its total was not read"),
    ("Net 15.00\nTotal Tax 0.75","a tax label is printed but its total was not read"),
    ("Prices incl. VAT\nTotal 15.75","a tax label is printed but its total was not read"),
    ("TRN 100000000000003\nVAT 5%\nTotal 15.75","a tax label is printed but its total was not read"),
])
def test_missing_tax_reason_tells_absent_from_unread(text,reason):
    assert scan_guard.missing_tax_reason(text)==reason
    code=scan_guard.TAX_UNREAD_CODE if "label" in reason else scan_guard.TAX_ABSENT_CODE
    assert scan_guard.missing_tax_code(text)==code


def test_vat_printed_inside_the_net_is_one_header_flag(monkeypatch,tmp_path):
    lines=[{"sku":"A","qty":"1","price":"105.00","net_amount":"105.00","tax_amount":"5.00","page":1},
           {"sku":"B","qty":"1","price":"210.00","net_amount":"210.00","tax_amount":"10.00","page":1}]
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(lines=lines,net="315.00",tax="15.00"))
    header_reviews=[e["review"]["reason"] for e in result["evidence"]["header"].values() if "review" in e]
    assert header_reviews==[f"2 lines: {scan_guard.INCLUSIVE_VAT}"]
    assert not any("review" in e for row in result["evidence"]["lines"] for e in row.values())


def test_a_weak_local_net_is_replaced_by_the_quoted_ai_net(monkeypatch,tmp_path):
    # The local read took the gross as the net; the AI read quotes the printed net.
    local={"number":"SCAN-1","net":"15.75","lines":JUNK}
    result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(),local=local,ocr_tokens=[CODE,OTHER])
    assert result["invoice"]["net"]=="15.00" and result["readers"]["header"]["net"]=="ai"
    assert result["evidence"]["header"]["net"]["origin"]==scan_guard.AI_PAGE_IMAGE


def test_an_unquoted_ai_tax_never_fills_a_weak_scan(monkeypatch,tmp_path):
    for tax in ("0.75","0"):
        result,_=run_scan(monkeypatch,tmp_path,ai_scan_read(tax=tax),local={"net":"15.75","lines":JUNK},
                          ocr_tokens=[CODE,OTHER],ai_evidence={"tax":{"quote":"","page":None}})
        assert result["invoice"]["tax"] is None
        assert result["evidence"]["header"]["tax"]["review"]["reason"]==scan_guard.NO_TAX


def test_absent_fields_is_always_emitted_so_a_re_read_clears_it(monkeypatch,tmp_path):
    first,_=run_scan(monkeypatch,tmp_path,ai_scan_read(tax=None),ocr_tokens=[CODE,OTHER])
    job={**first}
    assert job["absent_fields"]==["tax"]
    second,_=run_scan(monkeypatch,tmp_path,ai_scan_read(),ocr_tokens=[CODE,OTHER])
    job.update(second)
    assert job["absent_fields"]==[] and job["reason_codes"]=={}
