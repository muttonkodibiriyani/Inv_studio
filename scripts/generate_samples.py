"""Generate synthetic fixtures only. Requires the test extra (reportlab)."""
import json
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium

ROOT=Path(__file__).resolve().parents[1]
out=ROOT/"samples";out.mkdir(exist_ok=True)
sites=[{"id":"91001","seller":"LUMENA_AE","name":"Lumena Beauty Trading LLC","country":"AE"},
       {"id":"92001","seller":"LUMENA_KW","name":"Lumena Kuwait WLL","country":"KW"}]
routes=[{"site":"91001","buyer":"RETAIL_AE","origin":"AE","market":"AE","currency":"AED","status":"approved"},
        {"site":"91001","buyer":"RETAIL_KW","origin":"AE","market":"KW","currency":"AED","status":"pending"},
        {"site":"92001","buyer":"RETAIL_KW","origin":"KW","market":"KW","currency":"KWD","status":"approved"}]
items=[{"id":"345000101","site":"91001","sku":"LUM-SER30","gtin":"00012345678905","description":"Lumena serum 30 ml","uom":"EA"},
       {"id":"345000102","site":"91001","sku":"LUM-CRM50","gtin":"00012345678912","description":"Lumena cream 50 ml","uom":"EA"},
       {"id":"345000101","site":"92001","sku":"KW-SER30","gtin":"00012345678905","description":"Lumena serum 30 ml","uom":"EA"}]
orders=[];receipts=[]
for number,received in [("70001",100),("70002",100),("70003",6)]:
 orders.append({"id":number,"site":"91001","buyer":"RETAIL_AE","origin":"AE","market":"AE","currency":"AED","location":"38001","location_type":"Store (S)","status":"open",
  "lines":[{"id":"1","item":"345000101","ordered":"100","invoiced":"0","price":"60","uom":"EA"},
           {"id":"2","item":"345000102","ordered":"100","invoiced":"0","price":"40","uom":"EA"}]})
 receipts.extend([{"id":f"GRN-{number}-{line}","po":number,"line":line,"qty":str(received),"status":"accepted"} for line in ("1","2")])
refs={"sites":sites,"routes":routes,"items":items,"orders":orders,"receipts":receipts,
      "taxRules":[{"origin":"AE","market":"AE","currency":"AED","code":"UEPV05","rate":"0.05"}]}
(out/"reference.json").write_text(json.dumps(refs,indent=2))

def invoice(filename,number,po,lines):
 c=Canvas(str(out/filename),pagesize=A4)
 c.setFillColor(HexColor("#163D3A"));c.rect(0,730,595,112,fill=1,stroke=0)
 c.setFillColor(HexColor("#D6F79F"));c.setFont("Helvetica-Bold",11);c.drawString(38,801,"LUMENA")
 c.setFillColor(HexColor("#FFFFFF"));c.setFont("Helvetica-Bold",25);c.drawString(38,762,"Supplier invoice")
 c.setFillColor(HexColor("#213D37"));c.setFont("Helvetica",11)
 texts=[f"Invoice: {number}","Supplier: Lumena Beauty Trading LLC","Date: 2026-10-04",f"PO: {po}","Currency: AED","Bill to: Retail UAE","Deliver to: Store 38001"]
 y=695
 for text in texts:c.drawString(38,y,text);y-=23
 c.setFillColor(HexColor("#EFF4ED"));c.rect(30,440,535,70,fill=1,stroke=0)
 c.setFillColor(HexColor("#213D37"));c.setFont("Helvetica-Bold",9);c.drawString(38,493,"ITEMS BEGIN")
 c.setFont("Helvetica",9);y=474;net=0
 for l in lines:
  c.drawString(38,y,f"{l['sku']}  {l['gtin']}  {l['description']}  {l['qty']}  EA  {l['price']:.2f}");y-=16;net+=l['qty']*l['price']
 c.setFont("Helvetica-Bold",9);c.drawString(38,420,"ITEMS END")
 c.setFont("Helvetica-Bold",13);c.drawString(350,368,f"Net total: {net:.2f}");c.drawString(350,340,f"Tax total: {net*.05:.2f}")
 c.setFont("Helvetica-Bold",16);c.drawString(350,304,f"Total: {net*1.05:.2f} AED")
 c.setFont("Helvetica",9);c.drawString(38,84,"SYNTHETIC DEMONSTRATION DATA. Not a real supplier invoice.")
 c.drawString(38,66,"Review the source document before approving any export.")
 c.save()
 return {"number":number,"supplier_name":"Lumena Beauty Trading LLC","date":"2026-10-04","po":po,"currency":"AED","net":str(net),"tax":str(net*.05),"lines":[{**l,"qty":str(l["qty"]),"price":str(l["price"]),"uom":"EA"} for l in lines]}

first=invoice("invoice.pdf","DEMO-2026-001","70001",[{**items[0],"qty":10,"price":60},{**items[1],"qty":5,"price":40}])
second=invoice("invoice-2.pdf","DEMO-2026-002","70002",[{**items[1],"qty":13,"price":40}])
for name,data in [("invoice.json",first),("invoice-2.json",second)]:
 for line in data["lines"]:
  line.pop("id",None);line.pop("site",None)
 (out/name).write_text(json.dumps(data,indent=2))
doc=pdfium.PdfDocument(out/"invoice.pdf");doc[0].render(scale=2).to_pil().save(out/"invoice-scan.png");doc.close()
print("Generated synthetic PDFs, image scan, JSON invoices and reference data")
