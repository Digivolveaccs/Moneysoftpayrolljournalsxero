# Setup - the posting Mac (one-off, ~1 hour plus the Xero connections)

`msx` runs on ONE designated Mac (the "posting machine"). A second Mac may
have it installed as a standby (`"role": "standby"` in its config); it never
posts unless told to. Everything below is done once, with a human present.

## 1. Prerequisites

- macOS with Python 3.9+ (`python3 --version`; Apple's is fine, no packages
  needed).
- `brew install poppler` (gives `pdftotext`, the PDF reader the parser was
  verified against). Optional fallback: `pip3 install --user pypdf`.
- Dropbox signed in, with `Dropbox - Moneysoft Backups/PDF attachments`
  pinned **Available offline** (Finder > right-click the folder). Online-only
  placeholders are not readable.
- The repo: `git clone <this repo> ~/Moneysoftpayrolljournalsxero`.
- `~/.config/msx` must be OUTSIDE every synced folder (it holds the ledger and
  the token file if Keychain is not used).

## 2. Xero access: the practice's own app on Azure (recommended)

The practice already runs a Xero app - **Digivolve Practice API**, Azure
Function App `digivolve-xero`, repo `Digivolveaccs/digivolve-xero-api`. Xero
approved it as an internal-use app (19 Aug 2026: exempt from app pricing,
connection cap 500) and about 480 client organisations are connected to it.
`msx` uses it as its Xero backend (`xero.backend: "engine"`), so this Mac
never holds a Xero token, never logs in to Xero, has no connection cap and
nothing to keep alive. Every gate the pipeline runs (lock dates, account
classes, duplicate guard, read-back, recon, onboarding) runs unchanged
through the app's scoped pass-through (`/api/msx/xero/...`, allow-listed
calls only, two credentials required).

One-off, in the Azure repo: merge the `msx pass-through` change (the files in
`deploy/engine-passthrough/` here, or the branch if it was pushed) to `main`;
it deploys itself. Then, in the portal (5 minutes, `SETUP-MATT.md` section 9
there):

1. **Payroll Agent Client** (Entra app registration
   `3d4ab954-97e1-4ca7-8be5-0d810da7e8bd`, already in the example config):
   create a client secret.
2. **Easy Auth app id** `9d1971d9-8a51-4b76-815d-eb0ce04939c2` (already in the
   example config as `api://.../.default`; verified 29 Sep 2026 that its
   allowed client applications include the Payroll Agent Client). Nothing to do.
3. **Function key**: Function App -> App keys -> copy `default`, or add one
   named `msx`.

The Azure repo's `msx setup info` workflow (Actions -> run) re-reads these
ids and checks the deployed route answers 401 unauthenticated.

On the Mac, `msx auth login` asks for the function key and the client secret
(hidden input, Keychain) and lists the organisations the app can see. A
client whose organisation is not yet connected is connected at
`https://digivolve-xero.azurewebsites.net/api/connect` (practice login).

### Alternative: a PKCE app on this Mac (`xero.backend: "direct"`)

Only if the practice app is unavailable. developer.xero.com > New app,
**Mobile or desktop app** (Auth Code with PKCE, no secret), redirect URI
`http://localhost:8400/callback`; `xero.client_id` in the config;
`msx auth login` opens the browser. Connection caps apply (Starter 5, Core
50, exemption by request to api@xero.com); further apps go under
`xero.apps.<name>`. Scopes are requested by `msx`: `offline_access
accounting.manualjournals accounting.manualjournals.read
accounting.settings.read`.

## 3. Config

```
mkdir -p ~/.config/msx
cp ~/Moneysoftpayrolljournalsxero/config.example.json ~/.config/msx/config.json
```

