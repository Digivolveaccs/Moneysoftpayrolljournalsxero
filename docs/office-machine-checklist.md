# Office-machine checklist - the things only Matt (or staff at the Mac) can confirm

Everything in this repo was built and tested without access to Moneysoft,
the Windows VM, Dropbox or a live Xero developer account. The pipeline is
designed so that a wrong assumption produces a HOLD, not a wrong journal,
but these checks turn assumptions into facts. Tick them in order; record
the answer next to each (the answers change the config, not the code).

## A. Moneysoft (in the VM, one client file open, e.g. Browns Garage)

| # | Check | How | Record |
|---|---|---|---|
| A1 | The Employer's Summary preview has a layout control (Basic / Medium / Additions / Deductions) and how to reach it by keyboard | Open `Pay > Employer's Summary` (F10, p, Down x3, Enter); `pa_click` with a nonsense caption to dump the controls; Tab through and watch the title line `All Employees, Layout: ...` | exact keystrokes -> `ui_map.md`; then update `pa_export` (vm/README.md) |
| A2 | `pdftotext -layout` on a real Moneysoft PDF parses | Export Apr-2026 (all three layouts) for Browns, copy to the Mac, `bin/msx build <pdfs>` | must print `Dr = Cr = 41463.73` with no HOLD |
| A3 | The five archetypes parse and reconcile | Same for: a plain salaried client; a director-only client; a NEST client; a client with a student loan or attachment; a quarterly PAYE payer | each prints a journal or a *specific* HOLD naming what is missing |
| A4 | Employer Totals block on a client with **SMP recovery** and one with **CIS suffered**: is "Total Tax & NIC Due" net of them, and what are the exact printed labels? | Open those clients' summaries for a month with the item | labels -> `EMPLOYER_TOTALS` in `summary_parser.py` if they differ from the patterns |
| A5 | `Pay > Accounting File`: does it exist, what does it ask, what does the CSV contain? | Run it once for Browns Apr-2026; keep the file | attach to the engineer; decision: second source or drop |
| A6 | Does `Analysis > Employee Pay Totals > copy to clipboard` give a per-period, tab-delimited table with ER NIC and pensions? | Try it; paste into a text file; `bin/msx build file.txt` | works / does not |
| A7 | `pm3.exe "<file>.pay"` and `pm3.exe /?` from PowerShell in the guest | note behaviour with and without an instance running | for the playbook |
| A8 | Which clients post their own wages journal in Xero (Brandtek confirmed) | from CLIENT_NOTES.md | `xero.client_posts_own_journal: true` in their mappings |
| A9 | Which clients pay PAYE quarterly | from CLIENT_NOTES.md / P30 spans | `paye_frequency: "quarterly"` |

## B. Dropbox and the Macs

| # | Check | Record |
|---|---|---|
| B1 | On the posting Mac, `PDF attachments` is pinned **Available offline**; `~/.config/msx` is outside every synced folder | yes/no |
| B2 | Only ONE Mac runs Moneysoft against the Dropbox pay-file folder at a time (licence + corruption); Dropbox team file locking available? | yes/no |
| B3 | Does Moneysoft write the PDF in place or via temp+rename? (Export, watch the folder with `ls -l` every second) | decides `settle_seconds` |
| B4 | `pdftotext` installed (`brew install poppler`), Python 3.9+ present | `bin/msx doctor` |
| B5 | launchd agent can read the login keychain after a reboot without a prompt (if `token_store: keychain`) | test once after a reboot |

## C. Xero (developer.xero.com, signed in as the practice)

| # | Check | Record |
|---|---|---|
| C1 | Register the PKCE app; note the client id | config `xero.client_id` |
| C2 | **Ask Xero** how the practice exemption from the 2026 pricing tiers is applied to a bespoke practice app, whether it lifts the 5/50 connection caps, and whether Bulk Connections can be enabled | the answer decides one app vs several vs certification |
| C3 | Which staff login has Administrator/Standard access (with "edit connected apps") to every payroll client's org via Xero HQ | that login runs `msx auth login` |
| C4 | On the Demo Company: create a DRAFT journal with a deliberately unknown code and one dated before a lock date, via `bin/msx run --client-name ... --mode draft` against a test mapping | paste the exact ValidationErrors text into the engineer's notes |
| C5 | Post the same journal twice within a minute (same Idempotency-Key) and 25 hours apart | confirms the key's replay window |
| C6 | `bin/msx chart <slug>` on three client orgs including one with CIS enabled: confirm every mapped code exists and `No VAT` is TaxType `NONE` | |
| C7 | How many payroll clients are on Xero, how many share an org (groups), how many already have two uncertified apps connected | sets the real connection count |

## D. Decisions for Matt (docs/architecture/pipeline.md section 7)

POSTED vs DRAFT by default; month-end vs pay-date journal date; SER
compensation to employer NIC cost or other income; correction policy for a
posted journal; which Mac is primary and which is standby; who watches the
heartbeat alerts out of hours.
