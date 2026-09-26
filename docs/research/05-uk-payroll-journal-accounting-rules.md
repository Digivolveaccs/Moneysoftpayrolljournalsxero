# UK payroll journal accounting rules and edge cases the pipeline must reconcile (2025/26 and 2026/27)

Research note for the Moneysoft -> Xero payroll-journal pipeline. Written 26 September 2026.

**How this was researched, and the caveat that goes with it.** This session had no working
web access: the WebSearch budget was already exhausted (200/200) before the first query, and
every direct fetch (gov.uk, central.xero.com, moneysoft.co.uk) was refused by the egress proxy
(`CONNECT tunnel failed, response 403`). What *was* available and was actually read:

- the practice's own skills (`browns-payroll-journal`, `payroll-agent` + Moneysoft playbook,
  `xero-payroll-backfill`, `xero-journal-creator` import-format reference);
- this repo's earlier research notes `docs/research/01..03` (which *did* have web access and quote
  Moneysoft's Employment Allowance, statutory-recovery, CIS-suffered and RTI-corrections pages);
- the pipeline code (`src/msx/journal_builder.py`, `poster.py`, `mapping.py`, `p30.py`);
- **one live source**: the chart of accounts of a real UK Xero organisation (MPH & Associates
  Limited T/As Digivolve Accountants) read through the Xero MCP connector, used to verify the
  Xero UK default payroll codes and their stock descriptions.

Everything on tax rates, thresholds, HMRC procedure and provider practice below is therefore
**best knowledge as at a mid-2026 training cut-off**, labelled with a confidence level, and the
gov.uk / Xero / Moneysoft URLs listed are the canonical pages to verify against; they were **not
fetched in this session**. Where a 2026/27 figure is given it is the figure I believe was
published for 6 April 2026 and must be checked against the live "Rates and thresholds for
employers 2026 to 2027" page before it is hard-coded anywhere. Design decisions are written so
that they hold even if a specific number turns out to be wrong.

## Summary

- **The wages journal has exactly one number that must agree with an external party (HMRC): the
  PAYE control credit.** It equals PAYE tax + employee NIC + employer NIC + student/postgraduate
  loan deductions, *less* Employment Allowance, *less* statutory-pay recovery and compensation,
  *less* CIS suffered set off on the EPS, *plus* Apprenticeship Levy. Today `journal_builder.py`
  only subtracts EA; any client with an EPS item other than EA will HOLD every month (by design,
  currently) until the builder learns the other three items and where Moneysoft prints them.
- **Employment Allowance is £10,500 for 2025/26 and (to my knowledge) unchanged for 2026/27; the
  £100,000 prior-year secondary NIC cap was removed from 6 April 2025**, so many more of the ~260
  employers can claim. It is claimed on an EPS once per tax year and absorbs employer NIC
  cumulatively until used up. The explicit `Dr PAYE control / Cr Employer NIC cost` pair is the
  right presentation. One correctness gap: a claim made part-way through the year is applied
  retrospectively to the whole year, so in the claim month the EA figure can legitimately exceed
  that month's employer NIC; the builder's `ea > er_nic` HOLD must compare year-to-date, not month.
- **Statutory-pay recovery changed materially from 6 April 2025**: Small Employers' Relief
  compensation rose from 3% to 8.5% (recovery 108.5% for employers whose Class 1 NIC in the prior
  tax year was <= £45,000; 92% for everyone else), and Statutory Neonatal Care Pay was added to
  the recoverable list. Recovery is an EPS item that reduces the HMRC payable; the journal shows it
  as `Dr PAYE control / Cr statutory-pay cost` (recovery) and `Cr other income or Cr employer NIC
  cost` (the 8.5% compensation). SSP is never recoverable.
- **CIS suffered must stay out of the wages journal.** The practice already books it from the
  receipts side (`cis-tax-deducted`: Dr 821-YY-YY / Cr 1300, one journal per tax month dated the
  5th). The wages journal's PAYE control credit should therefore be the liability *before* CIS
  set-off, and the reconciliation must add back the EPS CIS-suffered figure if Moneysoft's "Total
  Tax & NIC Due" / P30 is printed net of it (undocumented; verify on a CIS client).