Edit it: `machine_name`, `role` (`primary` on this one machine, `standby`
on any other), `pdf_root` (the Dropbox PDF attachments folder on THIS Mac),
`clients_dir` (the repo's `clients/`), the `xero.engine.entra` ids from
section 2 (or `xero.client_id` for a direct app),
`notify.report_to`, `notify.missive_token_file` (a Missive API token in a
file, `chmod 600`, created in Missive > Settings > API by a user who can send
from the payroll address), and optionally `notify.heartbeat_url` (a
healthchecks.io-style check URL; the run pings `/start`, `/`, `/fail`).

`bin/msx doctor` must print `DOCTOR: ok` except for the Xero auth line.

## 4. Connect the client organisations

```
bin/msx auth login            # engine: paste the two secrets; direct: opens the browser
```

With the engine backend the organisations are already connected to the
practice app (about 480 of them); `msx auth tenants` lists them. Connect a
missing one at `https://digivolve-xero.azurewebsites.net/api/connect`.

The consent screen lists the organisations that login can access; pick one
per flow (Xero only offers multi-select to certified apps). Repeat
`auth login` per client org - each run adds one connection to the same
token. `bin/msx auth tenants` shows what is connected. Practice staff need
Administrator or Standard access to the client org with the "edit connected
apps" permission.

The refresh token rotates on every use and dies after 60 days unused; `msx
run` refreshes it every week on its own. If it ever dies, `auth login` again -
that is the only recovery.

## 5. Map the clients

For each payroll client on Xero:

```
bin/msx onboard "<pdf_root>/<Client YYYY-YY>/<Client> - Employer's Summary for <Mon-YYYY>.pdf"
```

It writes `clients/<slug>.json` in **shadow** mode with the codes it could
prove from the client's own posted wages journals and `TBC-` where it could
not. Matt confirms every `TBC-`, sets `approved_by/on`, and the file is
committed. `docs/architecture/client-mapping.md` explains every field.
Clients that post their own wages journal (Brandtek) get
`"xero": {"client_posts_own_journal": true}`.

## 6. Run it by hand first

```
bin/msx run --dry-run          # discover + build + reconcile, no writes at all
bin/msx run                    # shadow clients: ledger only; draft/post clients: Xero
bin/msx status
cat ~/.config/msx/out/HOLDS.md
```

## 7. Schedule it

```
cp deploy/launchd/uk.co.digivolve.msx.plist ~/Library/LaunchAgents/
sed -i '' "s#__REPO__#$HOME/Moneysoftpayrolljournalsxero#g; s#/Users/REPLACE_ME#$HOME#g" ~/Library/LaunchAgents/uk.co.digivolve.msx.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/uk.co.digivolve.msx.plist
launchctl kickstart -k gui/$(id -u)/uk.co.digivolve.msx
tail -f ~/.config/msx/launchd.log
```

At :05 and :35 every hour (calendar triggers, so firings missed while
asleep run at wake): idempotent, silent when nothing is new. Best on an
always-on Mac (Energy Saver never sleep; the payroll-agent already needs
the Mac awake for the VM). Another run cannot start while one is in
progress (lock file next to the ledger).
If `token_store` is `keychain`, unlock the login keychain once after a reboot
(launchd agents run in the user session, so the login keychain is available
once the user has logged in).

## 8. Promote clients

Shadow for two clean months (run report shows the client under Shadow with
`p30: ties`) -> `"mode": "draft"` -> a clean month with the draft approved in
Xero unchanged -> `"mode": "post"`. Each promotion is a commit to the mapping
file with `approved_by/on`. Any hold demotes the client in practice: nothing
is posted while it is held.

## 9. The second Mac (optional standby)

Same install; config `"role": "standby"` and `"primary_heartbeat_file"`
pointing at a Dropbox-synced copy of the primary's heartbeat (set the
primary's `notify.heartbeat_file` inside Dropbox, e.g. next to `ledger.csv`).
The standby builds and reconciles everything in shadow and only posts when
the primary's heartbeat is older than 24 h or `msx run --take-over` is used.
Its ledger is its own; the Xero duplicate guard is what stops a double post.
