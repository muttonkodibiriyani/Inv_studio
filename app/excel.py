import io
import re
from datetime import date
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

HEADERS={
"Header":["Transaction Number","Document","Supplier Site","Order No","Location","Location Type","Document Date","Total Cost Ex Tax","Tax Amount","Ref No. 1","Ref No. 2","Ref No. 3","Comment"],
"Tax_Breakdown":["Transaction Number","Tax Code","Tax Basis"],
"Details":["Transaction Number","Item","UPC","Unit Cost","Quantity","Unit Tax Code"]}


def text_cell(cell,value):
    # Explicit string typing prevents user-controlled formula injection.
    cell.value=str(value) if value is not None else ""
    cell.data_type="s"


def workbook(invoice,result):
    return batch_workbook([(invoice,result)])


def batch_workbook(entries):
    if not entries or any(not r["ready"] for _,r in entries):
        raise ValueError("Only validated invoices can be exported")
    w=Workbook(); w.remove(w.active)
    for name,cols in HEADERS.items():
        s=w.create_sheet(name); s.append(cols); s.freeze_panes="A2"
        for c in s[1]: c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="173D3B")
        for col in s.columns: s.column_dimensions[col[0].column_letter].width=max(18,len(str(col[0].value))+2)
    s=w["Header"]
    dv=DataValidation(type="list",formula1='"Store (S),Warehouse (W)"');s.add_data_validation(dv)
    for transaction,(invoice,result) in enumerate(entries,1):
        s=w["Header"];row=transaction+1
        loc=invoice.location or ""
        loc_type=result.get("location_type")
        if loc_type not in ("Store (S)","Warehouse (W)"):raise ValueError("Reference location type must be Store (S) or Warehouse (W)")
        vals=[transaction,invoice.number,invoice.site,invoice.po,loc,loc_type,date.fromisoformat(invoice.date),invoice.net,invoice.tax,"","","",""]
        for c,v in enumerate(vals,1):
            if c in (3,4,5) and re.fullmatch(r"[1-9]\d{0,14}",str(v)):s.cell(row,c,int(v))
            elif c in (2,3,4,5,6,10,11,12,13):text_cell(s.cell(row,c),v)
            else:s.cell(row,c,v)
        s.cell(row,7).number_format="m/d/yyyy";dv.add(s.cell(row,6))
        t=w["Tax_Breakdown"];t.append([transaction,None,invoice.net]);text_cell(t.cell(row,2),invoice.taxCode)
        for l,m in zip(invoice.lines,result["matches"]):
            s=w["Details"];n=s.max_row+1
            s.append([transaction,None,None,l.price,l.qty,None])
            for c,v in ((2,m["item"]),(3,m["gtin"]),(6,invoice.taxCode)):
                if c==2 and re.fullmatch(r"[1-9]\d{0,14}",str(v)):s.cell(n,c,int(v))
                else:text_cell(s.cell(n,c),v)
            s.cell(n,3).number_format="@"
            for c in (4,5):s.cell(n,c).number_format="0.0000"
    b=io.BytesIO();w.save(b);return b.getvalue()
