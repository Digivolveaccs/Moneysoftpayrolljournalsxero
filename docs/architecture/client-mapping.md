# Client mapping files (`clients/<slug>.json`)

The mapping is the only place a nominal code ever comes from. One file per
client, versioned in git, validated on every load; its SHA-256 fingerprint is
written against every journal the pipeline posts.

## Rules

1. **Never guess a code.** Copy it from the client's own last posted wages
   journal in Xero (`bin/msx onboard <report>` does this for you and leaves
   `TBC-...` placeholders where it could not). A mapping with a placeholder
   builds nothing.
2. **Every employee on the report must be mapped** by the exact name printed
   on the Employer's Summary (spaces are ignored when matching). A new
   starter therefore holds the run once, until a human adds their line.
3. **Mode is a per-client dial**: `shadow` (build + reconcile, nothing sent)
   -> `draft` (DRAFT journal in Xero for a human to approve) -> `post`
   (created POSTED). Promotion is a human edit to this file, in git.
4. **One-off corrections seen in an old journal are not standing rules.**

## Fields

| Field | Meaning |
|---|---|
| `client` | Display name; used in reports |
| `slug` | File name and ledger key: lower-case words joined by hyphens |
| `moneysoft_employer` | Employer name as printed on the report header (tax-year suffix removed). `Ltd`/`Limited`/`The` are ignored when matching |
| `aliases` | Other spellings that should match this client (folder names, old report headers) |
| `xero.org_name` | Exact organisation name in Xero (normalised match against the connected tenants); `xero.tenant_id` pins it (preferred once known) |
| `xero.app` | Which configured Xero app holds this org's connection (`default` unless the practice runs several apps to stay under a connection cap) |
| `mode` | `shadow` / `draft` / `post` |
| `journal_date` | `month_end` (default; last day of the calendar month of the pay period) or `pay_date` (requires a pay date to be supplied). A literal `dd/mm/yyyy` is honoured for one-off re-dating |
| `narration` | Template; must contain `{tag}` (`April 2026 (M1)`), `{month}` or `{period}` so each month is unique. Default `Payroll - {tag}` |
| `tax_rate` | CSV tax rate name, default `No VAT` (API uses TaxType `NONE`) |
| `show_on_cash_basis` | Xero's "show on cash basis reports" flag; default false (matches the CSV importer) |
| `paye_frequency` | `monthly` or `quarterly` - quarterly payers' P30s span three months, so the P30 cross-check is informational for them |
| `codes.wages_payable` | Cr net wages (one named line per employee) |
| `codes.paye_payable` | Cr ONE PAYE/NIC control line (tax + EE NIC + ER NIC + student loans), Dr the Employment Allowance |
| `codes.er_nic_cost` | Dr employer NIC per employee (full, pre-allowance); Cr the Employment Allowance |
| `codes.pensions_payable` | Cr one named line per member (EE + ER, split in the description) |
| `codes.er_pension_cost` | Dr employer pension per employee |
| `codes.dividend_code` | Dr dividends per employee (can be overridden per employee) |
| `codes.attachments_payable` | Cr attachments of earnings per employee (court / DWP creditor). Required whenever the report shows attachments |
| `codes.<deduction>_code` | Cr for `overpayment`, `payroll_giving`, `childcare`, `loans_repayment`, `rounding_deduction` when they occur |
| `addition_codes` | Where an addition should NOT follow the employee's own pay code, e.g. `{"sick": "...", "holiday": "..."}` (also per employee) |
| `employees.<Name>.pay_code` | Dr gross pay for that person (477 salaries / 478 directors / 6000 productive labour / 835 DLA ...) |
| `employees.<Name>.dividend_code` | Per-person dividend override |
| `employees.<Name>.dividend_tax_code` | Cr dividend tax deducted from net pay, to that person's loan account |
| `source`, `approved_by`, `approved_on`, `notes` | Provenance. Say which posted journal the codes came from |

## Conventions seen in the practice's client book

| Client type | Gross | ER NIC | PAYE | Net |
|---|---|---|---|---|
| Standard Xero UK chart | 477 (478 directors) | 479 | 825 | 814 |
| Director-only, salary to loan account | 478 | 479 | 825 | 835 (DLA) |
| Browns Garage | 230 / 381 / 6000 by person | 6002 | 2210 | 2200 |
| BMBD | 7000 | 7020 | 2210 | each director's own loan account |
| Brandtek | - | - | 814 is *PAYE* payable there | not 814 |

The last row is why blind defaults are forbidden.
