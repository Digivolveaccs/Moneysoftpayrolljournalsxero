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

## 2. Register the Xero app (once per practice)

1. developer.xero.com > My Apps > New app. Integration type: **Mobile or
   desktop app** (this is the **Auth Code with PKCE** grant - no client secret).
   Redirect URI: `http://localhost:8400/callback`. Company URL: the practice site.
2. Copy the **Client ID** into `~/.config/msx/config.json` (`xero.client_id`).
3. Connection caps: since March 2026 Xero's Starter tier allows **5**
   connected organisations and Core **50**; Xero says bespoke integrations a
   practice builds for its own clients are exempt from the new pricing, but
   the mechanism is not documented (see `docs/research/03`). **Before
   connecting more than five orgs, ask Xero** (developer support / partner
   manager) how the practice exemption is applied to this app and whether it
   lifts the cap. Until answered, register further PKCE apps as needed
   (`xero.apps.<name>`) - each mapping names the app that holds its org.
4. Scopes are requested by `msx`, not configured in the portal:
   `offline_access accounting.manualjournals accounting.manualjournals.read
   accounting.settings.read`.

## 3. Config

```
mkdir -p ~/.config/msx
cp ~/Moneysoftpayrolljournalsxero/config.example.json ~/.config/msx/config.json
```

Edit it: `machine_name`, `pdf_root` (the Dropbox PDF attachments folder on
THIS Mac), `clients_dir` (the repo's `clients/`), `xero.client_id`,
`notify.report_to`, `notify.missive_token_file` (a Missive API token in a
file, `chmod 600`, created in Missive > Settings > API by a user who can send
from the payroll address), and optionally `notify.heartbeat_url` (a
healthchecks.io-style check URL; the run pings `/start`, `/`, `/fail`).

`bin/msx doctor` must print `DOCTOR: ok` except for the Xero auth line.

## 4. Connect the client organisations

```
bin/msx auth login            # opens the browser; sign in as the practice user
```

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

Every 30 minutes: idempotent, silent when nothing is new. The Mac must not
sleep (Energy Saver / `caffeinate` - the payroll-agent already needs this).
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
