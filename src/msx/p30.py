"""P30 (Employer's Payslip) parsing - the independent cross-check.

The P30 is a different Moneysoft report from the Employer's Summary. Its
'Tax & NIC due for <span>' figure is what HMRC will be paid for the period,
net of Employment Allowance. When the P30 spans exactly the journal's period
it must equal the journal's PAYE control movement to the penny; a quarterly
P30 (three-month span) cannot prove a single month and is recorded as
'informational'.

Regexes proven on filed Jul-2026 PDFs (payroll-agent / xero-payroll-backfill).
"""
import re
from decimal import Decimal

from .summary_parser import pdf_to_text


def _f(s):
    return float(s.replace(",", ""))


def parse_p30_text(text):
    span = re.search(r"Employer's Payslip for ([A-Za-z]{3}-\d{4}"
                     r"(?: to [A-Za-z]{3}-\d{4})?)", text)
    due = re.search(r"Tax & NIC due for [A-Za-z]{3}-\d{4}"
                    r"(?: to [A-Za-z]{3}-\d{4})?\s+(-?[\d,]+\.\d{2})", text)
    pay = re.search(r"Payment for [^\n]*?(-?[\d,]+\.\d{2})", text)
    ref = re.search(r"Reference\s+([A-Za-z0-9/]{6,})", text)
    dl = re.search(r"To reach HMRC by\s+(\d{2}-[A-Za-z]{3}-\d{4})", text)
    pref = re.search(r"(\d{3}/[A-Z]{2}\d+)", text)
    return {"span": span.group(1) if span else None,
            "payment": _f(pay.group(1)) if pay else None,
            "reference": ref.group(1) if ref else None,
            "deadline": dl.group(1) if dl else None,
            "paye_ref": pref.group(1) if pref else None,
            "due_for_period": _f(due.group(1)) if due else None}


def parse_p30_file(path):
    return parse_p30_text(pdf_to_text(path))


def cross_check(p30, journal):
    """-> (status, message). status: 'ties' | 'mismatch' | 'informational'
    | 'missing'."""
    if not p30 or p30.get("due_for_period") is None:
        return "missing", "no P30 figure available to cross-check"
    due = Decimal(str(round(p30["due_for_period"], 2)))
    ours = journal.meta["paye_due"]
    if p30.get("span") == journal.period:
        if abs(due - ours) <= Decimal("0.005"):
            return "ties", f"P30 'Tax & NIC due for {journal.period}' {due} ties to the journal's PAYE control {ours}"
        return "mismatch", (f"P30 says {due} due for {journal.period} but the "
                            f"journal's PAYE control movement is {ours}")
    return "informational", (f"P30 spans {p30.get('span')} (quarterly payer?) "
                             f"- {due} due for the span; journal month {ours}")
