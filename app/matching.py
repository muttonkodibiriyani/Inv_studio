from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re
from .models import Invoice, Policy
from .references import decimal as D


def key(i):
    return "|".join([i.seller or "", i.buyer or "", (i.number or "").strip().upper()])


def enrich(i: Invoice, refs):
    """Derive internal fields from a unique PO, preserving every existing value."""
    i=i.model_copy(deep=True); provenance=[]
    orders=[p for p in refs.get("orders",[]) if str(p["id"])==i.po]
    if len(orders)==1:
        p=orders[0]
        for field in ("site","buyer","location","origin","market","currency"):
            if not getattr(i,field):
                setattr(i,field,str(p[field])); provenance.append({"field":field,"value":str(p[field]),"source":f"PO {p['id']}","reference_version":refs.get("version")})
        sites=[s for s in refs.get("sites",[]) if str(s["id"])==i.site]
        if len(sites)==1 and not i.seller:
            i.seller=str(sites[0]["seller"])
            provenance.append({"field":"seller","value":i.seller,"source":f"Site {i.site}","reference_version":refs.get("version")})
    if not i.taxCode:
        rules=[t for t in refs.get("taxRules",[]) if all(str(t[k])==getattr(i,k) for k in ("origin","market","currency"))]
        if len(rules)==1:
            i.taxCode=rules[0]["code"]
            provenance.append({"field":"taxCode","value":i.taxCode,"source":"Unique approved tax mapping","reference_version":refs.get("version")})
    return i,provenance


def validate(i: Invoice, refs, policy: Policy, ledger=(), reviewed=False):
    issues=[]; matches=[]
    def fail(code,message,owner="Accounts payable",line=None):
        issues.append({"code":code,"message":message,"owner":owner,"line":line})
    for f in ("number","seller","site","buyer","po","location","date","currency","origin","market","taxCode"):
        if not getattr(i,f): fail("MISSING",f"Missing {f}")
    try: date.fromisoformat(i.date or "")
    except ValueError: fail("DATE","A valid YYYY-MM-DD invoice date is required")
    decimals=policy.currency_decimals.get(i.currency)
    if decimals is None or not isinstance(decimals,int) or not 0<=decimals<=4:
        fail("CURRENCY","Configure the currency's decimal places"); decimals=2
    rounded=lambda x:x.quantize(Decimal(1).scaleb(-decimals),rounding=ROUND_HALF_UP)
    if any(x["invoice_key"]==key(i) for x in ledger): fail("DUPLICATE","Invoice already exported for this seller and buying company")
    sites=[s for s in refs.get("sites",[]) if str(s["id"])==i.site and str(s["seller"])==i.seller]
    if len(sites)!=1: fail("SITE","Supplier site and invoicing legal entity do not match","Supplier operations")
    elif i.supplier_name:
        normalized=lambda x:re.sub(r"[^\w]","",x.casefold())
        names=[sites[0]["name"],*sites[0].get("aliases",[])]
        if normalized(i.supplier_name) not in {normalized(x) for x in names}:
            fail("SUPPLIER_NAME","Invoice supplier name differs from the site's approved name or aliases","Supplier operations")
    if not any(all(str(r[k])==getattr(i,k) for k in ("site","buyer","origin","market","currency")) and r["status"]=="approved" for r in refs.get("routes",[])):
        fail("ROUTE","Trading relationship is not approved for this seller site, buyer, origin, market and currency","Supplier operations")
    orders=[p for p in refs.get("orders",[]) if str(p["id"])==i.po]
    p=orders[0] if len(orders)==1 else None
    if not p: fail("PO","No unique purchase order found","Buyer")
    else:
        if p.get("location_type") not in ("Store (S)","Warehouse (W)"):
            fail("LOCATION_TYPE","PO reference must provide an approved store/warehouse location type","Reference owner")
        if p["status"]!="open": fail("PO_STATUS","Purchase order is not open","Buyer")
        for f in ("site","buyer","location","currency","origin","market"):
            if str(p[f])!=getattr(i,f): fail("PO_SCOPE",f"PO {f} differs from invoice","Buyer")
    if not i.lines: fail("LINES","No invoice line items were read")
    used=defaultdict(lambda:Decimal(0)); within=defaultdict(lambda:Decimal(0)); net=Decimal(0)
    for e in ledger:
        for a in e["allocations"]: used[a["key"]]+=D(a["qty"])
    for n,l in enumerate(i.lines,1):
        candidates=[x for x in refs.get("items",[]) if str(x["site"])==i.site and ((l.gtin and str(x["gtin"])==l.gtin) or (not l.gtin and l.sku and str(x["sku"])==l.sku))]
        item=candidates[0] if len(candidates)==1 else None
        if not item: fail("ITEM","No unique exact supplier SKU / barcode match","Item steward",n)
        if item and l.gtin and l.sku and str(item["sku"])!=l.sku: fail("ITEM_CONFLICT","Barcode and supplier SKU identify different items","Item steward",n)
        if l.qty is None or l.qty<=0: fail("QTY","Quantity is missing or not positive",line=n)
        if l.price is None or l.price<0: fail("PRICE","Net unit price is missing or negative",line=n)
        if l.qty is not None and l.price is not None: net+=rounded(l.qty*l.price)
        pls=[x for x in (p or {}).get("lines",[]) if item and str(x["item"])==str(item["id"])]
        if len(pls)!=1:
            fail("PO_LINE","Cannot select one PO line for this item","Buyer",n); continue
        pl=pls[0]; allocation=f"{p['id']}|{pl['id']}"
        if l.uom!=pl["uom"]: fail("UOM","Unit differs; an approved conversion is required","Buyer",n)
        if l.price is not None and abs(l.price-D(pl["price"]))>policy.price_tolerance: fail("PRICE","Unit price differs from the PO beyond tolerance","Buyer",n)
        received=sum((D(r["qty"]) for r in refs.get("receipts",[]) if str(r["po"])==str(p["id"]) and str(r["line"])==str(pl["id"]) and r["status"]=="accepted"),Decimal(0))
        available=received-D(pl["invoiced"])-used[allocation]
        within[allocation]+=l.qty or Decimal(0)
        if within[allocation]>available: fail("RECEIPT",f"Needs {within[allocation]} units; {available} accepted received units remain","Receiving",n)
        if within[allocation]>D(pl["ordered"])-D(pl["invoiced"])-used[allocation]: fail("ORDER_QTY","Exceeds remaining ordered quantity","Buyer",n)
        matches.append({"line":n,"item":str(item["id"]),"gtin":l.gtin or str(item.get("gtin","")),"po_price":str(pl["price"]),"available":str(available),"allocation":{"key":allocation,"qty":str(l.qty or 0)}})
    if i.net is None or abs(rounded(net)-i.net)>policy.total_tolerance: fail("TOTAL",f"Calculated net {rounded(net)} differs from the invoice net")
    taxes=[t for t in refs.get("taxRules",[]) if all(str(t[k])==getattr(i,k) for k in ("origin","market","currency")) and t["code"]==i.taxCode]
    if len(taxes)!=1: fail("TAX","No unique approved tax mapping","Tax reviewer")
    elif i.tax is None or abs(rounded(net*D(taxes[0]["rate"]))-i.tax)>policy.total_tolerance: fail("TAX_TOTAL","Tax differs from the configured rule","Tax reviewer")
    if not reviewed: fail("REVIEW","Compare values and derived references with the document, then confirm review")
    return {"ready":not issues,"issues":issues,"matches":matches,"net":str(rounded(net)),"reference_version":refs.get("version"),"location_type":p.get("location_type") if p else None}
