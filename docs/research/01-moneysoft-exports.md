# Moneysoft Payroll Manager: journal / nominal-ledger exports and report exports

Research date: 26 Sep 2026. Method: ~45 web searches (moneysoft.co.uk, accountingweb.co.uk, bookkeepers.org.uk, community.xero.com, brightpay.co.uk, staffology, third-party reviews) plus the practice's existing skills. Direct fetches of moneysoft.co.uk, accountingweb.co.uk and the ICB forum are blocked from this environment, so everything below rests on search-result snippets; where a snippet is the only evidence I say so. Nothing here was verified on the office machine.

## Summary

- **Moneysoft has no API and no direct Xero/Sage/QuickBooks integration.** The only bookkeeping outputs are (a) a `Pay > Accounting File` CSV "for import into your book-keeping software", (b) an automatic post into Moneysoft's own Money Manager, and (c) PDF/print/email of any report. The AccountingWEB line "posts the journal directly over to Xero" describes a competitor (BrightPay, which documents an API post), not Moneysoft. Confidence high.
- **The Accounting File exists but is undocumented on the web.** No Moneysoft support page, screenshot or column list was found. The two user data points are: it is aimed at "multiple departments … post the gross pay separately", and one bookkeeper found it "does not produce anything usable". Its exact columns, nominal-code setup, per-employee vs per-total granularity and period selection are unknown and must be tested on the office machine. Treat it as a *possible* upgrade, not a foundation.
- **The Employer's Summary for tax period is Moneysoft's own recommended "what do I owe HMRC" report** and always shows Employment Allowance; that is the right anchor for the PAYE control figure. A forum user notes it omits some deduction types (loan repayments), which matches our need for the Additions/Deductions layouts.
- **Structured (non-PDF) report exports are limited**: `Tools > Export Data > Export employee data to CSV` (standing data + YTD, used by BrightPay/Staffology migration), and a **copy-to-clipboard** on `Analysis > Employee Pay Totals` (and date-range on Departmental Analysis). No evidence of a CSV/Excel export for the Employer's Summary, P30 or payslips; those are print/PDF/email only. `File > Print All Reports > Save all open reports as a PDF file` is a practice-verified keyboard path, not a documented feature.
- **Bureau features are RTI-centric**: the RTI Batch Processor bulk-files FPS/EPS across clients; agent invoicing and fee calculation exist. No "print this report for every employer" batch, no employer-list export and no command-line switches were found. Multi-employer automation will remain one-file-at-a-time UI driving.
- **EPS items (SMP/SPP recovery + compensation, CIS suffered) live outside the pay grid**: entered under `Employer > CIS Suffered` / statutory pay screens, auto-scheduled as an EPS on the RTI schedule, and Moneysoft's guidance is that the Employer's Summary reflects EA. Whether the Employer Totals block also nets CIS suffered and statutory recovery is not stated anywhere found; it must be checked on a client with each item.
- **Design consequence**: the pipeline should keep PDF-parse-and-reconcile (current approach) as the trusted path, run a one-off structured test of the Accounting File and the clipboard export on the office VM, and only promote either if it reproduces the Employer's Summary totals to the penny across the ~5 client archetypes (EA / no EA / pension / student loan / statutory pay / CIS suffered).

## Findings

### 1. `Pay > Accounting File` exists and emits a CSV for bookkeeping software
**Confidence: high** (that it exists and lives under Pay); **low** for everything about its content.

What the sources say: an AccountingWEB reply states "Moneysoft does have an Accounting File option under Pay which will extract a .CSV file for import into your book-keeping software", with the context "if you have multiple departments and need to post the gross pay separately". The same thread adds Moneysoft "has its own book-keeping software, Money Manager, into which you can post entries directly" but the poster had "never heard of anyone using this". An ICB forum poster, asking for the best way to extract a monthly journal for Sage, said the Accounting File "does not produce anything usable" for their purposes and that the "Employer's Summary for tax period" report omits "some items such as loan repayments". Moneysoft's own overview lists "export CSV files to accounts software" and "accounts reconciliation" among features but never documents the export.

Inference (not stated by any source): the department comment implies the file is broken down by department (and probably by employee) rather than one line per nominal; the "nothing usable" comment suggests it is a data dump rather than a balanced debit/credit journal with nominal codes. No evidence of a nominal-code setup screen, a Xero/Sage/QuickBooks format chooser, or a pay-period selector.

