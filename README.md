# Moneysoft -> Xero payroll journals (Digivolve)

Hands-free posting of each client's monthly wages journal from Moneysoft
Payroll Manager into the client's Xero organisation, triggered by the report
the payroll-agent files when the payroll has been run and the FPS accepted.
No input when nothing is wrong; a precise, human-actionable HOLD when
anything is.

```
Moneysoft (Windows VM)  --payroll-agent files PDFs-->  Dropbox "PDF attachments"
                                                             |
                                       msx run (launchd, every 30 min, one Mac)
                                                             |
   discover -> stable? -> parse (Medium/Additions/Deductions) -> prove every figure
   -> build house-shape journal -> P30 cross-check -> mapping + chart checks
   -> duplicate guard -> Xero API (DRAFT or POSTED) -> read back -> ledger
                                                             |
                                        HOLDS.md + run report (Missive) + heartbeat
```

* `docs/architecture/pipeline.md` - the design, the failure points and what
  removes each one.
* `docs/setup.md` - install on the posting Mac, register the Xero app,
  connect the client orgs, map the clients, schedule.
* `docs/architecture/client-mapping.md` - the per-client mapping file.
* `docs/architecture/payroll-json.md` - the parsed report shape and the
  reconciliation identities.
* `docs/research/` - the research dossier behind the design (Moneysoft,
  Xero API and pricing tiers, benchmarks, UK payroll accounting rules,
  failure catalogue, runtime options) with the office-machine checklist.
* `skills/moneysoft-xero-journals/SKILL.md` - the Claude operator skill:
  triage holds, onboard clients, approve drafts, run recon.
* `vm/README.md` - the one change the Windows-side export needs (report layouts).

## Quick start

```
brew install poppler
cp config.example.json ~/.config/msx/config.json    # edit paths + Xero client id
bin/msx doctor
bin/msx auth login                                   # human signs in, picks an org
bin/msx onboard "<Client> - Employer's Summary for Aug-2026.pdf"
bin/msx run --dry-run
bin/msx run
```

## What "hands-free" means here

| Situation | What happens | Human? |
|---|---|---|
| Report filed, everything reconciles, client in `post` mode | POSTED journal created, verified by read-back, recorded | No |
| Client in `draft` mode | DRAFT created for approval in Xero (or `msx approve`) | Approve |
| Client in `shadow` mode (default for new clients) | Built and reconciled, nothing sent, shown in the report | No |
| Nil payroll | Skipped as nil | No |
| Already in Xero (ours, or by narration) | Skipped | No |
| Another wages-looking journal already in that month | HOLD | Yes |
| New starter, new pay element, code missing/archived, locked period, P30 mismatch, EPS item without a code | HOLD with the exact next action | Once |
| Payroll re-run after posting | HOLD with old vs new totals | Yes |
| Xero down | Deferred, retried next run with the same idempotency key | No |
| Run did not happen | Heartbeat silence alerts | Yes |

## Guarantees

* Nothing is written unless every figure re-derives to the penny from the
  report's own totals and the PAYE control equals what HMRC is owed.
* A journal is created at most once per client-month: ledger, exact-narration
  check in Xero, and Xero's `Idempotency-Key`, with crash recovery in between.
* No nominal code is ever guessed; mappings are git-versioned and their
  fingerprint is recorded on every journal.
* Prior periods are never corrected automatically.
* Stdlib-only Python 3.9+; `python3 -m unittest discover -s tests`.
