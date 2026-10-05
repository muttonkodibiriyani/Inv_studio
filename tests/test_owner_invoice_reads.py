"""Synthetic reads for layouts seen on a scanned supplier invoice: run-on OCR headers and printed dates."""
from app.layout_extract import _extract_printed_date


def test_ordinal_date_after_a_mid_line_date_label_is_kept_as_printed():
    lines = ["Tax Invoice  Order Number: SYN-7  Date: 3rd March 2031  Invoice Number: SYN-7"]

    assert _extract_printed_date(lines) == "3rd March 2031"


def test_full_month_and_ordinal_forms_are_printed_dates():
    assert _extract_printed_date(["Invoice Date: 21st March 2031"]) == "21st March 2031"
    assert _extract_printed_date(["Date: 2 March, 2031"]) == "2 March, 2031"
    assert _extract_printed_date(["Document Date: 03/04/2031"]) == "03/04/2031"


def test_other_dates_in_a_run_on_line_are_not_the_invoice_date():
    assert _extract_printed_date(["Ship Date: 2nd June 2031"]) is None
    assert _extract_printed_date(["Payment Due Date: 5th May 2031"]) is None
    assert _extract_printed_date(["Due Date: 5th May 2031  Date: 1st May 2031"]) == "1st May 2031"


def test_two_different_labelled_dates_stay_unresolved():
    assert _extract_printed_date(["Date: 1st May 2031", "Invoice Date: 2nd May 2031"]) is None
