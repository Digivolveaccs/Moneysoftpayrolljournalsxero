"""Machine configuration - one JSON file per machine, never in git.

Default location ``~/.config/msx/config.json`` (override with ``MSX_CONFIG``).
Every path is resolved here and nowhere else. A missing required key is a
hard failure with an instruction, never a silent default (lesson from the
payroll-agent: a silent default once ran the whole client book against a
five-week-old copy of the data).

Keys::

    {
      "pdf_root": "~/Library/CloudStorage/Dropbox-Personal/Dropbox - Moneysoft Backups/PDF attachments",
      "clients_dir": "~/Moneysoftpayrolljournalsxero/clients",
      "state_db": "~/.config/msx/state.sqlite",
      "out_dir": "~/.config/msx/out",
      "ledger_csv": "<optional Dropbox path for a read-only CSV copy of the ledger>",
      "xero": {"client_id": "...", "redirect_uri": "http://localhost:8400/callback",
               "token_store": "file" | "keychain", "token_file": "~/.config/msx/tokens.json",
               "apps": {"default": {"client_id": "..."}}},
      "default_mode": "shadow",
      "settle_seconds": 30,
      "stability_sample_seconds": 1.0,
      "notify": {"missive_token_file": "~/.config/msx/missive_token",
                 "report_to": "matthew@digivolve.co.uk",
                 "from_address": "payroll@digivolve.co.uk",
                 "from_name": "Digivolve Payroll",
                 "heartbeat_file": "<optional path touched after every run>",
                 "heartbeat_url": "<optional healthchecks.io-style ping URL>"},
      "machine_name": "matt-mac"
    }
"""
import json
import os

from .errors import Hold

DEFAULT_PATH = "~/.config/msx/config.json"
REQUIRED = ("pdf_root", "clients_dir")
DEFAULTS = {
    "state_db": "~/.config/msx/state.sqlite",
    "out_dir": "~/.config/msx/out",
    "default_mode": "shadow",
    "settle_seconds": 30,
    "xero": {},
    "notify": {},
}


class Config:
    def __init__(self, data, path=None):
        self.path = path
        merged = dict(DEFAULTS)
        merged.update(data or {})
        self.data = merged
        missing = [k for k in REQUIRED if not str(self.data.get(k, "")).strip()]
        if missing:
            raise Hold(f"config {path or DEFAULT_PATH} is missing "
                       + ", ".join(missing) + " - see docs/setup.md",
                       stage="config")

    def path_of(self, key, default=None):
        val = self.data.get(key, default)
        return os.path.expanduser(val) if val else val

    @property
    def pdf_root(self):
        return self.path_of("pdf_root")

    @property
    def clients_dir(self):
        return self.path_of("clients_dir")

    @property
    def state_db(self):
        return self.path_of("state_db")

    @property
    def out_dir(self):
        return self.path_of("out_dir")

    @property
    def ledger_csv(self):
        return self.path_of("ledger_csv")

    @property
    def default_mode(self):
        return self.data.get("default_mode", "shadow")

    @property
    def settle_seconds(self):
        return int(self.data.get("settle_seconds", 30))

    @property
    def sample_wait(self):
        return float(self.data.get("stability_sample_seconds", 1.0))

    @property
    def xero(self):
        return self.data.get("xero") or {}

    @property
    def notify(self):
        return self.data.get("notify") or {}

    @property
    def machine_name(self):
        return self.data.get("machine_name")

    def xero_app(self, name="default"):
        """{'client_id', 'client_secret'?, 'redirect_uri', 'token_file'} for
        a named app. Several apps let a practice stay under Xero's
        25-tenant limit per uncertified app."""
        x = self.xero
        apps = x.get("apps") or {}
        if name in apps:
            app = dict(apps[name])
        elif name == "default" and x.get("client_id"):
            app = {"client_id": x["client_id"],
                   "client_secret": x.get("client_secret")}
        else:
            raise Hold(f"config has no Xero app named '{name}' - add "
                       "xero.client_id (or xero.apps.<name>.client_id)",
                       stage="config")
        app.setdefault("redirect_uri", x.get("redirect_uri",
                                             "http://localhost:8400/callback"))
        app.setdefault("token_store", x.get("token_store", "file"))
        app.setdefault("token_file", x.get("token_file",
                                           f"~/.config/msx/tokens-{name}.json"))
        app["name"] = name
        return app


def load(path=None):
    path = os.path.expanduser(path or os.environ.get("MSX_CONFIG")
                              or DEFAULT_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise Hold(f"no config at {path} - copy config.example.json there "
                   "and fill it in (docs/setup.md)", stage="config")
    except json.JSONDecodeError as exc:
        raise Hold(f"{path} is not valid JSON: {exc}", stage="config")
    return Config(data, path)
