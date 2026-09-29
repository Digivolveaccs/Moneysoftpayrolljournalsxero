# Engine pass-through: the change the Azure app needs

`msx` reaches Xero through the practice's own app (repo
`Digivolveaccs/digivolve-xero-api`, Function App `digivolve-xero`). That app
needs one new route, `/api/msx/xero/{path}`, which forwards an allow-listed
set of Xero calls under the app's token. This folder is the hand-off copy of
that change (it could not be pushed from the session that wrote it):

| File here | Goes to | What |
|---|---|---|
| `shared__msx_proxy.py` | `shared/msx_proxy.py` | the pure allow-list decision (new) |
| `tests__test_msx_proxy.py` | `tests/test_msx_proxy.py` | 29 tests for it (new) |
| `route-and-forward.patch` | `shared/xero.py`, `function_app.py` | `XeroClient.forward` (verbatim answer) and the route (unified diff against commit fbccd29) |

Apply on a branch of that repo, run `pytest` (304 pass on fbccd29 with this
applied), merge to `main`; the GitHub Action deploys it. Then the three
portal steps in that repo's `SETUP-MATT.md` section 9 (Payroll Agent Client
secret, Easy Auth app id, function key) and `msx auth login` on the Mac.

What the route allows, and nothing else: GET connections / Organisation /
Accounts / TaxRates / ManualJournals(+id); PUT one balanced DRAFT or POSTED
ManualJournal by AccountCode; POST ManualJournals/{id} with exactly
`{ManualJournalID, Status: "POSTED"}`. The tenant must be a `connected` org
in the app's registry. Two locks: the Easy Auth principal must be the Payroll
Agent Client app, and the function key must match.
