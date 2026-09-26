# Changes to the existing payroll-agent skill

`msx` takes over exactly one of the payroll-agent's steps - "9. Xero" - and
needs one change to its export. Everything else in that skill stays as it is.
These edits are to the skill files on the staff Macs (`payroll-agent/SKILL.md`
and the guest scripts under `~/.config/payroll-agent`), not to this repo.

## A. Export the Employer's Summary in the parser's layouts

`pa_export` phase 1 currently opens the Employer's Summary in the **Basic**
layout. Change it to open **Medium, Additions and Deductions** (see
`vm/README.md` for the verification protocol of the layout control), and have
`split_reports.py` name each layout's page:

```
<Client> - Employer's Summary for <Mon-YYYY>.pdf                 (Medium)
<Client> - Employer's Summary (Additions) for <Mon-YYYY>.pdf
<Client> - Employer's Summary (Deductions) for <Mon-YYYY>.pdf
```

`split_reports.py` already classifies pages by content; add a branch on the
`Layout: <name>` line. Keep filing the P30 and payslips exactly as now.

## B. Replace step 9 ("Xero - always check their history FIRST")

Old: switch org in Chrome, inspect posted journals, import CSV, post.

New, for any client whose `clients/<slug>.json` in this repo is in `draft`
or `post` mode:

> 9. **Xero** - nothing to do. `msx` picks the filed Employer's Summary up
>    within 30 minutes, reconciles it and posts (or drafts) the journal. Do
>    not import a CSV or post by hand for this client. If you need the
>    journal now: `~/Moneysoftpayrolljournalsxero/bin/msx run --client-name
>    "<Client as filed>" --period <Mon-YYYY>`, then read the printed outcome.
>    Anything held is in `~/.config/msx/out/HOLDS.md` with the next action.

For clients with no mapping yet, or in `shadow`: the old step 9 still applies
for this month, and the run report will show what `msx` would have posted so
the two can be compared. Run `bin/msx onboard <the Employer's Summary PDFs>`
to create the mapping.

## C. Registry fields that move

The payroll-agent registry's `xero` block (`org_name`, `mapping`,
`gross_by_name`, `net_by_name`) is superseded by `clients/<slug>.json` in
this repo. Do not maintain both. `net_by_name` becomes a per-employee
`wages_payable` override; `gross_by_name` becomes `pay_code`. `msx onboard`
proposes them from the client's posted journals.

## D. CLIENT_NOTES.md items that become mapping fields

| Note today | Mapping field |
|---|---|
| "Brandtek posts theirs manually every month" | `xero.client_posts_own_journal: true` |
| "director's remuneration to 835 DLA" | `employees.<Name>.pay_code: "478"` and `wages_payable` override to `835` |
| "quarterly PAYE payer" | `paye_frequency: "quarterly"` |
| "EA true/false" | nothing - the report says whether EA was applied; the mapping never asserts it |
| "in the March gap; backfill needed" | backfill months stay with the xero-payroll-backfill skill; `msx run --since Apr-2026` ignores anything older |

## E. What the agent should read at the end of a batch

`~/.config/msx/out/HOLDS.md` and `last_run_summary.json`. Carry any hold
into the run report's Held section verbatim (it already has the exact next
action). Never resolve a hold by posting manually.
