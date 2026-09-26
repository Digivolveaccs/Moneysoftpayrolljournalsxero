# The pipeline - design, failure points, and what removes each one

Written for Matt and for whoever maintains this. The research behind every
decision is in `docs/research/` (start with `00-research-summary.md`).

## 1. What the research settled

| Question | Answer | Where |
|---|---|---|
| Can Moneysoft push journals anywhere? | No. No API, no Xero link, no scripting. Its only outputs are PDF/print/email, an undocumented `Pay > Accounting File` CSV and a Money Manager post. | research/01, 02 |
| What is the trustworthy figures source? | Moneysoft's own recommended HMRC-liability report, the **Employer's Summary for tax period** (Medium + Additions + Deductions layouts), cross-checked against the P30. The practice already parses and reconciles it to the penny. | research/01, 05 |
| How should journals get into Xero? | The **Accounting API** `ManualJournals` endpoint (one journal per call, `Idempotency-Key`, created DRAFT or POSTED), replacing Chrome-driven CSV import. Every serious payroll product does this. | research/03, 04 |
| What is the biggest external risk? | Xero's **connection caps** (since March 2026: Starter 5, Core 50, Plus 1,000 with certification). Xero says practice-built bespoke integrations are exempt but the mechanism is undocumented. Must be confirmed with Xero; the design supports several apps meanwhile. | research/03 |
| What must the reconciliation enforce? | PAYE control = tax + EE NIC + ER NIC + student loans + levy − EA − statutory recovery − SER compensation (− CIS suffered add-back) = Total Tax & NIC Due; pensions EE+ER = Total Other Payments; net + HMRC + pensions = TOTAL NET OUTLAY; each employee re-derives; journal = 0.00. | research/05 |
| What are the known failure modes? | ~70, catalogued with mitigations; the two silent-wrong-money classes are *reading the wrong source* and *posting with the wrong mapping*. | research/06 |
| Where should it run? | On the one Mac that already has Dropbox and the payroll-agent, from launchd every 30 minutes; a second Mac as a shadow-only standby. Cloud runners lack the files; the VM lacks the token safety. | research/07 |

## 2. The shape

```
                 payroll-agent (existing, Windows VM)
                 runs payroll, files FPS, exports reports
                                 |
                                 v
   Dropbox: PDF attachments/<Client YYYY-YY>/<Client> - Employer's Summary for <Mon-YYYY>.pdf
                                 |                       (+ Additions / Deductions / P30)
                                 v
        msx run  (launchd, every 30 min, the posting Mac, idempotent)
        ---------------------------------------------------------------
        1 discover      every Employer's Summary; conflicted copies -> HOLD
        2 stable        non-empty, size+mtime unchanged, >30 s old -> else pending
        3 mapping       exactly one clients/<slug>.json matches the header -> else HOLD
        4 parse         columns proven against the report's own Total row
        5 build         house-shape journal; every identity to the penny -> else HOLD
        6 P30           independent report ties (monthly) / informational (quarterly)
        7 YTD EA        cumulative allowance <= annual maximum
        8 artefacts     journal.csv + journal.json per client-month (audit copy)
        9 re-run?       same period already in Xero with different figures -> HOLD
       10 mode          shadow: stop here, record.   draft/post: continue
       11 tenant        pinned tenant_id, or exact normalised org-name match
       12 lock dates    journal date after PeriodLockDate / EndOfYearLockDate
       13 chart         every code exists, ACTIVE, not a system account, right class,
                        name does not contradict its use (814 = "PAYE Payable" trap)
       14 duplicate     GET journals in the month: exact narration -> skip/adopt;
                        other wages-looking journal -> HOLD
       15 write-ahead   ledger row 'posting' with payload SHA + idempotency key
       16 PUT           one journal, Idempotency-Key, DRAFT or POSTED
       17 read back     debits, line count, date, status, warnings must match
       18 record        ledger row draft/posted with ManualJournalID, mapping SHA,
                        source SHA, EA, ER NIC
        ---------------------------------------------------------------
        HOLDS.md  |  last_run_report.html (emailed via Missive)  |  heartbeat
```

Separately, read-only: `msx recon` (duplicates, missing, mismatch, orphans,
aging drafts, no-journal for active clients) and `msx status`.

## 3. Why each piece exists

**Trigger = the filed report, not Moneysoft state.** Moneysoft leaves no
receipt on disk when an FPS is accepted; the `.pay` mtime changes for many
reasons. The payroll-agent only files the Employer's Summary after its own
verification gate and the FPS result, so the PDF's existence is the cleanest
"this month is done" signal, and it needs no VM access on the posting side.

**Stable-file gate.** Dropbox writes in place; a half-synced or online-only
file must never be parsed. Pending, not held: it resolves itself.

**One mapping per client, in git, fingerprinted.** Charts vary wildly across
the book and the same code means different things in different orgs. The
mapping is the only source of codes; its SHA is on every ledger row; changes
are commits a human made. `msx onboard` builds the proposal from the
client's own posted journals so nobody types a code from memory.

**Penny-perfect gates before any write.** The parser re-sums every column
against the Total row; the builder re-derives every employee's net pay and
every employer-totals identity. Anything else is a HOLD naming the figures.
Never a corrected number.