- **Pensions**: the deduction printed on the Employer's Summary is what the provider collects -
  100% of the employee rate under a net pay arrangement, 80% of it under relief at source (NEST,
  People's Pension default, most GPPs). The journal never needs to gross it up; the per-member
  `pensions payable` line (EE + ER) is correct because the provider's contribution schedule and
  Direct Debit are per member. Opt-out refunds inside the one-month window produce a *negative*
  employee deduction that the builder must accept (Dr pensions payable) rather than HOLD on.
- **Directors' NI, the £5,000 secondary threshold and the 15% rate** make employer NIC lumpy and
  sometimes nil for months, and the year-end "alternative method" true-up can make a director's
  NIC line negative in the final period. The builder must allow signed per-employee NIC lines.
- **Xero UK defaults, verified live**: 477 Salaries, 479 Employers National Insurance, 482 Pensions
  Costs, 814 Wages Payable - Payroll, 825 PAYE Payable, 826 NIC Payable, 835 Directors' Loan
  Account, 858 Pensions Payable, 868 Earnings Orders Payable. 478 Directors' Remuneration is a
  default in fresh UK orgs but was absent (archived/renamed) in the org inspected - never assume a
  default exists. In that org **477 carries a default tax rate of 20% (`INPUT2`)**, so the journal
  must always send `LineAmountTypes: NoTax` / tax type `NONE` explicitly.
- **Re-filed periods**: from 2020/21 onwards there is no EYU; corrections are a further FPS with
  revised year-to-date figures (in-year or after 19 April). The pipeline should treat "filed-row
  count went up for a period we already journaled" as a *supersede* event: replace a DRAFT, but
  never edit a POSTED journal - post a delta correction journal (narration
  `Payroll correction - Mon YYYY (Mn) v2`) and flag it. The existing `duplicate_guard` already
  HOLDs on a figure mismatch, which is the safe default until that behaviour is built.

## Findings

### 1. Employment Allowance (EA): amount, eligibility, claim, application, presentation

**Confidence: high** (amount, threshold removal, claim mechanism); **medium** (2026/27 unchanged;
state-aid rules).

Claim: For 2025/26 the annual EA is **£10,500** (up from £5,000), and the rule that excluded
employers whose previous-year secondary Class 1 NIC liability exceeded £100,000 was **removed
from 6 April 2025**, so eligibility is now essentially: an employer with a secondary Class 1 NIC
liability, that is not a public body doing >50% public work, not employing someone for personal
/ household work (care/support workers excepted), and **not a company whose only employee paid
above the secondary threshold is a director** (the single-director exclusion still applies; a
company with two directors both above the ST, or a director plus one employee above the ST,
is eligible). Connected companies / charities share one allowance and must nominate which PAYE
scheme claims it. Because the £100k cap is gone, the de minimis state-aid declaration that used to
be required for some sectors is (to my knowledge) no longer part of the claim from 2025/26. I am
not aware of any change to the amount or rules announced for 2026/27 (Autumn Budget, 26 November
2025); treat £10,500 as continuing unless the 2026/27 rates page says otherwise.

Claiming and applying: The claim is made on an **EPS** (tick "Employment Allowance indicator" and
give the business sector), **once per tax year** - it does not roll forward, so a new-year claim
must be re-sent each April (Moneysoft: Employer > Employer Details, the EA tick, which schedules the
EPS row the payroll-agent already reads). HMRC then reduces the employer's **secondary Class 1
NIC only** (not Class 1A/1B, not employee NIC, not PAYE) by the allowance **cumulatively across the
tax year** until £10,500 is used. In practice this means: full absorption of employer NIC each
month while the running total is below £10,500, then a partial month, then nil. A claim made late in
the year is applied to the whole year from 6 April, so the *first* month after the claim can carry
an EA figure larger than that month's employer NIC (the year-to-date catch-up). An unused balance
cannot be carried into the next year.

What the practice's sources say: Moneysoft's EA guide (quoted in research note 01): the Employer's
Summary for tax period "will always show the EA (if applicable)" and EA "serves to reduce the total
Employer NIC due to HMRC in that particular period"; `Analysis > Employer's NIC Allowance` shows the
month-by-month use and "when the allowance has been 'used-up'". The payroll-agent skill: "EA absorbs
employer NIC until £10,500 in the year is used up, posted Dr PAYE control / Cr Employer NIC. Take it
from the EPS row, never inferred from a quarterly P30".

Journal presentation: the practice convention (`browns-payroll-journal`) - employer NIC debited
gross per employee to the NIC cost code, PAYE control credited with the gross liability, then an
explicit pair `Dr paye_payable / Cr er_nic_cost` for the EA - is correct under FRS 102 (EA is a
reduction of the employer's NIC cost, not income) and keeps the control account equal to the P30.
The alternative some accountants use (Cr a separate "Employment Allowance" other-income line) is
acceptable but hides the true NIC cost and is not what the practice does. Never net it silently
into the employer NIC debit: a single-director company that is *not* eligible, or a client whose
claim HMRC rejected, would then be indistinguishable from a client whose allowance is simply used
up.

Sources (not fetched this session):
[gov.uk - Employment Allowance](https://www.gov.uk/claim-employment-allowance) ·
[gov.uk - EA eligibility](https://www.gov.uk/claim-employment-allowance/eligibility) ·
[gov.uk - Rates and thresholds for employers 2025 to 2026](https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2025-to-2026) ·
[Moneysoft - Employment Allowance](https://moneysoft.co.uk/support/employment-allowance/) (via research note 01) ·
practice skills `browns-payroll-journal/SKILL.md`, `payroll-agent/SKILL.md`.

### 2. Statutory payments recovery and Small Employers' Relief

**Confidence: high** (92% / SER structure, EPS, SSP not recoverable, SNCP introduced April 2025);
**medium-high** (8.5% compensation from April 2025); **medium** (Moneysoft presentation).

Claim: Employers recover statutory maternity, paternity, adoption, shared parental, parental
bereavement and (from 6 April 2025) **statutory neonatal care pay** from HMRC at **92%** of the
amount paid. Employers that qualify for **Small Employers' Relief** - total Class 1 NIC (employee +
employer, before EA) of **£45,000 or less in the qualifying tax year** (the complete tax year before
the one in which the qualifying/matching week falls) - recover **100% plus NIC compensation**.
Compensation was 3% (recovery 103%) up to 2024/25 and was raised to **8.5% (recovery 108.5%) from
6 April 2025** alongside the employer NIC increase; I believe it stays at 8.5% for 2026/27.
**SSP is not recoverable** (the Percentage Threshold Scheme ended in April 2014); it is simply part
of gross pay. From 6 April 2026 the Employment Rights Act changes make SSP payable from day one and
remove the lower-earnings-limit condition (SSP at the lower of the flat weekly rate or 80% of
average weekly earnings) - more clients will pay SSP, but it still never touches the HMRC line.
Weekly statutory rates: £187.18 (2025/26); 2026/27 figure to verify (I believe £194.66); SSP
£118.75 (2025/26), 2026/27 to verify.

Mechanism: Recovery and compensation are reported on the **EPS as year-to-date figures** for the
tax month, and HMRC deducts them from what the employer owes for that month. If the EPS arrives
after the 19th of the following month, HMRC applies the recovery to the *next* month's charge - a
timing difference on HMRC's ledger, not in the books. If recovery exceeds the month's liability the
excess is carried forward within the year (or advance funding can be requested from HMRC).

What Moneysoft does (from research note 01, quoting moneysoft.co.uk): statutory pay is entered on
the employee's pay screens; recovery and compensation are "reported to HMRC via an EPS" and Payroll
Manager "will automatically schedule an EPS on the 'Pay – Employers RTI schedule' screen for tax
periods that contain recoverable amounts". **Whether the Employer's Summary's Employer Totals block
("Total Tax & NIC Due") and the P30 are printed net of recovery/compensation is not documented
anywhere found** (note 01, Finding 5). Best-knowledge answer: they are net, because both reports
are built to show the amount payable to HMRC and Moneysoft treats EA the same way - but this must be
confirmed on one SMP client before the builder is taught the arithmetic.

Journal presentation:

| | Line | Code |
|---|---|---|
| Dr | Gross pay incl. SMP/SPP/etc (as printed, per employee) | employee `pay_code` (or a separate `statutory_pay_cost` if the client wants it visible) |
| Dr | PAYE control - statutory recovery (92% or 100% of the statutory amount) | `paye_payable` |
| Cr | Statutory pay cost / same gross code the SMP went to | `statutory_recovery_code` (defaults to the pay code) |
| Dr | PAYE control - SER compensation (8.5%) | `paye_payable` |
| Cr | Employer NIC cost (it is compensation for NIC) or Other income | `ser_compensation_code` |

Do not net recovery into gross pay: the P&L should still show the full cost of the leave and the
recovery separately, and the auditor / accounts preparer expects to see the 92%/108.5% figure
against the EPS.

Sources (not fetched this session):
[gov.uk - Recover statutory payments](https://www.gov.uk/recover-statutory-payments) ·
[gov.uk - Rates and thresholds 2025-26 (statutory pay section)](https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2025-to-2026) ·
[Moneysoft - Statutory pay recovery and compensation](https://moneysoft.co.uk/support/statutory-pay-recovery-and-compensation/) (via research note 01) ·
[gov.uk - Statutory Neonatal Care Pay employer guide](https://www.gov.uk/employers-neonatal-care-pay-leave).

### 3. CIS suffered set-off via EPS (limited-company subcontractors)

**Confidence: high** (mechanism, limited companies only, YTD on EPS, excess carried forward);
**medium** (Moneysoft/P30 presentation).

Claim: A **limited company** that has CIS deductions taken from its own sales invoices may set them
against its PAYE/NIC/student-loan liability by reporting the **year-to-date CIS suffered on an
EPS** each month. Sole traders and partnerships cannot - they claim through Self Assessment. The
set-off is against the whole PAYE bill (tax, both NICs, student loans, Apprenticeship Levy), not
just employer NIC. If cumulative CIS suffered exceeds the cumulative liability, the excess rolls
forward inside the tax year; it cannot be repaid until after the year-end (final EPS, then a
written/online claim - HMRC's "Claim a refund of CIS deductions if you're a limited company" form,
optionally asking for set-off against Corporation Tax). HMRC may ask for the contractor's
deduction statements and will reject/adjust EPS figures it cannot match. From research note 01:
Moneysoft takes the figure at `Employer > CIS Suffered` and it "is submitted to HMRC on a monthly
EPS"; a figure found after the final EPS needs a further EPS (Moneysoft page "CIS suffered -
reporting a new figure after the final EPS has been sent").

Interaction with the practice's existing convention: `cis-tax-deducted` already posts, from the
Xero side, one journal per tax month dated the 5th, `Dr 821-YY-YY (tax-year PAYE code) / Cr 1300
(CIS suffered holding)`, narration "CIS Tax Deducted Month n", built from the client's own receipts.
That is the correct double entry: the CIS asset created when a customer pays net is cleared
against the PAYE liability. **Therefore the wages journal must credit the PAYE control with the
liability before CIS set-off** - if it also deducted CIS suffered, the set-off would be booked
twice and 1300 would never clear. Two consequences:

1. If Moneysoft's "Total Tax & NIC Due" / P30 is net of CIS suffered, `journal_builder.py`'s
   reconciliation (`tax + EE NIC + ER NIC + SL - EA == Total Tax & NIC Due`) needs the EPS CIS
   figure added back: `... - EA - statutory recovery - compensation - CIS suffered + levy == due`.
   Today such a client HOLDs every month with the message that already hints at this ("statutory
   recovery / CIS suffered / apprenticeship levy on the EPS?").
2. The amount Moneysoft carries as CIS suffered (typed in from statements) and the amount Xero's
   1300 accumulates from receipts can differ (missing statements, timing). That difference is a
   reconciliation item for the `paye-cis-reconciliation` working paper, not something the wages
   journal should absorb. The pipeline should record the EPS CIS figure per month in its ledger so
   the two can be compared.

Also note the practice uses **tax-year-specific PAYE codes (`821-25-26`, `821-26-27`)** for some
clients. For those clients the wages journal's `paye_payable` must roll to the new code at tax
month 1, or the control never ties. This is a mapping-schema question (see Implications).

Xero's own CIS feature creates locked system accounts (`CISASSET`, `CISLIABILITY`, `CISLABOUR...`;
`poster.py` already lists them) that reject manual journals - which is why the practice uses a
manually created 1300. Keep it that way.

Sources (not fetched this session):
[gov.uk - Claim a refund of CIS deductions if you're a limited company](https://www.gov.uk/guidance/claim-a-refund-of-construction-industry-scheme-deductions-if-youre-a-limited-company-or-an-agent) ·
[gov.uk - What you must do as a CIS subcontractor: getting paid](https://www.gov.uk/what-you-must-do-as-a-cis-subcontractor/get-paid) ·
[Moneysoft - CIS suffered after final EPS](https://moneysoft.co.uk/support/cis-suffered-reporting-a-new-figure-after-the-final-eps-has-been-sent/) (via note 01) ·
practice skill `cis-tax-deducted` (description in the skills list) · `src/msx/poster.py` system-account list ·
[Xero Central - Locked and system accounts](https://central.xero.com/0/article/Locked-and-system-accounts-in-your-chart-of-accounts) (via note 03).

### 4. Apprenticeship Levy

**Confidence: high** (rates and thresholds; unchanged for years); **low** (any renaming).

Claim: 0.5% of the employer's annual pay bill (earnings subject to Class 1 secondary NIC, whether
or not above the threshold), less a **£15,000 annual allowance** applied cumulatively at £1,250 per
month, so only employers with a pay bill above **£3 million** (or connected groups sharing the
allowance) pay anything. It is reported on the **EPS** (YTD) and paid with PAYE by the 22nd.
Political talk of a "Growth and Skills Levy" has not, to my knowledge, changed the charge. With 260
small employers the pipeline will essentially never see it; when it does, the journal line is
`Dr apprenticeship_levy_cost (or er_nic_cost) / Cr paye_payable`, and it *increases* the PAYE
control credit. Design rule: if the Employer Totals or EPS row shows a levy figure and the mapping
has no `apprenticeship_levy_cost`, HOLD.

Sources (not fetched this session): [gov.uk - Pay Apprenticeship Levy](https://www.gov.uk/guidance/pay-apprenticeship-levy).

### 5. Pensions: NPA vs RAS, salary sacrifice, postponement, re-enrolment, per-member reconciliation

**Confidence: high** (tax-relief mechanics, NEST = RAS, per-member schedules, opt-out refunds);
**medium** (qualifying-earnings band for 2026/27; Moneysoft's printed figure under RAS);
**medium** (salary-sacrifice NIC change from 2029).

Net pay arrangement (NPA): the employee contribution is deducted from **gross pay before tax** (but
after NIC - NI is charged on the full gross). The deduction printed is the full rate (e.g. 5% of
qualifying earnings). Tax relief is automatic at the employee's marginal rate; low earners below the
personal allowance get no relief (HMRC's "low earners top-up" from 2024/25 fixes that outside
payroll). Providers commonly on NPA: NOW: Pensions, many trust-based occupational schemes; People's
Pension and Smart offer either.

Relief at source (RAS): the employee contribution is deducted from **net pay after tax**; only
**80%** of the gross contribution is taken from the employee (e.g. a 5% rate becomes a 4% deduction)
and the provider claims the 20% basic-rate relief from HMRC. **NEST is RAS**, as are most contract
GPPs (Aviva, Royal London, Scottish Widows, Standard Life) and People's Pension by default.
Higher-rate taxpayers claim the extra relief via SA. From the journal's point of view the printed
deduction *is* the cash the provider collects; the 20% never passes through the employer's books.

Journal effect of each arrangement (the builder already does this correctly by taking the
printed figures at face value):

| | Gross pay line | Employee deduction | Payable to provider | ER cost |
|---|---|---|---|---|
| NPA | full gross | 100% of EE rate | EE + ER | ER |
| RAS | full gross | 80% of EE rate | 0.8 x EE + ER | ER |
| Salary sacrifice | gross **after** sacrifice (or gross with a pre-tax "sacrifice" deduction - depends on how it is set up in Moneysoft) | nil | ER (includes the sacrificed amount) | ER + sacrificed amount |

Salary sacrifice: the employee gives up salary and the employer pays the whole contribution.
Employer NIC is saved on the sacrificed amount, so it also reduces EA usage. Moneysoft can show it
either as a reduced basic pay or as a pre-tax deduction line; in the second form Total Payments
is pre-sacrifice and the sacrifice appears in the Deductions layout - the builder must then route
that deduction to the *employer* pension cost, not to pensions payable as an employee amount.
Announced in the Autumn Budget 2025 (to my knowledge): from **April 2029** salary-sacrificed
pension contributions above £2,000 a year become subject to NIC - not relevant to 2025/26-2026/27
journals but worth a note in the mapping schema.

Auto-enrolment thresholds: earnings trigger £10,000; qualifying earnings band **£6,240 - £50,270**
(frozen for 2025/26 and, I believe, again for 2026/27 - verify DWP's annual review). Minimum 8% of
qualifying earnings, at least 3% employer.

Postponement: an employer may postpone assessment for up to three months from the start date /
staging / the date an employee first crosses the trigger; no contributions are due during
postponement, so a new starter's first one to three payslips carry no pension lines, then they
appear - the builder already tolerates line-count changes per employee. If the employee opts in
during postponement, contributions start from the opt-in date (no backdating). **Opt-out within the
one-month window** obliges the employer to refund contributions already taken, usually through the
next payslip as a **negative deduction** (and the employer's contribution is refunded by/kept back
from the provider). The builder must accept a negative `ee_pension` for a member (net effect: Dr
pensions payable) and a negative `er_pension`, rather than failing the "every deduction is
explained" check. Re-enrolment (every three years, plus the re-declaration of compliance) puts
previously opted-out staff back in, so pension lines reappear for names that used to have none -
no HOLD needed, but the run summary should say so because the client will ask.

Why one pensions-payable line per member: every provider (NEST, People's Pension, Smart, NOW,
Aviva) receives a per-member contribution schedule (employee £ + employer £ per pay reference
period) and collects the schedule total by Direct Debit (NEST: one DD per schedule, ~10 working
days after approval). The control account is reconciled by ticking the schedule against the
ledger, and a schedule query ("why is X's contribution different?") is only answerable if the
ledger carries X's line. NEST's charges (1.8% contribution charge + 0.3% AMC) are taken from the
member's pot, not invoiced to the employer, so the DD equals the schedule to the penny. Members
whose contributions are refunded (opt-outs) appear on a separate NEST refund - another reason the
per-member line matters.

Sources (not fetched this session):
[gov.uk - Workplace pensions: what you, your employer and the government pay](https://www.gov.uk/workplace-pensions/what-you-your-employer-and-the-government-pay) ·
[NEST - tax relief / relief at source](https://www.nestpensions.org.uk/schemeweb/nest/employers/pensionsandtax.html) ·
[The Pensions Regulator - postponement](https://www.thepensionsregulator.gov.uk/en/employers/new-employers/im-an-employer-who-has-to-provide-a-pension/postponement) ·
[TPR - re-enrolment](https://www.thepensionsregulator.gov.uk/en/employers/re-enrolment) ·
[gov.uk - Automatic enrolment earnings thresholds review](https://www.gov.uk/government/publications/automatic-enrolment-review-of-the-earnings-trigger-and-qualifying-earnings-band-for-2026-27) ·
practice skill `browns-payroll-journal/SKILL.md` ("Pensions payable named per member").

### 6. Student loans, attachments/DEA, payroll giving, childcare vouchers, advances, net pay to DLA

**Confidence: high** (who each deduction is paid to; 2025/26 thresholds); **low** (2026/27 student
loan thresholds - verify).

Student and postgraduate loans: deducted through payroll on the FPS and **paid to HMRC with PAYE**,
so they belong in the single PAYE control credit (as the builder does). Rates: Plan 1, 2, 4 and 5 at
9% above threshold; PGL at 6%. 2025/26 thresholds: Plan 1 £26,065; Plan 2 £28,470; Plan 4 £32,745;
Plan 5 £25,000 (first Plan 5 repayments from 6 April 2026, so Plan 5 codes only bite from 2026/27);
PGL £21,000. 2026/27 (to verify): Plan 1 ~£27,660, Plan 2 ~£29,385, Plan 4 ~£34,400, Plan 5
£25,000, PGL £21,000 (frozen). None of these numbers affects the journal; the pipeline only needs
the printed deduction and the rule "student loan -> PAYE control".

Attachment of earnings orders (AEO, council tax AEO, CMS/CSA DEO, Scottish earnings arrestments)
and **DWP Direct Earnings Attachments (DEA)**: deducted from **net pay** and paid to the **court,
council, CMS or DWP** - never to HMRC. Separate creditor, which is why `journal_builder.py` HOLDs
until the mapping has `attachments_payable`. Two refinements: (a) the employer may keep a **£1
administration charge** per deduction for most order types (Moneysoft supports it); if used it is a
further net-pay deduction credited to other income, not to the creditor; (b) if a client has orders
for more than one creditor, one `attachments_payable` code is still fine because the payment is
matched per order on the bank side, but the description must name the employee and order type.
Xero's UK default for this is **868 Earnings Orders Payable** (verified live).

Payroll giving (Give As You Earn): deducted from gross **before tax** (not before NIC) and paid to
an approved Payroll Giving Agency (CAF, Charities Trust etc.) - separate creditor
(`payroll_giving_code` already exists as an optional mapping code). Childcare vouchers: closed to
new joiners since 4 October 2018; existing members only; a salary-sacrifice deduction with exempt
limits of £55 / £28 / £25 per week by tax band; the voucher provider (Edenred, Computershare,
Sodexo) invoices the employer, so the deduction is credited to a vouchers creditor / against the
provider's bill (`childcare_code`). Salary advances and loan repayments: net-pay deductions
credited to the **employee loan asset** that was debited when the advance was paid
(`loans_repayment_code`); since 2024 HMRC lets advances be reported on the normal FPS rather than as
a separate payment. Rounding carried forward (Moneysoft "round net pay") is a tiny net-pay
deduction/addition to `rounding_deduction_code`.

Net pay to directors' loan accounts: where a director's salary is not physically paid, credit the
net pay to that director's DLA (Xero default **835 Directors' Loan Account**) instead of 814 - the
practice already expresses this with `net_by_name` / `dividend_tax_code`. Per-employee net-pay
credits to 814 are still right when wages are paid: a single "wages" bank payment is reconciled
against several 814 lines with Xero's find-and-match, or the client posts one bank transaction to
814 and the account clears in total.

Sources (not fetched this session):
[gov.uk - Rates and thresholds 2025-26 (student loan section)](https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2025-to-2026) ·
[gov.uk - Make benefit debt deductions from an employee's pay (DEA)](https://www.gov.uk/make-benefit-debt-deductions) ·
[gov.uk - Attachment of earnings employer guide](https://www.gov.uk/debt-deductions-from-employee-pay) ·
[gov.uk - Payroll Giving](https://www.gov.uk/payroll-giving) ·
[gov.uk - Expenses and benefits: childcare](https://www.gov.uk/expenses-and-benefits-childcare) ·
`src/msx/mapping.py` OPTIONAL_CODES · live Xero chart (868, 835, 814).

### 7. Directors' NI and the 2025/26 - 2026/27 NIC parameters

**Confidence: high** (2025/26 ST £5,000, 15%, annual earnings period, both methods);
**medium** (2026/27 values).

2025/26 Class 1 NIC: employee 8% between PT and UEL, 2% above; employer **15% above the secondary
threshold of £5,000 a year** (£416.67/month; down from £9,100 and 13.8%); PT £12,570; UEL £50,270;
LEL £6,500. The ST is legislated to stay at £5,000 until April 2028 and then rise with CPI; PT and
UEL are frozen; **for 2026/27 I expect all of these unchanged except possibly the LEL** - verify on
the 2026/27 rates page. Employer NIC on a director paid the £12,570 personal-allowance salary is
now £1,135.50 a year, which EA covers *only* if the company is EA-eligible (i.e. not a
single-director-only scheme) - hence many single-director clients were moved to a £5,000 salary in
2025/26 and others deliberately kept £12,570 and pay the NIC.

Directors have an **annual (or pro-rata from appointment) earnings period**. Under the standard
cumulative method no NIC is due until cumulative pay passes the annual thresholds, so employer NIC
is nil for the early months and then appears in a lump (a director on £12,570 paid monthly crosses
£5,000 in month 5 and pays 15% on everything above from then on); a bonus month can produce a large
one-off figure. Under the **alternative method** NIC is worked out per period like an employee, with
a mandatory **annual recalculation in the final period** (or on leaving) that can produce a large
positive *or negative* adjustment. Consequences for the pipeline: (a) per-employee ER NIC of 0.00
for a director is normal, not a parse failure; (b) a **negative** ER or EE NIC line for a director in
month 12 (or the leaving month) is legitimate and the builder must write it as a credit to the cost
code / debit to the control, not HOLD; (c) EA usage is front-loaded for director-heavy payrolls and
can be exhausted in a single month for a company with several well-paid directors; (d) the
`ea > er_nic` HOLD must be year-to-date aware (Finding 1).

Class 1A NIC (15% from 2025/26) on benefits in kind is outside the payroll journal until benefits are
payrolled; **mandatory payrolling of benefits was deferred to 6 April 2027**, after which Class 1A
will be reported on the FPS and paid monthly with PAYE - a 2027/28 change that adds a
`class1a_nic` component to the PAYE control. Class 1A on termination awards above £30,000 and
sporting testimonials is already collected in-year through the FPS and appears as employer NIC not
covered by EA.

Sources (not fetched this session):
[gov.uk - Rates and thresholds 2025-26](https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2025-to-2026) ·
[gov.uk - Rates and thresholds 2026-27](https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2026-to-2027) ·
[gov.uk - CA44 National Insurance for company directors](https://www.gov.uk/government/publications/ca44-national-insurance-for-company-directors) ·
[gov.uk - Mandating payrolling of benefits in kind (deferral to April 2027)](https://www.gov.uk/government/publications/mandating-the-reporting-of-benefits-in-kind-and-expenses-through-payroll-software).

### 8. PAYE payment timing, quarterly payers, payment reference, tax-month numbering

**Confidence: high.**

- Tax months run from the **6th to the 5th**; month 1 = 6 April - 5 May, month 12 = 6 March - 5
  April. The tax month is determined by the **pay date**, not the period worked: a monthly payroll
  paid on the 1st-5th of a calendar month sits in the *previous* tax month.
- Payment is due by the **22nd** of the following tax month if paid electronically (Faster Payments,
  BACS, CHAPS, online/telephone banking, Direct Debit), or the **19th** if paying by post/cheque.
  If the 22nd falls on a weekend or bank holiday the money must clear on the last working day
  before (Faster Payments on the day is accepted). Late payment carries interest (base + 4% from
  April 2025) and, for more than one late payment in a year, penalties.
- **Quarterly payment** is allowed where the employer's **average monthly PAYE/NIC/student-loan/CIS
  liability is under £1,500** (under £18,000 a year). Quarters end 5 July, 5 October, 5 January and 5
  April, payable by 22 July / 22 October / 22 January / 22 April. The journal remains monthly; only the
  cash timing and the P30 span (three months) change, which `p30.py` already treats as
  "informational". HMRC still expects an EPS/FPS every month.
- **Payment reference**: the 13-character Accounts Office reference (e.g. `123PA00012345`) followed
  by **four digits: YY = the tax year in which the period ends (2025/26 -> 26, 2026/27 -> 27) and MM =
  the tax month 01-12** - so month 1 of 2026/27 is `123PA0001234527 01` (written without the space).
  Quarterly payers use the last month of the quarter (03, 06, 09, 12). Paying early (before the 6th
  of the month the payment relates to) or with the wrong suffix causes HMRC to allocate it to the
  wrong month, which is the commonest cause of PAYE "underpayment" letters. The client email the
  payroll-agent drafts should therefore quote the reference *with* the suffix.
- Nil months: no payment, but an EPS "no payment due" (or a nil FPS where Moneysoft prints "Not
  paid") is still needed by the 19th to stop a specified charge.

Sources (not fetched this session):
[gov.uk - Pay employers' PAYE](https://www.gov.uk/pay-paye-tax) ·
[gov.uk - PAYE reference number](https://www.gov.uk/pay-paye-tax/reference-number) ·
[gov.uk - Running payroll: paying HMRC](https://www.gov.uk/running-payroll/paying-hmrc) ·
practice skill `payroll-agent/SKILL.md` ("Quarterly PAYE payers").

### 9. Xero UK default chart of accounts codes for payroll (verified live)

**Confidence: high** for the codes listed below (read from a live UK org through the Xero MCP
connector, 344 active accounts); **medium** for 478.

| Code | Name (Xero stock description) | Type |
|---|---|---|
| 477 | Salaries - "Payment to employees in exchange for their resources" | Direct cost (expense) |
| 478 | Directors' Remuneration - **not present** in the org inspected (it has a user-created 349 "Directors Remuneration"); 478 is the UK default name/code in a fresh org to my knowledge, but it can be archived or renamed | Overhead |
| 479 | Employers National Insurance - "Payment made for National Insurance contributions - business contribution only" | Overhead |
| 482 | Pensions Costs - "Payments made to pension schemes" | Direct cost |
| 480 | Staff Training (not payroll, listed because it sits between them) | Overhead |
| 803 | Wages Payable - "Xero automatically updates this account for payroll entries created using Payroll..." (older default) | Current liability |
| 814 | Wages Payable - Payroll - "Where this account is set as the nominated Wages Payable account within Payroll Settings, Xero allocates the net wage amount of each pay run..." | Current liability |
| 825 | PAYE Payable - "The Amount of PAYE tax due to be paid to the HMRC" | Current liability |
| 826 | NIC Payable - "The amount of a business' portion of National Insurance Contribution that is due to be paid to the HMRC" | Current liability |
| 835 | Directors' Loan Account - "Monies owed to or from company directors" | Current liability |
| 858 | Pensions Payable - "Payroll pension payable account" | Current liability |
| 868 | Earnings Orders Payable - "Payroll earnings order account" | Current liability |
| 860 | Rounding | Current liability |

Observations that matter for the pipeline:

1. **The default chart splits PAYE (825) from NIC (826)**, and Xero's own payroll module posts tax
   to 825 and both NICs to 826. The practice's convention is one control line to 825 (or 2210)
   holding the whole P30 figure, which is *better* for reconciliation to HMRC, but it means a client
   that used Xero Payroll or a bookkeeper before will have balances in 826 that the practice's
   journals never touch. The mapping should record a deliberate choice (`paye_payable` only, or
   `paye_payable` + `nic_payable` split) per client, and the run summary should flag a non-zero 826
   when the mapping does not use it.
2. **Default tax rate on 477 in the inspected org is `INPUT2` (20% VAT on expenses)** - the tax
   rate is a property of the account, so a journal line that omits an explicit tax type inherits it.
   The pipeline must send `LineAmountTypes: NoTax` and `TaxType: NONE` on every line (the CSV
   equivalent is `No VAT`), which research note 03 already recommends; this is the evidence for why.
3. None of these are system accounts (`SystemAccount` empty), so manual journals are accepted on
   all of them. Charts "vary wildly" (payroll-agent skill: Sage-style 2210/2200/7000 charts, 814
   meaning PAYE in one org) - so defaults are only a starting suggestion for a *new* mapping, and
   the mapping's `source` field must say where the codes came from.
4. Codes are max 10 characters and alphanumeric, so `821-25-26` style codes are valid (note 03).

Sources: live read of the Digivolve Xero organisation chart of accounts via the Xero MCP connector
(26 Sep 2026) · [Xero Central - Default chart of accounts (UK)](https://central.xero.com/s/article/Default-chart-of-accounts-UK) (not fetched) ·
`docs/research/03-xero-manual-journals-api-and-auth.md` Findings 2, 8, 13 · `payroll-agent/SKILL.md` ("Charts vary wildly").

### 10. Week 53, month 12 / year-end, P60s, corrections after 19 April, re-filed periods

**Confidence: high** (EYU abolition for 2020/21+, week 53 rule, P60 deadline, final-submission
mechanics); **medium** (Moneysoft's correction flow, from note 02).

- **Week 53** (also 54 / 56 for fortnightly / four-weekly) arises only for non-monthly payrolls
  whose regular pay day falls on 5 April (a Sunday in 2026 - so weekly-Sunday payrolls had a week
  53 in 2025/26; 5 April 2027 is a Monday). Tax for that period is calculated on a **week 1 /
  non-cumulative basis**, giving the employee an extra slice of personal allowance that HMRC later
  recovers by P800 or coding; NIC is unaffected. Accounting-wise it is simply an extra pay period
  inside tax month 12 - nothing special in the journal, but the period tag / narration scheme must
  allow a 53rd weekly period and the monthly roll-up for tax month 12 must include it. Monthly
  payrolls never have a period 13.
- **Year-end**: the last FPS (or an EPS) of the year carries the "final submission for the tax
  year" indicator and is due by **19 April**; **P60s to employees by 31 May**; P11D/P11D(b) and
  Class 1A payment by 6 July / 22 July (outside this pipeline unless benefits are payrolled). None
  of these create journal lines. EA, statutory recovery and CIS suffered are all year-to-date
  figures that reset at tax month 1.
- **Corrections**: the **Earlier Year Update (EYU) is not accepted for 2020/21 onwards**; a
  correction to any year from 2020/21 (including after 19 April) is made by sending **another FPS
  for that year with corrected year-to-date figures** (Moneysoft supports this from the RTI
  schedule; its "Payroll corrections and RTI returns" page, quoted in note 02, says the schedule can
  be used to resubmit deliberately, and its "Outstanding RTI Submissions" page warns that a
  duplicate accepted FPS makes HMRC "double up"). In-year corrections to an earlier period are
  either (a) a re-sent FPS for that pay date with revised YTD figures, or (b) simply corrected in
  the next period's FPS because YTD figures self-correct - HMRC's charge for the month is
  recalculated from the latest YTD either way. Overpayments recovered from a later payslip are
  negative additions in that later period.
- **What the pipeline must decide**: a re-run of a period the pipeline already journaled shows up
  as (i) the RTI schedule's filed-row count for that period going up, and/or (ii) a re-exported
  Employer's Summary whose totals differ from the ledger's `total_debits` / `paye_due` / payload
  fingerprint. The correct accounting response depends only on the Xero status of the earlier
  journal:
  - earlier journal is **DRAFT** (mode `draft`): delete/void the draft and import the corrected
    journal with the same narration - one journal per period is preserved;
  - earlier journal is **POSTED** (mode `post`) and the period is open: post a **delta journal**
    (corrected minus original, line by line, same codes) with narration
    `Payroll correction - Mon YYYY (Mn) v2` dated the same period end, and flag it. Voiding and
    re-posting is also legitimate in Xero but destroys the audit trail Matt's team relies on and
    breaks any bank reconciliation already matched to 814;
  - period is **locked** in Xero (lock date passed): date the delta on the first open day and say
    so in the narration (the `xero-journal-creator` reference already prescribes this).
  Today `poster.duplicate_guard` HOLDs on "same narration, different figures" and the payroll-agent
  rule is "prior-period differences are flagged, never corrected in the run". That is the right
  default until the delta-journal path is built and Matt has approved it.

Sources (not fetched this session):
[gov.uk - Fix problems with running payroll / correct an earlier year](https://www.gov.uk/payroll-errors) ·
[gov.uk - Payroll: annual reporting and tasks](https://www.gov.uk/payroll-annual-reporting) ·
[gov.uk - Week 53 payments](https://www.gov.uk/guidance/week-53-payments) ·
[Moneysoft - Payroll corrections and RTI returns](https://moneysoft.co.uk/support/payroll-corrections-and-rti-returns/) and
[Moneysoft - Outstanding RTI Submissions](https://moneysoft.co.uk/support/outstanding-rti-submissions/) (both via note 02) ·
`src/msx/poster.py` `duplicate_guard` · `xero-journal-creator/references/xero-import-format.md`.

## Implications for the pipeline design

1. **Generalise the PAYE-control identity in `journal_builder.py`.** Replace
   `tax + ee_nic + er_nic + sl - ea == total_tax_nic_due` with
   `tax + ee_nic + er_nic + sl + pgl + levy - ea - stat_recovery - ser_compensation - cis_suffered == due`,
   where every EPS item defaults to 0.00 and is sourced from the Employer Totals block (if
   Moneysoft prints it there) or from the EPS row of the RTI schedule (which the payroll-agent
   already reads for EA). Each non-zero EPS item needs its own mapping code
   (`statutory_recovery_code`, `ser_compensation_code`, `apprenticeship_levy_cost`) and its own
   explicit journal pair, mirroring the EA pair. CIS suffered is the exception: it is **excluded
   from the wages journal** (booked by `cis-tax-deducted` from the receipts side), so it is an
   add-back in the reconciliation only, and the pipeline records the EPS CIS figure per month for the
   PAYE rec.
2. **Make the EA sanity check year-to-date.** Keep `ea <= er_nic` as a warning, but only HOLD when
   `sum(EA, tax months 1..n) > sum(ER NIC, months 1..n)` or when the cumulative EA exceeds £10,500
   (constant per tax year in config, not code). Requires the ledger to store `ea` and `er_nic` per
   period, which it can do from the same build.
3. **Allow signed lines.** Negative ER/EE NIC (director alternative-method true-up, corrected
   periods), negative pension deductions (opt-out refunds), negative additions (overpayment
   recovery) are legitimate. The invariant is the per-employee net-pay derivation and the totals,
   not the sign of any component. Write a negative debit as a credit line and keep the description.
4. **Tax-year-aware PAYE code.** Add `paye_payable_by_tax_year: {"2025-26": "821-25-26",
   "2026-27": "821-26-27"}` (or a template `821-{yy}-{yy+1}`) to the mapping schema; resolve from
   the tax month of the period; HOLD if the resolved code is missing/archived in the org (the
   `GET /Accounts` check in note 03). Do the same for any client that splits 825/826.
5. **Derive the tax month from the pay date, not the calendar month.** `Mn` in the narration, the
   payment-reference suffix in the client email and the P30 match all key off the tax month.
   Add `pay_day` to the mapping and a `tax_month(pay_date)` helper; the `journal_date` rule
   (`month_end` | `pay_date`) already exists - document which tax month a `month_end` journal
   belongs to when the pay day is the 1st-5th.
6. **Supersede workflow (Finding 10).** Extend the ledger with a `version` per (client, period) and
   a `superseded_by` link; on a figure mismatch: DRAFT -> replace; POSTED -> build the delta,
   create it as DRAFT regardless of the client's mode, and notify; locked period -> re-date. Keep the
   present HOLD until this is built and approved.
7. **Weekly / fortnightly / four-weekly clients**: one journal per tax month built from
   "Employer's Summary for tax period" (which spans the whole tax month), period tags `W1..W53`
   rolled up to `Mn`; the month-12 roll-up must include week 53/54/56.
8. **Always send `NoTax` / `NONE`** (Finding 9.2), and validate on each run that every mapped code
   exists, is ACTIVE and is not a system account; suggest defaults 477/478/479/482/814/825/858/868
   only when *creating* a mapping and mark them `TBC-` until a human confirms.
9. **Schedule the 2027/28 change now**: mandatory payrolling of benefits adds Class 1A to the FPS
   and to the PAYE control from April 2027 - add `class1a_nic` to the parser's component list and
   the identity in (1) when Moneysoft's 2027-28 build ships.
10. **Per-client "archetype" test set** before switching any client from `draft` to `post`: plain
    salary; EA claimed mid-year; director on cumulative method crossing the ST; director on the
    alternative method at month 12; RAS pension with an opt-out refund; NPA pension; salary
    sacrifice; SMP with SER at 108.5%; SSP under the April 2026 day-one rules; AEO + DEA with the £1
    admin charge; CIS-suffered company; quarterly payer; weekly payroll with week 53; nil month;
    re-filed month. Each must reconcile to the Employer's Summary and P30 to the penny or produce a
    specific HOLD.

## Open questions / things to verify on the office machine

1. **Moneysoft Employer Totals / P30 vs EPS items**: on one SMP client (SER and non-SER if
   possible) and one CIS-suffered client, is "Total Tax & NIC Due" / "Tax & NIC due for <period>"
   printed net of statutory recovery + compensation and net of CIS suffered? Does the block print
   them as separate lines (as it does "NIC Employment Allowance")? This decides the parser fields
   and the identity in Implication 1.
2. **EA catch-up month**: on a client that claimed EA after tax month 1, does the Employer's Summary
   show the whole year-to-date allowance in the claim month, or spread? (Determines Implication 2.)
3. **Directors on the alternative method**: does the month-12 Employer's Summary print a negative ER
   NIC for the director when the true-up is negative, and how does pdftotext render the sign
   (leading minus, trailing minus, brackets)?
4. **Opt-out refunds**: how does the Deductions layout print a refunded pension contribution
   (negative deduction vs an addition line)?
5. **Salary sacrifice**: for a client with sacrifice, is Total Payments pre- or post-sacrifice, and
   does the sacrifice appear as a named deduction?
6. **AEO admin charge**: does the Employer's Summary print the £1 charge separately, and is it in
   Total Deductions?
7. **RTI schedule after a correction**: does a re-sent FPS for an earlier period add a new row or
   replace the existing row's date (affects the "filed-row count went up" supersede trigger).
8. **Live figures to confirm on gov.uk before hard-coding**: EA for 2026/27 (£10,500?), SER
   compensation 2026/27 (8.5%?), SER threshold (£45,000), statutory weekly rate 2026/27 (£194.66?),
   SSP 2026/27 rate, LEL 2026/27, student-loan thresholds 2026/27, AE qualifying-earnings band
   2026/27, late-payment interest rate.
9. **Xero**: confirm 478 "Directors' Remuneration" exists in a fresh UK org; confirm which of
   825/826 the client's previous bookkeeper used (non-zero 826 balances); confirm the "No VAT" rate
   maps to `TaxType: NONE` in every client org (note 03, question 6).
10. **Practice policy questions for Matt**: (a) delta-journal vs void-and-repost for corrections to
    POSTED journals; (b) whether SER compensation is credited to employer NIC cost or to other
    income; (c) whether the £1 AEO admin charge is taken and where it is credited; (d) whether
    clients on tax-year PAYE codes (`821-YY-YY`) should have the wages journal roll the code
    automatically at month 1 or HOLD for a human the first month of each tax year.
