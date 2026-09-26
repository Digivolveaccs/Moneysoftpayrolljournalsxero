# Windows-VM side: what the payroll-agent must export for the journal pipeline

The pipeline never touches Moneysoft. It reads the report PDFs the
payroll-agent files into Dropbox after each client's payroll is run and its
FPS accepted. This folder documents the ONE change the guest-side export
needs, and the verification protocol for it.

## The change: export the Employer's Summary in the layouts the parser needs

Today `pa_export` phase 1 opens the Employer's Summary in the **Basic**
layout. Basic omits zero columns and has no pension, student-loan or
attachment columns, so it cannot prove a journal (the backfill skill holds on
the first client with a pension for exactly that reason).

The parser needs:

| Layout | When | Why |
|---|---|---|
| **Medium** | always | every column (basic, hourly, additions, deductions, EE pension, student/postgrad loan, EE NIC, tax, attachments, net, ER NIC, ER pension) plus the Employer Totals block |
| **Additions** | when Total Additions is non-zero | says *which* addition (dividend / holiday / sick / parenting / rounding) so it can be coded |
| **Deductions** | when Total Deductions is non-zero | says *which* deduction (dividend tax / overpayment / payroll giving / childcare / loan / rounding) |

Simplest robust rule: **always open all three** (the layouts are cheap and
the parser merges them; layouts that disagree are a hard Hold). File them as
one PDF per layout, or one combined PDF that `split_reports.py` classifies by
the `Layout:` line on each page:

    <Client> - Employer's Summary for <Mon-YYYY>.pdf                 (Medium)
    <Client> - Employer's Summary (Additions) for <Mon-YYYY>.pdf
    <Client> - Employer's Summary (Deductions) for <Mon-YYYY>.pdf

`msx` discovers all three by name and parses them together.

## How to switch layout in the preview (TO VERIFY ON THE OFFICE MACHINE)

The playbook records that the Employer's Summary opens with `F10, p, Down x3,
Enter` and that "Basic layout persists" - i.e. the layout is a control on the
preview window (or its options dialog) that remembers its last value.
Nothing verified says which keystroke reaches it. First run, with a human
watching:

1. Open the Employer's Summary preview. `pa_enum` the window; `pa_click` with
   a nonsense caption to dump its button/control list. Look for a control
   whose caption is `Layout`, `Basic`, `Medium` or an `Options` button.
2. If it is a combo box: Tab to it, press `Down` until the title line of the
   preview reads `All Employees, Layout: Medium`, and record the exact Tab
   count and key sequence in `ui_map.md`.
3. If it is a dialog before the preview (`Report options`): record the
   accelerator letters. Moneysoft dialogs are keyboard-driven; underlined
   letters are the accelerators.
4. Repeat for Additions and Deductions. Then the export chain becomes:
   open Summary (Medium) -> open Summary (Additions) -> open Summary
   (Deductions) -> payslips -> P30 -> `File > Print All Reports > Save all
   open reports as a PDF file`.
5. Prove it: run `bin/msx build <the three PDFs>` on the Mac. It must print
   the journal summary with no HOLD. Then run it against **the five client
   archetypes** before trusting it: plain salaried; director-only; pension
   (NEST); dividends (Browns); a client with an attachment or student loan.

Until this is verified, `pa_export` can be pointed at Medium only (change
the persisted layout once by hand in the preview): Medium alone is enough for
every client with no additions and no deductions, and the builder holds -
with a message saying which layout to supply - for the rest.

## Alternative source to trial once (research note 01/02)

`Pay > Accounting File` (a CSV "for import into your book-keeping software")
exists in Moneysoft but is undocumented. If a keyboard-driven run of it
produces a per-employee file carrying gross, tax, EE/ER NIC, EA, EE/ER
pension, student loan, attachments and net, it becomes a **second
independent source** for `msx` to cross-check the PDF parse against. Trial
protocol: run it for Browns Garage Apr-2026, save the CSV next to the PDFs,
and send it to the engineer with the PDF; do not build on it until it has
reproduced the Employer's Summary totals to the penny on the archetypes
above.

## What does NOT change

* Filing (FPS), payslip splitting, the client email and the Bright Manager
  tick stay exactly as the payroll-agent does them.
* The payroll-agent's own "Xero" step (steps 9 in its fast path) is
  **replaced** by `msx`: once a client's mapping is in `draft` or `post`
  mode, the agent must not import a CSV or post a journal for that client by
  hand. The ledger and the Xero duplicate guard would refuse the second copy
  anyway, but the agent should read `~/.config/msx/out/HOLDS.md` at the end
  of a batch instead of posting.
