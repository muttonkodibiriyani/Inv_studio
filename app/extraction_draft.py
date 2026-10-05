"""An explicitly unvalidated workbook of source facts, with unknown codes blank."""
import io
from datetime import date
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from .excel import HEADERS, text_cell

DISCLOSURE = 'EXTRACTION_REVIEW_ONLY: Unvalidated source values. Blank fields need completion. Not approved for posting or payment; no receipts reserved.'


def extraction_batch_workbook(entries):
    book=Workbook();book.remove(book.active)
    book.properties.title='Invoice extraction — review copy'
    book.properties.description=DISCLOSURE
    for name,headers in HEADERS.items():
        sheet=book.create_sheet(name);sheet.append(headers);sheet.freeze_panes='A2'
        for cell in sheet[1]:
            cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='78521D')
            sheet.column_dimensions[cell.column_letter].width=max(18,len(cell.value)+2)
            cell.comment=Comment(DISCLOSURE,'Invoice Studio')
    for transaction,(invoice,source_name,revision) in enumerate(entries,1):
        header=book['Header'];header_row=transaction+1
        try: invoice_date=date.fromisoformat(invoice.date or '')
        except ValueError: invoice_date=invoice.date
        values=[transaction,invoice.number,invoice.site,invoice.po,invoice.location,None,invoice_date,invoice.net,invoice.tax,'','','',DISCLOSURE]
        for col,value in enumerate(values,1):
            if col in (2,3,4,5,6,10,11,12,13) or isinstance(value,str):text_cell(header.cell(header_row,col),value)
            else:header.cell(header_row,col,value)
        header.cell(header_row,7).number_format='yyyy-mm-dd'
        header.cell(header_row,2).comment=Comment(f'Source: {source_name}\nReviewed snapshot revision: {revision}\n'+DISCLOSURE,'Invoice Studio')
        tax=book['Tax_Breakdown'];tax.append([transaction,None,invoice.net]);text_cell(tax.cell(transaction+1,2),invoice.taxCode)
        details=book['Details']
        for line in invoice.lines:
            row=details.max_row+1
            details.append([transaction,None,None,line.price,line.qty,None])
            # A printed supplier SKU is not a proven internal target item ID.
            text_cell(details.cell(row,2),None)
            text_cell(details.cell(row,3),line.gtin)
            text_cell(details.cell(row,6),invoice.taxCode)
            details.cell(row,3).number_format='@'
            details.cell(row,4).number_format='0.0000';details.cell(row,5).number_format='0.0000'
            notes=[f'Supplier item code: {line.sku or "not read"}',f'Description: {line.description or "not read"}',f'Source page: {line.page or "not recorded"}']
            for field,label in (('net_amount','Printed line net'),('tax_amount','Printed line tax')):
                value=getattr(line,field,None)
                if value is not None:notes.append(f'{label}: {value}')
            if line.evidence:notes.append('Evidence: '+line.evidence)
            details.cell(row,2).comment=Comment('\n'.join(notes)+'\nInternal item mapping requires confirmation.','Invoice Studio')
    output=io.BytesIO();book.save(output);return output.getvalue()


def extraction_workbook(invoice, source_name, revision):
    return extraction_batch_workbook([(invoice,source_name,revision)])