**The P30 as a second report.** Same payroll, different report, different
code path in Moneysoft. If both say the same PAYE figure, the parse is right.

**Chart checks that read names, not just codes.** Xero's `Accounts` endpoint
gives name, class, status and system-account flag, so the pipeline refuses a
"payable" that is really an expense account, a "wages payable" whose name
says PAYE, an archived code, or a system account - all before the write.

**Three-layer idempotency.** Xero has no external reference field. The
narration `Payroll - <Month YYYY> (Mn)` is unique per client-month and is
checked by a GET over the month before every write; the ledger remembers
what was posted; `Idempotency-Key` makes an in-run retry safe. A crash
between the PUT and the ledger write leaves a `posting` row that the next
run resolves by adopting the journal Xero already holds.

**Read back after writing.** A journal Xero accepted but altered (a dropped
line, a warning) is a human-level HOLD, never auto-voided.

**Modes per client.** `shadow` -> `draft` -> `post`, promoted by a human
commit after clean months. Any hold stops posting for that client-month; the
mode never changes on its own.

**Never correct history.** A re-run after posting, a prior-period
difference, an orphan journal: reported with both figures, decided by a
human. This is the practice's standing rule and the market's.

**Silence is an alert.** Heartbeat `/start`, `/`, `/fail` on every run; a
dead-man's switch turns "the run did not happen" into a notification. The
run report is emailed only when something was posted or held (or always,
during rollout).

**Single posting machine.** SQLite cannot arbitrate across Dropbox replicas
and a rotated Xero refresh token used from two machines invalidates one. The
standby only posts on `--take-over` or when the primary's heartbeat is a day
old; Xero's duplicate check remains the arbiter of last resort.

## 4. Failure points, ranked, and the mitigation

| # | Failure | Silent? | Mitigation in the code |
|---|---|---|---|
| 1 | Wrong / stale / half-synced source file | yes | pinned `pdf_root`; `stable()`; conflicted-copy HOLD; header-vs-mapping assertion; source re-stat before the PUT |
| 2 | Wrong mapping (code means something else in this org) | yes | codes only from the client's own journals; class + name checks against the live chart; `expect_names`; mapping SHA on every row |
| 3 | Duplicate journal (repeating journal, manual posting, our own retry) | no | ledger; exact-narration GET; wages-looking HOLD; Idempotency-Key; adopt on crash |
| 4 | Figures do not reconcile (EPS items, new pay element, parse drift) | no | every identity to the penny; specific HOLD text; EPS items need explicit codes |
| 5 | Xero refuses (lock date, archived code, system account, tax type) | no | pre-flight Organisation/Accounts; validation text surfaced verbatim |
| 6 | Token dies (60-day unused expiry, lost rotation) | yes | weekly keep-alive; atomic token write; Keychain option; `AUTH REQUIRED` in warnings; `doctor` |
| 7 | Run does not happen (Mac asleep, launchd unloaded) | yes | heartbeat file + URL; standby watches the primary's heartbeat age |
| 8 | Xero outage mid-run | no | retry with backoff; circuit breaker after 3 -> remaining `pending`, same key next run |
| 9 | Payroll re-run after posting | yes | payload SHA compared on every run; HOLD with both totals |
| 10 | Connection cap reached | no | several PKCE apps (`xero.apps`), per-mapping `xero.app`; `auth login` warns at the cap |
| 11 | New starter / leaver / pension join | no | HOLD once for a starter; line counts are data-driven |
| 12 | Two machines both posting | yes | `role: standby` shadow-only; take-over rule; Xero GET arbiter |
| 13 | Report layout missing (Additions/Deductions) | no | builder HOLDs naming the layout; vm/README specifies the export |
| 14 | Quarterly payer's P30 spans a quarter | no | P30 informational; PAYE proven from the Summary |
| 15 | April rollover / client left / joined | yes | `active_from/to`; `recon` `no-journal` finding per active client |

## 5. What is deliberately NOT automated

* Choosing a nominal code (`TBC-` stays until a human replaces it).
* Promoting a client's mode.
* Voiding or correcting anything already in Xero.
* Deciding which of two conflicted files is real.
* Anything the payroll-agent does before the report is filed.

## 6. Rollout

1. Office-machine checks (`docs/research/00-research-summary.md`, last
   section): export layouts, Employer Totals treatment of EPS items, Xero
   tier/exemption, `pdftotext` on real PDFs.
2. Install on the posting Mac (`docs/setup.md`); `doctor` clean.
3. Onboard the five archetype clients (salaried; director-only; NEST
   pension; dividends - Browns; one with a student loan or attachment) in
   shadow; two clean months each.
4. Promote to `draft`; approve in Xero; compare against what a human would
   have posted; promote to `post`.
5. Onboard the rest of the book in batches of 20 (each needs one Xero
   consent flow); recon weekly during rollout, monthly after.

## 7. Open decisions for Matt

* `POSTED` from day one for well-behaved clients, or `DRAFT` for a month?
* Journal date: month end (practice convention) or pay date (market)?
* SER compensation credited to employer NIC cost or to other income?
* Corrections to a POSTED journal: delta journal or void and re-post?
* Which staff login connects the client orgs, and who holds the standby.
