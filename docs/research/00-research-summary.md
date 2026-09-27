# Research summary - Moneysoft to Xero hands-free journals

Written 26 Sep 2026 for Matt and for whoever maintains `src/msx`. The seven notes beside this file are the evidence; the pipeline they shaped is described in [../architecture/pipeline.md](../architecture/pipeline.md). One caveat, stated once: the planned adversarial web-verification pass could not run (the session's search budget was exhausted and outbound fetches were blocked), so no claim below has been re-verified beyond what each note says about its own sources. Every confidence label is the source note's own.

## The answer in one paragraph

Moneysoft cannot push anything anywhere: no API, no scripting, no Xero link, and its one bookkeeping export (`Pay > Accounting File`) is undocumented. The trustworthy figures source is the report Moneysoft designates for "what do I owe HMRC", the Employer's Summary for tax period, which the payroll-agent already files as a PDF into Dropbox once the FPS is accepted. The hands-free design is: watch that folder from one always-awake Mac, reconcile every figure to the penny against the report's own totals and the P30, map codes through a per-client git-versioned file, and write one `PUT /ManualJournals` per client (POSTED, `Idempotency-Key`, read back afterwards) instead of driving Chrome. That is what BrightPay, Staffology and KeyPay do, and the practice's journal shape (one PAYE control line tied to the P30, Employment Allowance as an explicit Dr PAYE / Cr ER NIC pair) is better than theirs. Only two failures put wrong money in a ledger silently, reading the wrong file and posting with the wrong mapping, so the design spends its effort there, holds rather than corrects, and never edits history. The one unknown that could still change the architecture is Xero's March 2026 connection caps and how the "practice exemption" applies to a 260-org bespoke app; that is a question for Xero, not for more research.

## Decision-relevant facts

| Fact | Confidence | Source doc | What it means for the design |
|---|---|---|---|
| No API, macro language or Xero link; outputs are PDF/print/email, one CSV and a Money Manager post | high | [01-moneysoft-exports.md](01-moneysoft-exports.md), [02-moneysoft-automation-and-files.md](02-moneysoft-automation-and-files.md) · [overview](https://moneysoft.co.uk/payroll-manager-overview/) | Drive the UI, extract, reconcile, post; no connector to find |
| `Pay > Accounting File` CSV exists; columns, nominal setup and period selector undocumented; one user: "not usable" | high (exists) / low (content) | [01-moneysoft-exports.md](01-moneysoft-exports.md) · [AccountingWEB](https://www.accountingweb.co.uk/any-answers/payroll-software-60) | One VM trial; adopt only if it matches the Summary to the penny |
| The Employer's Summary for tax period is Moneysoft's recommended HMRC-liability report and always shows EA | high | [01-moneysoft-exports.md](01-moneysoft-exports.md) · [EA guide](https://moneysoft.co.uk/support/employment-allowance/) | Parse target and PAYE-control anchor; the P30 is the second report |
| The FPS Submission Log is inside the `.pay`; no receipt or response file on disk | high / medium (disk trace) | [02-moneysoft-automation-and-files.md](02-moneysoft-automation-and-files.md) · [Submission Log](https://moneysoft.co.uk/support/submission-log/) | Trigger is the filed PDF; the RTI schedule row count is the authority |
| Dropbox as the live pay-file store is against vendor advice; conflicted copies documented | high (vendor) / medium (support quote) | [02-moneysoft-automation-and-files.md](02-moneysoft-automation-and-files.md) · [Working from home](https://moneysoft.co.uk/support/working-from-home-payroll-manager/) | Single writer, conflicted-copy HOLD, stable-file gate, folder pinned offline |
| `PUT /ManualJournals` creates POSTED directly; `Idempotency-Key` supported; retention window undocumented | high / low (window) | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) · [idempotency](https://developer.xero.com/documentation/guides/idempotent-requests/idempotency/) | One journal per call; key covers in-run retries; cross-run dedupe is the ledger plus a GET by date |
| Pricing since 2 Mar 2026: Starter free/5 connections, Core ~£18/50, Plus ~£120/1,000 (certified); practice exemption stated, mechanism and cap relief undocumented | high (tiers) / medium (exemption) | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) · [pricing](https://developer.xero.com/pricing), [FAQ](https://developer.xero.com/faq/pricing-and-policy-updates) | Biggest external risk; `xero.apps` bridges. [04-connectors-and-benchmarks.md](04-connectors-and-benchmarks.md) quotes the legacy 25-org cap; 03 supersedes it |
| Custom Connections: one org each, £5/month ex VAT, bought inside the client's org | high | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) · [guide](https://developer.xero.com/documentation/guides/oauth2/custom-connections/) | ~£1,300/month and 260 client purchases: sandbox only |
| PKCE: no secret; access token 30 min; refresh token rotates on each use, dies after 60 days unused, 30-minute grace | high | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) · [PKCE flow](https://developer.xero.com/documentation/guides/oauth2/pkce-flow) | Keychain, atomic write, keep-alive refresh, one machine, alert at 7 days |
| Consent connects one org per flow; Bulk Connections needs Advanced/Enterprise tier plus a security assessment | high | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) | ~260 consent flows by one staff login; `msx auth login` loops |
| Granular scopes: `accounting.manualjournals`, `.read`, `accounting.settings.read`, `offline_access`; `accounting.journals.read` is gated | high | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) · [scopes](https://developer.xero.com/documentation/guides/oauth2/scopes/) | Request only these |
| Official Xero MCP server: single tenant, static or client-credentials auth, no refresh, no `where` filter | high | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) · [source](https://raw.githubusercontent.com/XeroAPI/xero-mcp-server/main/src/clients/xero-client.ts) | Not a posting engine; `xero_client.py` instead |
| BrightPay, Staffology and KeyPay post POSTED on finalise, dated the pay date; EA is opt-in and silently dropped when unmapped | high | [04-connectors-and-benchmarks.md](04-connectors-and-benchmarks.md) · [Staffology spec](https://github.com/SynergiTech/staffology-swagger), [BrightPay data](https://github.com/maddy-codes/FinalAppRevComp/blob/main/Main/pythonCollection/xero_output_journals.txt) | POSTED on a clean gate is market practice; keep the explicit EA pair |
| No Moneysoft-to-Xero connector exists | high (GitHub) / medium (commercial) | [04-connectors-and-benchmarks.md](04-connectors-and-benchmarks.md) · [HMRC list](https://github.com/hmrc/software-choices-frontend/blob/main/conf/SoftwareProviders.json) | Nothing to copy; native posting still needed a control layer (Almond Valley) |
| EA £10,500 from 2025/26; £100k prior-year NIC cap removed; claimed yearly on an EPS, applied cumulatively | high (not fetched) | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) · [gov.uk](https://www.gov.uk/claim-employment-allowance) | The `ea > er_nic` check must be year-to-date; verify 2026/27 |
| SER compensation 3% to 8.5% from 6 Apr 2025; SSP never recoverable | medium-high | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) · [gov.uk](https://www.gov.uk/recover-statutory-payments) | EPS items need their own codes and journal pairs |
| CIS suffered is set off on the EPS; the practice books it from receipts (`cis-tax-deducted`) | high | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) · [gov.uk](https://www.gov.uk/guidance/claim-a-refund-of-construction-industry-scheme-deductions-if-youre-a-limited-company-or-an-agent) | The wages journal credits PAYE before set-off; CIS is an add-back only |
| NEST and most GPPs are relief at source: the printed deduction is 80% and is what the provider collects | high | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) · [NEST](https://www.nestpensions.org.uk/schemeweb/nest/employers/pensionsandtax.html) | Never gross up; one payable line per member; accept negative refunds |
| Directors' annual earnings period: ER NIC nil then lumpy; the alternative-method true-up can be negative | high | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) · [CA44](https://www.gov.uk/government/publications/ca44-national-insurance-for-company-directors) | Signed NIC lines are legitimate; 0.00 is not a parse failure |
| Xero UK defaults verified live: 477, 479, 482, 814, 825, 826, 835, 858, 868; 478 absent; 477 had a 20% default rate | high | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) · [Xero Central](https://central.xero.com/s/article/Default-chart-of-accounts-UK) | Defaults are `TBC-` suggestions; always `NoTax`/`NONE`; check names too |
| EYU abolished from 2020/21; a correction is a further FPS with revised YTD figures | high | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) · [gov.uk](https://www.gov.uk/payroll-errors) | A re-filed period is a supersede event: HOLD with both figures |
| Two silent-wrong-money classes: wrong source, wrong mapping; ~70 modes catalogued, half observed live | high | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) | Effort goes on source guards and chart name/type checks |
| launchd `StartCalendarInterval` jobs missed in sleep run at wake; `StartInterval` and cron are skipped | high | [07-runtime-and-scheduling-options.md](07-runtime-and-scheduling-options.md) · [Apple](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html) | The repo plist uses `StartCalendarInterval`; a Mac mini removes sleep |
| Cowork scheduled tasks run remotely and cannot watch a local folder; local tasks need the app open and the Mac awake | high | [07-runtime-and-scheduling-options.md](07-runtime-and-scheduling-options.md) · [support](https://support.claude.com/en/articles/13854387-schedule-recurring-tasks-in-claude-cowork) | Trigger/reporter only, plus the Bright Manager tick |
| Routines: hourly minimum, no permission prompts, "green" only means the session exited; the Default allowlist refuses api.xero.com and dropboxapi.com | high | [07-runtime-and-scheduling-options.md](07-runtime-and-scheduling-options.md) · [Routines](https://code.claude.com/docs/en/routines) | Fallback only: Custom allowlist, own consent, own ledger, kept disabled |

## What Moneysoft can and cannot give us

It can give: any report as PDF (the toolbar button is mouse-only; the practice-verified path `File > Print All Reports > Save all open reports as a PDF file` sweeps every open window into one file); the Employer's Summary in four layouts, selectable for historic periods; the P30; an employee-data CSV via `Tools > Export Data` (standing data and YTD, not a period journal); copy-to-clipboard on `Analysis > Employee Pay Totals`; the `.pfi` sidecar (field 12 = current period end); a `.pay` path as `pm3.exe`'s only known argument; and, on PM100/PM250, an RTI batch processor with a forum history of mis-detecting due returns ([02-moneysoft-automation-and-files.md](02-moneysoft-automation-and-files.md)).

It cannot give: an API, a command-line print or export switch, a receipt for an accepted FPS, a documented `.pay` format (migration tools read the FPS XML or CSV instead), or a batch report runner. Whether the Employer Totals block nets statutory recovery and CIS suffered is undocumented ([01-moneysoft-exports.md](01-moneysoft-exports.md) finding 5) and decides the parser identity. Also: one `.pay` per employer per tax year, updates about every two months with an admin-rights prompt, Windows 11 mandatory since May 2026, and a licence forbidding concurrent use on two machines.

## Posting into Xero: options compared and the recommendation

| Option | Verdict |
|---|---|
| Chrome-driven CSV import (today) | No cap, but drafts need approval and the browser session, org switching and file chooser are the largest failure surface ([06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) PO9-PO11). Kept as fallback and audit copy |
| Accounting API, one PKCE app | Recommended and built (`poster.py`, `xero_client.py`): one `PUT` per client, POSTED or DRAFT by client mode, pre-flight `Organisation`/`Accounts`/`TaxRates`, read-back after write. Capped at 5 or 50 connections unless the exemption lifts caps |
| Several PKCE apps | Supported by `xero.apps`; each org may hold only two uncertified apps: a bridge, not a destination |
| App Partner certification (Plus) | Removes the cap; months of review meant for App Store apps; only if Xero says the exemption does not cover 260 orgs |
| Custom Connections; official MCP server | No: per-org cost bought client-side; single-org with no refresh |
| Switch payroll product | Buys a mapping UI and an API, not the control layer; 260 mid-year migrations; BrightPay's journal shape is the EA/PAYE defect the practice just left ([04-connectors-and-benchmarks.md](04-connectors-and-benchmarks.md)). Staffology is the one to evaluate if ever forced |

Journal contract, as built: narration `Payroll - <Mon YYYY> (Mn)` unique per client-month; `NoTax` and `TaxType: NONE` on every line; `ShowOnCashBasisReports` explicit; never a system account (AR, AP, bank, VAT, `WAGEPAYABLES`, CIS); duplicate guard = ledger, then `GET /ManualJournals` over the month: exact narration skips, any wages-looking narration HOLDs. [04-connectors-and-benchmarks.md](04-connectors-and-benchmarks.md) says Xero "gives no idempotency key" while [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) shows the header in the spec; both hold: a retry key exists, an external-reference field does not, so cross-run dedupe stays the caller's job.

## Accounting rules the reconciliation enforces

From [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md), as implemented in `journal_builder.py` and `p30.py`; still to build marked (*):

1. Every employee's net pay re-derives from the printed components; every column re-sums to the Total row.
2. PAYE control = tax + EE NIC + ER NIC + student/PG loans (+ Apprenticeship Levy*) - EA (- statutory recovery - SER compensation*) = Total Tax & NIC Due; CIS suffered is added back*, never deducted.
3. EE + ER pensions = Total Other Payments; net + HMRC + pensions = TOTAL NET OUTLAY; journal = 0.00.
4. P30 ties for monthly payers; informational for quarterly payers, whose P30 spans a quarter.
5. EA is an explicit Dr PAYE / Cr ER NIC pair from the month's report, never carried forward; cumulative EA within the annual maximum; the `ea > er_nic` check year-to-date*, since a mid-year claim catches up in one month.
6. Student loans to the PAYE control; attachments/DEA to `attachments_payable` (Xero default 868), never HMRC; payroll giving, vouchers and loan repayments to their own codes; an unmapped element is a HOLD.
7. Pensions at the printed figure (RAS or NPA), one payable line per member; negative pension deductions and negative director NIC accepted as signed lines*.
8. Directors' net pay to the DLA via `net_by_name`; tax-year PAYE codes (`821-YY-YY`) roll at tax month 1*.
9. Tax month from the pay date (6th to 5th), narration `Mn`, payment due the 22nd; journal date per client (`month_end` default, `pay_date` allowed), never mixed within an org.
10. History is never edited: a re-filed DRAFT is replaced, a re-filed POSTED period HOLDs until the delta-journal policy is approved; prior-period differences are flagged, never corrected.

Every rate in note 05 is training-cut-off knowledge, not fetched; 2026/27 figures must be checked on gov.uk before being hard-coded.

## Runtime recommendation (primary + fallback) and why

Primary, as built: a launchd LaunchAgent (`deploy/launchd/uk.co.digivolve.msx.plist`, `StartCalendarInterval` every 30 minutes, plus a daily `recon` at 07:10) on the one Mac that already hosts the Parallels VM and the live Dropbox replica; stdlib Python plus `pdftotext`; Xero token in the login Keychain; SQLite ledger on local disk; dead-man's-switch pings and a Missive run report. Everything the poster needs is local, the token never leaves the machine, and the payroll-agent flow is unchanged. Ideally that Mac is a desk-bound Mac mini set never to sleep, which also enforces the single-writer rule Moneysoft needs ([07-runtime-and-scheduling-options.md](07-runtime-and-scheduling-options.md) findings 1-6, 29). A second Mac runs `role: standby`: shadow-only unless `--take-over` or the primary's heartbeat is over 24 hours old (`runner.standby_guard`), because a rotated Xero token used from two machines invalidates one and SQLite on Dropbox corrupts.

Rejected: the Windows guest (suspended when the Mac sleeps, DPAPI token reverts with a snapshot); Cowork/Desktop scheduled tasks (remote ones cannot see the folder, local ones need the app open); Claude Code's in-session cron (fires only while running, expires after 7 days).

Fallback, to build but keep disabled: a Claude Code Routine or GitHub Actions cron running the same `msx` code against a Dropbox app-folder copy of the PDFs, with its own Xero consent (it cannot borrow the Mac's rotating token), its own ledger, a Custom network allowlist, hourly at :07, defaulting to `draft`. A ~£5/month VPS is the better fallback host if the practice prefers not to route money-moving work through a preview scheduler. Never both live against the same orgs; Xero's GET-by-window is the cross-runtime arbiter.

## Top failure points and the mitigation for each (ranked, 10-15 rows)

| # | Failure | Silent? | Mitigation (built unless marked *) | Source |
|---|---|---|---|---|
| 1 | Wrong, stale or half-synced source file (three Moneysoft folders on one Mac; Brandtek's twin `.pay`; stray report windows in the PDF) | yes | Pinned `pdf_root`; `stable()` gate; conflicted-copy HOLD; employer name on the PDF must match the mapping; source re-stat before the PUT | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) T1-T2, X1, X7-X8 |
| 2 | Wrong mapping: the same code means different things per org (814 = PAYE Payable at Brandtek) | yes | Codes only from the client's own journals (`msx onboard`); `GET /Accounts` class, status, system-account and `expect_names` checks; mapping SHA on every row; `TBC-` = HOLD | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) M3-M6 |
| 3 | Duplicate journal (Brandtek's repeating journal, manual posting, our own retry) | no | Ledger; exact-narration GET over the month; wages-looking narration HOLD; `Idempotency-Key`; `client_posts_own_journal` flag | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) PO1-PO2 |
| 4 | Figures do not reconcile (EPS items, new pay element, parse drift after an update) | no | Every identity to the penny; HOLD names the figures; layout fixtures in tests; explicit codes for EPS items* | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) B1, P9-P10 |
| 5 | Refresh token dies (60 days unused, lost rotation, two machines sharing it) | yes | Keychain; atomic write; keep-alive refresh; `AUTH REQUIRED` warning; `doctor`; one machine holds the token | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) 16 |
| 6 | Run does not happen (Mac asleep, login window, keychain locked, launchd unloaded) | yes | `StartCalendarInterval`; heartbeat file and URL with `/start` and `/fail`; standby watches the primary's heartbeat age; Mac mini | [07-runtime-and-scheduling-options.md](07-runtime-and-scheduling-options.md) 1, 4 |
| 7 | Xero refuses (lock date, archived code, system account, tax type) | no | Pre-flight `Organisation`, `Accounts`, `TaxRates`; validation text surfaced verbatim; never auto-move a date | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) 7-14 |
| 8 | Payroll re-run after posting (late changes, re-sent FPS) | yes | Payload SHA compared every run; HOLD with both totals; delta journal only after approval* | [05-uk-payroll-journal-accounting-rules.md](05-uk-payroll-journal-accounting-rules.md) 10 |
| 9 | Posted into the wrong org (Ltd vs Limited; Awecreative vs Awe Creative) | yes | Pinned `tenant_id` or exact normalised name; zero or two hits = HOLD; tenant name asserted before the PUT | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) M6, PO11 |
| 10 | Crash between the PUT and the ledger write | yes | Write-ahead `posting` row with key and payload SHA; next run adopts the journal Xero already holds | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) REC1 |
| 11 | Connection cap reached or org disconnected (tier caps, two-uncertified-apps rule) | no | Several PKCE apps; `auth login` warns at the cap; HOLD "reconnect"; settle the exemption question first | [03-xero-manual-journals-api-and-auth.md](03-xero-manual-journals-api-and-auth.md) 21-23 |
| 12 | Xero outage mid-run; April rollover; drafts never approved | no / yes | `Retry-After`, jittered backoff, circuit breaker after 3 (remaining clients `pending`); `active_from/to`; `recon` reports no-journal and aging drafts | [06-reliability-and-failure-modes.md](06-reliability-and-failure-modes.md) B.9, T8-T9, PO18 |

## Things only Matt can confirm on the office machine

The full list, with what to record, is [../office-machine-checklist.md](../office-machine-checklist.md). The ten that change the design most:

1. Employer's Summary layout control (Basic/Medium/Additions/Deductions): exact keystrokes, so every export carries all three layouts (A1).
2. `pdftotext -layout` on a real Moneysoft PDF: Browns Apr-2026 must build to `Dr = Cr = 41463.73` with no HOLD (A2).
3. On one SMP client and one CIS-suffered client: is "Total Tax & NIC Due" net of recovery/compensation and of CIS suffered, and what are the printed labels (A4). This decides the parser identity.
4. `Pay > Accounting File`: what it asks and what the CSV contains (A5); whether `Employee Pay Totals > copy to clipboard` gives a per-period tab-delimited table (A6).
5. The five archetypes (salaried, director-only, NEST, student loan or attachment, quarterly payer) each build or produce a specific HOLD (A3).
6. Dropbox on the posting Mac: `PDF attachments` pinned available offline; `~/.config/msx` outside every synced folder; PDFs written in place or temp-and-rename, which sets `settle_seconds` (B1, B3).
7. Keychain readable by the LaunchAgent after a reboot without a prompt; sleep and auto-login settings; laptop or Mac mini (B5).
8. Ask Xero how the practice exemption applies to a bespoke app, whether it lifts the 5/50 caps, and whether Bulk Connections can be enabled (C2). Nothing else in the Xero design is settled until this is.
9. Which staff login has Administrator/Standard access with "edit connected apps" to every payroll client's org; how many payroll clients are on Xero, share an org, or already carry two uncertified apps (C3, C7).
10. On the Demo Company: exact `ValidationErrors` text for an unknown code and a locked date, and whether the same `Idempotency-Key` replayed after 25 hours returns the original journal (C4, C5).

## Open questions and decisions for Matt

- `POSTED` from day one for clean clients, or `DRAFT` for a month first? The market posts on finalise; the code supports either per client.
- Journal date: month end (practice convention) or pay date (market)? Matters for clients paid after month end.
- SER compensation (8.5%): credit to employer NIC cost or other income? Same for the £1 AEO admin charge.
- Corrections to a POSTED journal: delta journal or void and re-post? Until decided the pipeline HOLDs.
- Tax-year PAYE codes (`821-YY-YY`): roll automatically at tax month 1, or HOLD once a year for a human?
- Which login authorises the primary Xero app, who holds the standby Mac, who watches heartbeat alerts out of hours, and is a second login available for the fallback's separate consent?
- Buy a Mac mini as the payroll machine, or keep a laptop with `pmset`/`caffeinate`?
- Heartbeat provider (hosted healthchecks.io or self-hosted), and is the Claude plan Pro/Max (cloud API credentials, Routines) or Team?
- Build the cloud fallback now (needs an app-folder copy of each PDF and a second Xero consent) or after a clean quarter on the primary?
- Email Moneysoft support two questions the web cannot answer: any command-line or silent-print facility, and whether the Accounting File layout is documented.
- Confirm on gov.uk before hard-coding: 2026/27 EA, SER threshold and rate, statutory weekly rates, LEL, student-loan thresholds, AE qualifying-earnings band.
