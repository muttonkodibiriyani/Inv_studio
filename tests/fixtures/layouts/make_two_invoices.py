"""Regenerate two-invoices-synthetic.pdf: two synthetic tax invoices bound in one PDF, one page each.

Run from the repository root: .venv/bin/python tests/fixtures/layouts/make_two_invoices.py
Every value is invented; the layout mirrors the columnar tax-invoice family already covered by
tests/test_columnar_tax_invoice.py so the native reader can read each part after the split.
"""

from pathlib import Path

from reportlab.pdfgen import canvas

X = (34, 56, 181, 233, 278, 331, 380, 447, 476, 534)
HEADER = [(X[0], "#"), (X[1], "Description"), (X[2], "Brand"), (X[3], "Quantity"), (X[4], "UOM"),
          (X[5], "Price (Excl. VAT),"), (X[6], "Amount (Excl. VAT),"), (X[7], "VAT,"), (X[8], "VAT Amount,"),
          (X[9], "Amount (Incl. VAT),")]
CURRENCY = [(X[5], "XYZ"), (X[6], "XYZ"), (X[8], "XYZ"), (X[9], "XYZ")]

INVOICES = [
    {"number": "ZZTI26-00000101", "buyer": "Sample Retail Co - Store 1", "net": "37.50", "vat": "1.88", "total": "39.38",
     "items": [(1, "Hydra Gel Cream 50ml,", "NORTHLEAF", "3.000", "Pcs", "12.50", "37.50", "5", "1.88", "39.38")]},
    {"number": "ZZTI26-00000102", "buyer": "Sample Retail Co - Store 2", "net": "24.00", "vat": "1.20", "total": "25.20",
     "items": [(1, "Lip Tint Coral LTC-04,", "NORTHLEAF", "4.000", "Pcs", "6.00", "24.00", "5", "1.20", "25.20")]},
]


def rows_for(invoice):
    rows = [[(220, "TAX INVOICE")], [(31, f"# {invoice['number']}")], [(31, "Date of Issuing: March 7, 2026")],
            [(31, "Date of Supply: March 7, 2026")], [(31, "Issued By:"), (300, "Issued To:")],
            [(31, "Example Trading LLC"), (300, invoice["buyer"])], [(31, "Reference #: 13000042"), (200, "Reference Date:")],
            HEADER, CURRENCY]
    for item in invoice["items"]:
        rows.append(list(zip(X, map(str, item))))
    rows += [[(379, f"Sub Total, XYZ: {invoice['net']}")], [(379, f"Total VAT, XYZ: {invoice['vat']}")],
             [(379, f"Total, XYZ: {invoice['total']}")], [(30, "Terms and Conditions:")], [(500, "Page 1 of 1")]]
    return rows


def build(path):
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    for invoice in INVOICES:
        y = 50
        for row in rows_for(invoice):
            for x, value in row:
                c.setFont("Helvetica", 7)
                c.drawString(x, 842 - y, value)
            y += 11
        c.showPage()
    c.save()


if __name__ == "__main__":
    build(Path(__file__).with_name("two-invoices-synthetic.pdf"))
