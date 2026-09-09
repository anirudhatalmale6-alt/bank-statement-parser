"""Generate synthetic statement PDFs in three different layouts.

These exist so the parser can be tested (and demonstrated) without anyone's
real banking data.  Every name, number and account here is invented.

    python tools/make_sample_pdfs.py samples/

Layouts produced:

``sample_uk_debit_credit.pdf``   Date / Description / Debit / Credit / Balance,
                                 DD/MM/YYYY, £, opening balance, a wrapped
                                 description, and a second page.
``sample_us_signed.pdf``         Date / Description / Amount / Balance,
                                 MM/DD/YYYY, $, debits in (parentheses).
``sample_plain_no_header.pdf``   No heading row at all, DD Mon YYYY dates and a
                                 trailing-minus convention — the layout the
                                 parser has to handle positionally.

``expected.json`` is written alongside them: the ground truth used by the test
suite, so a regression in the parser fails a test rather than a client's books.
"""

from __future__ import annotations

import json
import os
import sys

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4

UK_ROWS = [
    ("01/03/2025", "Balance brought forward", None, None, 2450.00),
    ("03/03/2025", "CARD PAYMENT TO TESCO STORES 3421", 42.60, None, 2407.40),
    ("04/03/2025", "DIRECT DEBIT BRITISH GAS", 128.15, None, 2279.25),
    ("05/03/2025", "FASTER PAYMENT FROM J HOBBS REF INVOICE 2201 CONSULTING WORK FEBRUARY",
     None, 1850.00, 4129.25),
    ("07/03/2025", "CARD PAYMENT TO AMAZON.CO.UK", 1234.56, None, 2894.69),
    ("11/03/2025", "STANDING ORDER RENT", 950.00, None, 1944.69),
    ("14/03/2025", "CONTACTLESS PRET A MANGER", 4.85, None, 1939.84),
    ("18/03/2025", "TRANSFER FROM SAVINGS", None, 500.00, 2439.84),
    ("21/03/2025", "DIRECT DEBIT COUNCIL TAX", 187.00, None, 2252.84),
    ("24/03/2025", "CARD PAYMENT TO SHELL SERVICE STN", 68.42, None, 2184.42),
    ("26/03/2025", "INTEREST PAID", None, 1.37, 2185.79),
    ("28/03/2025", "CARD PAYMENT TO WAITROSE 0091", 96.30, None, 2089.49),
    ("31/03/2025", "MONTHLY ACCOUNT FEE", 5.00, None, 2084.49),
]

US_ROWS = [
    ("03/01/2025", "Beginning balance", None, 5120.44),
    ("03/02/2025", "POS PURCHASE WHOLE FOODS MKT #221", -87.19, 5033.25),
    ("03/04/2025", "ACH DEPOSIT PAYROLL ACME CORP", 3200.00, 8233.25),
    ("03/06/2025", "CHECK #1042", -1450.00, 6783.25),
    ("03/09/2025", "POS PURCHASE SHELL OIL 574", -52.88, 6730.37),
    ("03/12/2025", "ONLINE TRANSFER TO SAVINGS", -1000.00, 5730.37),
    ("03/15/2025", "ATM WITHDRAWAL 5TH AVE", -200.00, 5530.37),
    ("03/19/2025", "REFUND UNITED AIRLINES", 412.60, 5942.97),
    ("03/23/2025", "POS PURCHASE APPLE.COM/BILL", -9.99, 5932.98),
    ("03/28/2025", "SERVICE CHARGE", -12.00, 5920.98),
]

PLAIN_ROWS = [
    ("02 Apr 2025", "SALARY CREDIT NORTHWIND LTD", 2750.00, 3020.11),
    ("05 Apr 2025", "UTILITIES DD SEVERN TRENT", -64.20, 2955.91),
    ("08 Apr 2025", "GROCERIES SAINSBURYS 4412", -113.75, 2842.16),
    ("12 Apr 2025", "ONLINE PURCHASE ARGOS", -249.99, 2592.17),
    ("15 Apr 2025", "CASH WITHDRAWAL", -80.00, 2512.17),
    ("19 Apr 2025", "REFUND ARGOS", 249.99, 2762.16),
    ("22 Apr 2025", "MOBILE BILL VODAFONE", -31.50, 2730.66),
    ("30 Apr 2025", "BANK CHARGES", -3.00, 2727.66),
]


def _money(v: float) -> str:
    return f"{v:,.2f}"