Sources: [AccountingWEB – Payroll software](https://www.accountingweb.co.uk/any-answers/payroll-software-60) · [MyICB forum – Extracting a Monthly Journal](https://www.bookkeepers.org.uk/forum/?tid=89089) · [Moneysoft – Payroll Manager Overview](https://moneysoft.co.uk/payroll-manager-overview/)

### 2. No API, no direct Xero posting; the "posts the journal directly over to Xero" quote is about another product
**Confidence: high**

What the sources say: Expertsure: "Moneysoft does not connect directly with Xero but it can produce a CSV journal file for importing into Xero." hr.software review: "lacks API or integrations with accounting software (Xero, Sage, QuickBooks)" and "requires manual CSV exports for posting journals to cloud accounting platforms like Xero". Ranktora: "no API or integrations with accounting software". The AccountingWEB thread that contains "posts the journal directly over to Xero and it can create a Bacs file" is a multi-product recommendation thread in which BrightPay is being recommended (£89 + VAT bureau version); BrightPay documents an API journal post to Xero. A separate AccountingWEB thread says "Moneysoft, Brightpay, Kaypay and Staffology … will allow you to post the payroll journals to Xero" with "varying levels of integration", and another user: "I use Moneysoft and journal into Xero". Xero Community users on Moneysoft describe keeping a draft/manual journal each month. Moneysoft's change log (moneysoft.co.uk/update/PM_change_log.txt) exists; the snippet visible shows 2025 entries about attachment orders, expenses and the Help menu, and no search surfaced any Xero entry.

Sources: [Expertsure review](https://www.expertsure.com/uk/payroll/moneysoft-review/) · [hr.software review](https://www.hr.software/reviews/moneysoft-payroll-manager) · [Ranktora review](https://ranktora.com/moneysoft-payroll-manager-review/) · [AccountingWEB – Payroll software](https://www.accountingweb.co.uk/any-answers/payroll-software-60) · [AccountingWEB – Xero and payroll, what do you use?](https://www.accountingweb.co.uk/any-answers/xero-and-payroll-what-do-you-use) · [BrightPay – Xero API journal](https://www.brightpay.co.uk/docs/23-24/payroll-journals/xero/xero-submitting-your-journal-using-api/) · [Xero Community – UK Payroll](https://community.xero.com/business/discussion/8768630/) · [Moneysoft change log](https://moneysoft.co.uk/update/PM_change_log.txt)

### 3. Post to Money Manager is the only "direct" accounting link
**Confidence: medium**

What the sources say: reviews and Moneysoft copy describe "automatic posting of data to Money Manager and CSV files for other accounting systems". Nothing describes the format Money Manager receives, and it is not usable for Xero. Not worth pursuing except as a possible way to learn the Accounting File's structure (Money Manager presumably consumes the same file).

Sources: [Ranktora review](https://ranktora.com/moneysoft-payroll-manager-review/) · [AccountingWEB – Payroll software](https://www.accountingweb.co.uk/any-answers/payroll-software-60) · [Moneysoft overview](https://moneysoft.co.uk/payroll-manager-overview/)

### 4. The Employer's Summary for tax period is Moneysoft's canonical HMRC-liability report and always shows Employment Allowance
**Confidence: high**

What the sources say: Moneysoft's Employment Allowance guide: "The correct report to refer to when determining how much is owed to HMRC is the 'Pay – Employer's Summary for tax period' report, which will always show the EA (if applicable)" and EA "serves to reduce the total Employer NIC due to HMRC in that particular period". A month-by-month EA report exists at `Analysis > Employer's NIC Allowance`, which "will also show when the allowance has been 'used-up'". Employer notes appear at the bottom of the Employer's Summary for Tax Period. The practice's own skill confirms the Basic/Medium/Additions/Deductions layouts and the Employer Totals block (Total Tax & NIC Due, NIC Employment Allowance, TOTAL NET OUTLAY), and that "Employer's Summary for tax period" can be pointed at historic periods without moving the current pay period.

Sources: [Moneysoft – Employment Allowance](https://moneysoft.co.uk/support/employment-allowance/) · [Moneysoft – Handy tips](https://moneysoft.co.uk/support/handy-payroll-manager-tips/) · practice skill `browns-payroll-journal/SKILL.md`, `payroll-agent/SKILL.md`

### 5. Whether the Employer Totals block reflects EPS items (statutory recovery, CIS suffered) is not documented
**Confidence: low** (web silent; best-knowledge answer)

What the sources say: CIS suffered is entered via `Employer > CIS Suffered` and "submitted to HMRC on a monthly EPS"; SMP/SPP recovery and NIC compensation are "reported to HMRC via an EPS" and "Payroll Manager will automatically schedule an EPS on the 'Pay – Employers RTI schedule' screen for tax periods that contain recoverable amounts". No page describes the P30 or Employer's Summary layout in enough detail to say whether "Total Tax & NIC Due" / the HMRC ACCOUNT section is net of these.

Best-knowledge answer: Moneysoft's P30 (Tax & NIC payslip) and the Employer Totals block on the Employer's Summary are built to show the amount actually payable to HMRC, so they very likely deduct statutory recovery/compensation and CIS suffered in the same way they deduct EA; the ICB poster's complaint that the summary omits loan repayments is about the employee deduction columns, not the HMRC block. This must be verified on a real client for each item because the journal shape depends on it (CIS suffered is a Dr PAYE / Cr CIS-suffered-holding pair, statutory recovery is Dr PAYE / Cr the statutory pay cost code, and neither belongs in the employer-NIC line).

Sources: [Moneysoft – CIS suffered after final EPS](https://moneysoft.co.uk/support/cis-suffered-reporting-a-new-figure-after-the-final-eps-has-been-sent/) · [Moneysoft – Statutory pay recovery and compensation](https://moneysoft.co.uk/support/statutory-pay-recovery-and-compensation/) · [Moneysoft – RTI FAQs](https://moneysoft.co.uk/real-time-information-rti-faqs/)

### 6. Structured exports: employee-data CSV via Tools, and copy-to-clipboard on Analysis reports
**Confidence: high** for the two mechanisms; **medium** that nothing else exists.

What the sources say: Staffology, BrightPay, paiyroll and TimeKeeper migration guides all use `Tools > Export Data > Export Employee data to a CSV file`, "when asked what to export, choose Everything"; TimeKeeper warns not to open the file in Excel because it reformats sort codes/account numbers. This export carries employee standing data and YTD figures (it is what BrightPay uses for a mid-year import), not a per-period journal. An AccountingWEB reply says "go to employee pay totals (under analysis) and copy to clipboard to get data into an Excel sheet". Another AccountingWEB thread: "the date range option is available for employee pay totals and departmental analysis" (click date range, select first month, drag to last, OK), and that the basic (single-employer) edition may not allow it. Moneysoft's overview: "any report can be exported as a PDF for e-mailing"; payslips have a toolbar "Save as pdf" button. No source mentions CSV/Excel/text export or clipboard for the Employer's Summary, P30, YTD figures or payslips.

Sources: [Staffology – Import from Moneysoft](https://help.staffology.co.uk/payroll/administration/import-export/import-moneysoft.htm) · [BrightPay – Importing from Moneysoft CSV mid year](https://www.brightpay.co.uk/docs/24-25/moving-to-brightpay-from-another-payroll-software/importing-from-moneysoft/importing-from-moneysoft-using-a-csv-file-mid-year/) · [TimeKeeper – Importing into Moneysoft](https://help.timekeeper.co.uk/en/article/importing-into-moneysoft-1befrwz/) · [AccountingWEB – Sending moneysoft payslips](https://www.accountingweb.co.uk/any-answers/sending-moneysoft-payslips-to-employees) · [AccountingWEB – query on Payroll Manager reports](https://www.accountingweb.co.uk/any-answers/moneysoft-query-on-payroll-manager-reports) · [Moneysoft – Emailing payslips and reports](https://moneysoft.co.uk/support/emailing-payslips-and-reports/)

### 7. `File > Print All Reports > Save all open reports as a PDF file` is real but undocumented; it combines every open preview into one PDF
**Confidence: high** (practice-verified), web silent.

What the sources say: no Moneysoft page mentions it. The practice's playbook records the exact keyboard path (F10 chain), that it sits one item below "Print all open reports to the printer", that the save dialog defaults to `C:\Mac\Home\Documents`, and that stray open report windows get swept into the combined PDF (once producing extra payslips). The preview toolbar "Save as PDF" auto-splits/auto-names but is mouse-only and synthetic clicks do not reach the Parallels guest.

Sources: practice skill `payroll-agent/references/moneysoft-playbook.md`, `payroll-agent/SKILL.md`

### 8. Bureau features are RTI batch filing, agent invoicing and fee calculation; no batch reporting or employer-list export found
**Confidence: medium-high**

What the sources say: "All multi-company versions of Payroll Manager include the 'RTI batch processor'" which "can be used to file RTI returns for multiple clients consecutively at the click of a button"; agent versions PM100 and PM250 "handle an unlimited number of clients"; extras are "10 payslip layouts … agent invoicing and fee calculation facilities". A "Pay count report" support page exists (likely a per-file count, not cross-employer). No search returned a cross-employer report runner, an employer list export, or command-line parameters for `pm3.exe` (PC Matic lists the executable; the AccountingWEB "problems opening" thread discusses file association only). Data lives as one `<Employer> <YYYY-YY>.pay` per employer per year (default `Documents\Payroll`) with `.pfi` sidecars that a user observed "seem to contain the same info as the file selection menu"; the practice already reads `.pfi` field 12 as current pay-period end.

Sources: [Moneysoft – RTI Batch Processor](https://moneysoft.co.uk/support/rti-batch-processor/) · [Moneysoft – Agents and Accountants](https://moneysoft.co.uk/agents-accountants/) · [Moneysoft – Pay count report](https://moneysoft.co.uk/support/pay-count-report/) · [Moneysoft – Data file questions](https://moneysoft.co.uk/support/data-file-questions/) · [AccountingWEB – Moneysoft payroll (.pfi)](https://www.accountingweb.co.uk/any-answers/moneysoft-payroll-12) · [PC Matic – pm3.exe](https://www.pcmatic.com/company/libraries/fileextension/application.asp?appname=pm3.exe.html)

### 9. Departments are per-employee and reportable; relevant to any gross-pay split
**Confidence: high**

What the sources say: employee records have an optional Department field (default "Default"); a "Department Details" report was added under `Pay > Department Details` "to report on the pay of employees in a specific department"; "Departmental analysis" is a standard Analysis report with a date-range option. The Accounting File's stated use case is department-split gross pay (Finding 1).

Sources: [Moneysoft – New features and guides](https://moneysoft.co.uk/support/payroll-manager-new-features-guides/) · [Moneysoft overview](https://moneysoft.co.uk/payroll-manager-overview/) · [AccountingWEB – query on reports](https://www.accountingweb.co.uk/any-answers/moneysoft-query-on-payroll-manager-reports)

### 10. Other per-file screens useful to a pipeline
**Confidence: high**

What the sources say: `Pay > Tax and NIC Actually Paid` (records payments to HMRC, with Notes); `Pay > Employer's RTI schedule` (FPS/EPS rows, auto-EPS for recoverable amounts); a Submission Log; `Analysis > Employee Pay Details / Employee Pay Totals` "for both the current and previous pay periods"; emailing of payslips, P30 and reports through the user's SMTP/Outlook/Gmail with an email log. None of these produce machine-readable output except the clipboard copy on Pay Totals.

Sources: [Moneysoft – Handy tips](https://moneysoft.co.uk/support/handy-payroll-manager-tips/) · [Moneysoft – Submission log](https://moneysoft.co.uk/support/submission-log/) · [Moneysoft – HMRC have wrong figures](https://moneysoft.co.uk/support/what-to-do-if-hmrc-have-the-wrong-figures/) · [Moneysoft – Emailing payslips and reports](https://moneysoft.co.uk/support/emailing-payslips-and-reports/)

## Implications for the pipeline design

1. **No API path exists; the architecture stays "drive Moneysoft UI → structured extract → reconcile → post to Xero".** Do not budget any time looking for a Moneysoft connector. The Xero side can move from Chrome-driven CSV import to the Xero API (manual journals endpoint) independently of anything Moneysoft does.
2. **Keep the PDF route as the reference implementation.** The Employer's Summary (Medium + Additions + Deductions) plus P30 already reconcile to the penny in `build_payroll_journal.py`, and Moneysoft itself designates the Employer's Summary for tax period as the HMRC-liability source. Any new extract must agree with it before it is trusted.
3. **Run a one-off Accounting File trial on the office VM** (Pay menu, count the Down-arrows and record in ui_map). Capture: whether it asks for a period, a department, a file location; the header row; whether rows are per employee/department or per nominal; which of gross, PAYE, EE NIC, ER NIC, EA, EE/ER pension, student loan, AoE, SSP/SMP, net appear. If it is per employee with all components, it becomes a **second independent source** to cross-check the PDF parse (two-source agreement is the cheapest way to cut parse-failure holds). If it is gross-only or department-only, drop it.
4. **Trial `Analysis > Employee Pay Totals > copy to clipboard`** as a text source: in the Parallels guest the clipboard can be read from PowerShell (`Get-Clipboard`) and shipped to the Mac, avoiding pdftotext column-boundary risk entirely. Confirm it can be set to the single current period and that it includes ER NIC and pensions.
5. **Batch over employers stays file-by-file.** There is no batch reporting; the existing `.pfi` scan → open file → export → close loop is the right shape. The RTI Batch Processor could bulk-file FPS/EPS, but it removes the per-client verification gate the practice relies on; do not adopt it unless the filing/journal steps are decoupled.
6. **EPS-driven journal lines need their own detection.** Because CIS suffered and statutory recovery are entered outside the pay grid and surface on the EPS row of the RTI schedule, the pipeline should read the EPS row (as it already does for the EA claim) and treat the presence of an EPS with amounts as a trigger to expect extra lines and to HOLD until the Employer Totals treatment (Finding 5) is confirmed for that client type.
7. **Quarterly payers and nil months** already have rules; the Employer's Summary for tax period being selectable for historic periods means backfills and re-runs never need the current pay period moved.
8. **Failure points to design out**: stray report windows in the combined PDF (close-all before export), initials-vs-full-name matching, department splits changing line counts, EA exhaustion mid-year (take from report/EPS, never carry forward), locked Xero periods, repeating wages journals already in the org.

## Open questions / things to verify on the office machine

1. `Pay > Accounting File`: exact menu position (Down-arrow count after F10, p), dialog fields (period? department? format?), output filename, header row, one row per employee/department/nominal?, which components are present (ER NIC, EA, EE/ER pension, student loan, AoE, statutory pay, net pay), whether it can target a prior period without changing the current period.
2. Does Payroll Manager have any nominal-code setup screen (`Tools > Setup`, `Employer > Employer Details`, per-pay-element) that feeds the Accounting File? None found on the web.
3. Does the Employer Totals block (Total Tax & NIC Due / HMRC ACCOUNT / TOTAL NET OUTLAY) deduct CIS suffered and statutory recovery + compensation? Test on one CIS-suffered client and one SMP client, comparing with the P30 and the EPS row.
4. Is "Total Tax & NIC Due" on a quarterly-payer's monthly summary the month or the quarter-to-date? (P30 is known to span the quarter.)
5. `Analysis > Employee Pay Totals`: is copy-to-clipboard keyboard-reachable, can the period be restricted to the current month, does it include ER NIC, EE/ER pension, student loan, and is the text tab-delimited?
6. Does the preview toolbar of the Employer's Summary offer anything other than Print / Save as PDF / Email (e.g. Save as CSV, Copy)? The playbook says Save as PDF is mouse-only; check whether the toolbar buttons have accelerators or can be reached by Tab from inside the guest (PostMessage clicks by handle already work for other dialogs).
7. Does `pm3.exe` accept a file path argument (`pm3.exe "<file>.pay"`) or any `/print` style switch? Test from PowerShell in the guest.
8. Which Payroll Manager edition/version is installed (`Help > About`) and does its change log list any accounting/export changes after mid-2025 (fetch `moneysoft.co.uk/update/PM_change_log.txt` from the office network and grep for "account", "CSV", "export", "Xero").
9. Money Manager: is it installed, and does opening its payroll import reveal the Accounting File's schema?
10. Does the Employer's Summary "Deductions" layout show loan repayments, AoE and salary-sacrifice items the ICB poster found missing from the tax-period summary, or do those require the payslip/Pay Details screen?
