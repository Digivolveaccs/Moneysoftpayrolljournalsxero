#!/bin/bash
# One-command install of msx on the payroll Mac.
#
#   curl -fsSL https://raw.githubusercontent.com/Digivolveaccs/Moneysoftpayrolljournalsxero/claude/gallant-ritchie-czpla6/deploy/install.sh | bash -s -- <PAYROLL_AGENT_CLIENT_APP_ID> <EASY_AUTH_APP_ID>
#   (or, from a clone:  bash deploy/install.sh <PAYROLL_AGENT_CLIENT_APP_ID> <EASY_AUTH_APP_ID>)
#
# Xero is reached through the practice's own app on Azure (Digivolve Practice
# API, Function App digivolve-xero): the two ids are the Entra "Payroll Agent
# Client" app registration and the Function App's Easy Auth app (see
# docs/setup.md section 2). Pass "--direct <XERO_PKCE_CLIENT_ID>" instead to
# use a PKCE app on this Mac.
#
# What it does (idempotent, safe to re-run):
#   1. clones/updates the repo into ~/Moneysoftpayrolljournalsxero
#   2. installs poppler (pdftotext) via Homebrew if missing
#   3. writes ~/.config/msx/config.json from config.example.json if absent,
#      pointing pdf_root at the Dropbox "PDF attachments" folder it finds
#   4. installs and loads the two launchd jobs (run every 30 min, recon daily)
#   5. runs `msx doctor`
# It never touches Xero, never stores a token, never posts anything: the
# first `msx auth login` and the client mappings are yours to do afterwards.
set -euo pipefail

BACKEND=engine
if [ "${1:-}" = "--direct" ]; then BACKEND=direct; shift; fi
ARG1="${1:-}"; ARG2="${2:-}"
REPO_URL="https://github.com/Digivolveaccs/Moneysoftpayrolljournalsxero.git"
BRANCH="claude/gallant-ritchie-czpla6"
DEST="$HOME/Moneysoftpayrolljournalsxero"
CFG_DIR="$HOME/.config/msx"

say() { printf '\n==> %s\n' "$*"; }

say "repo"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch -q origin "$BRANCH" && git -C "$DEST" checkout -q "$BRANCH" && git -C "$DEST" pull -q --ff-only origin "$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO_URL" "$DEST"
fi
chmod +x "$DEST/bin/msx"

say "pdftotext"
if ! command -v pdftotext >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then brew install poppler; else
    echo "Homebrew not found - install it (https://brew.sh) then: brew install poppler"; fi
fi

say "config"
mkdir -p "$CFG_DIR/out"
if [ ! -f "$CFG_DIR/config.json" ]; then
  PDF_ROOT=$(ls -d "$HOME"/Library/CloudStorage/Dropbox*/"Dropbox - Moneysoft Backups/PDF attachments" 2>/dev/null | head -1 || true)
  [ -z "$PDF_ROOT" ] && PDF_ROOT="$HOME/Library/CloudStorage/Dropbox-Personal/Dropbox - Moneysoft Backups/PDF attachments"
  python3 - "$DEST/config.example.json" "$CFG_DIR/config.json" "$PDF_ROOT" "$BACKEND" "$ARG1" "$ARG2" "$DEST" <<'PY'
import json, sys, socket
src, dst, pdf_root, backend, arg1, arg2, dest = sys.argv[1:]
cfg = json.load(open(src))
cfg["machine_name"] = socket.gethostname().split(".")[0]
cfg["role"] = "primary"
cfg["pdf_root"] = pdf_root
cfg["clients_dir"] = dest + "/clients"
x = cfg["xero"]
x["backend"] = backend
x.pop("apps", None); x.pop("_direct_comment", None)
if backend == "engine":
    x["engine"].pop("_comment", None)
    x["engine"]["credential_store"] = "keychain"
    if arg1: x["engine"]["entra"]["client_id"] = arg1
    if arg2: x["engine"]["entra"]["scope"] = "api://" + arg2 + "/.default"
    for k in ("client_id", "redirect_uri", "token_store", "scopes"):
        x.pop(k, None)
else:
    x.pop("engine", None)
    x["client_id"] = arg1 or "PUT-THE-PKCE-APP-CLIENT-ID-HERE"
    x["token_store"] = "keychain"
json.dump(cfg, open(dst, "w"), indent=2)
print("wrote", dst)
PY
  chmod 600 "$CFG_DIR/config.json"
else
  echo "keeping existing $CFG_DIR/config.json"
fi

say "launchd"
for job in uk.co.digivolve.msx uk.co.digivolve.msx-recon; do
  PL="$HOME/Library/LaunchAgents/$job.plist"
  sed "s#__REPO__#$DEST#g; s#/Users/REPLACE_ME#$HOME#g" "$DEST/deploy/launchd/$job.plist" > "$PL"
  launchctl bootout "gui/$(id -u)/$job" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PL"
  echo "loaded $job"
done

say "doctor"
"$DEST/bin/msx" doctor || true

cat <<EOF

Next, in this order:
  1. Edit $CFG_DIR/config.json if the pdf_root or the Xero ids are wrong.
  2. $DEST/bin/msx auth login          # engine: paste the function key + client secret; direct: browser sign-in
  3. $DEST/bin/msx onboard "<path to the client's latest Employer's Summary PDF>"
  4. Confirm the TBC- codes in clients/<slug>.json, set mode to "draft", commit.
  5. $DEST/bin/msx run                 # or wait: launchd runs it at :05 and :35
EOF