def make_uk(path: str) -> None:
    c = canvas.Canvas(path, pagesize=A4)
    rows_per_page = 8

    def header(page_no: int) -> float:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, H - 50, "NORTHBRIDGE BANK PLC")
        c.setFont("Helvetica", 9)
        c.drawString(40, H - 66, "Current Account Statement")
        c.drawString(40, H - 78, "Account number 12345678   Sort code 40-11-22")
        c.drawString(40, H - 90, "Statement period 1 March 2025 to 31 March 2025")
        c.drawRightString(W - 40, H - 50, f"Page {page_no} of 2")
        y = H - 120
        c.setFont("Helvetica-Bold", 9)
        c.drawString(40, y, "Date")
        c.drawString(105, y, "Description")
        c.drawRightString(400, y, "Debit")
        c.drawRightString(470, y, "Credit")
        c.drawRightString(550, y, "Balance")
        c.line(40, y - 4, 550, y - 4)
        c.setFont("Helvetica", 9)
        return y - 20

    y = header(1)
    for n, (date, desc, dr, cr, bal) in enumerate(UK_ROWS):
        if n and n % rows_per_page == 0:
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(40, 60, "Continued on next page")
            c.showPage()
            y = header(2)
        head, tail = desc, ""
        if len(desc) > 46:  # wrap long descriptions onto a second line
            cut = desc.rfind(" ", 0, 46)
            head, tail = desc[:cut], desc[cut + 1 :]
        c.drawString(40, y, date)
        c.drawString(105, y, head)
        if dr is not None:
            c.drawRightString(400, y, _money(dr))
        if cr is not None:
            c.drawRightString(470, y, _money(cr))
        c.drawRightString(550, y, _money(bal))
        y -= 14
        if tail:
            c.drawString(105, y, tail)
            y -= 14
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y - 10, "Closing balance")
    c.drawRightString(550, y - 10, _money(UK_ROWS[-1][4]))
    c.save()


def make_us(path: str) -> None:
    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, H - 50, "FIRST MERIDIAN BANK")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 66, "Checking Statement  |  Account ****4417")
    c.drawString(40, H - 78, "Statement Period: March 1, 2025 - March 31, 2025")
    c.drawRightString(W - 40, H - 50, "Page 1 of 1")
    y = H - 110
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Date")
    c.drawString(120, y, "Description")
    c.drawRightString(450, y, "Amount")
    c.drawRightString(550, y, "Balance")
    c.line(40, y - 4, 550, y - 4)
    c.setFont("Helvetica", 9)
    y -= 20
    for date, desc, amt, bal in US_ROWS:
        c.drawString(40, y, date)
        c.drawString(120, y, desc)
        if amt is not None:
            txt = f"(${_money(abs(amt))})" if amt < 0 else f"${_money(amt)}"
            c.drawRightString(450, y, txt)
        c.drawRightString(550, y, f"${_money(bal)}")
        y -= 15
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y - 8, "Ending balance")
    c.drawRightString(550, y - 8, f"${_money(US_ROWS[-1][3])}")
    c.save()


def make_plain(path: str) -> None:
    """No heading row, trailing-minus amounts — deliberately awkward."""
    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("Courier-Bold", 11)
    c.drawString(40, H - 50, "CALEDONIA BUILDING SOCIETY")
    c.setFont("Courier", 9)
    c.drawString(40, H - 64, "Statement for April 2025 - account 8891 2233")
    y = H - 100
    c.setFont("Courier", 9)
    for date, desc, amt, bal in PLAIN_ROWS:
        txt = f"{_money(abs(amt))}-" if amt < 0 else _money(amt)
        c.drawString(40, y, date)
        c.drawString(140, y, desc)
        c.drawRightString(450, y, txt)
        c.drawRightString(545, y, _money(bal))
        y -= 15
    c.save()


def expected() -> dict:
    """Ground truth: the signed amounts each file must yield."""
    uk = [
        {"date": "2025-03-%02d" % int(d[:2]), "amount": (cr or 0) - (dr or 0), "balance": bal}
        for d, _desc, dr, cr, bal in UK_ROWS[1:]
    ]
    us = [
        {"date": "2025-%s-%s" % (d[:2], d[3:5]), "amount": amt, "balance": bal}
        for d, _desc, amt, bal in US_ROWS[1:]
    ]
    plain = [
        {"date": "2025-04-%02d" % int(d[:2]), "amount": amt, "balance": bal}
        for d, _desc, amt, bal in PLAIN_ROWS
    ]
    return {
        "sample_uk_debit_credit.pdf": uk,
        "sample_us_signed.pdf": us,
        "sample_plain_no_header.pdf": plain,
    }


def main(outdir: str = "samples") -> None:
    os.makedirs(outdir, exist_ok=True)
    make_uk(os.path.join(outdir, "sample_uk_debit_credit.pdf"))
    make_us(os.path.join(outdir, "sample_us_signed.pdf"))
    make_plain(os.path.join(outdir, "sample_plain_no_header.pdf"))
    with open(os.path.join(outdir, "expected.json"), "w", encoding="utf-8") as fh:
        json.dump(expected(), fh, indent=2)
    print(f"Wrote 3 sample statements + expected.json to {outdir}/")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples")
