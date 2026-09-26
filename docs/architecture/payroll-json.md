# payroll.json - the parsed Employer's Summary

`msx.summary_parser.parse_files()` produces this from the Moneysoft
Employer's Summary (Medium layout plus, when Total Additions or Total
Deductions are non-zero, the Additions and Deductions layouts). Every amount
is a plain number in pounds, positive as printed; the builder decides debit
or credit. Omitted keys are 0.00.

```json
{
  "client": "Browns Garage (Haywards Heath) Limited",
  "period": "Apr-2026",
  "tax_year": "2026-27",
  "layouts_seen": ["additions", "deductions", "medium"],
  "employees": [
    {
      "name": "Ylva Alexandersson",
      "total_payments": 5068.45,
      "basic": 1047.50, "hourly": 0.0, "additions": 4020.95,
      "dividend": 4020.95, "holiday": 0.0, "sick": 0.0, "parenting": 0.0,
      "rounding_addition": 0.0,
      "total_deductions": 546.89, "dividend_tax": 546.89,
      "overpayment": 0.0, "payroll_giving": 0.0, "childcare": 0.0,
      "loans_repayment": 0.0, "rounding_deduction": 0.0,
      "ee_pension": 21.10, "student_loan": 0.0, "postgrad_loan": 0.0,
      "attachments": 0.0, "ee_nic": 0.0, "tax": 209.40,
      "net": 4291.06, "er_nic": 94.57, "er_pension": 15.83
    }
  ],
  "report_totals": { "same keys, from the report's Total row": 0 },
  "employer_totals": {
    "paye_tax": 2537.80, "total_tax_due": 2537.80,
    "ee_nic": 774.13, "er_nic": 1946.01,
    "employment_allowance": -1946.01, "total_nic_due": 774.13,
    "total_tax_nic_due": 3311.93,
    "hmrc_due_for_period": 3311.93, "hmrc_payment_for_period": 3251.20,
    "hmrc_balance_cf": 60.73,
    "ee_pension": 421.63, "er_pension": 316.24, "total_other_payments": 737.87,
    "total_net_pay": 31865.63, "total_net_outlay": 35915.43
  }
}
```

## How the groups must hang together (the builder proves all of this)

**Payments.** `total_payments = basic + hourly + additions`, and `additions`
must be fully explained by `dividend + holiday + sick + parenting +
rounding_addition`. In the Medium layout the Holiday/Sick/Parenting columns
are *inside* Total Additions, which is why the Additions layout matters.

**Deductions.** `total_deductions` covers only deductions with no column of
their own: `dividend_tax + overpayment + payroll_giving + childcare +
loans_repayment + rounding_deduction`. Pension, student loan, postgraduate
loan and attachments are separate columns and are NOT inside it. (The
Deductions layout's own "Deductions" total does include pension - stored as
`deductions_breakdown_total`, informational only.)

**Net.** `net = total_payments - ee_pension - student_loan - postgrad_loan -
attachments - ee_nic - tax - total_deductions`.

**Employer totals.** `employment_allowance` is stored as printed (negative).
`total_tax_nic_due` is what the PAYE control line must land on after the
allowance pair; without it the build holds because nothing proves the PAYE
line. `paye_tax + ee_nic + er_nic + student/postgrad loans - EA` must equal
it to the penny; a difference means an EPS item (statutory recovery, CIS
suffered, apprenticeship levy) that needs its own mapping - the build holds
and says so.

## Parsing safety

* Column boundaries come from the report's own Total row and every column is
  re-summed against it; a misread column is a hard Hold.
* A column title the parser does not recognise is a hard Hold (add it to
  `COLUMNS` in `msx/summary_parser.py`, then to `ADDITIONS`/`DEDUCTIONS` in
  `journal_builder.py` and to the client mapping if it needs a code).
* Names that pdftotext splits mid-word (`Y lva Alexandersson`) merge across
  layouts by ignoring spaces, so a split name never becomes a phantom
  employee. Two people with the same initial and surname are two people.
* Layouts that disagree about the same figure are a hard Hold.
