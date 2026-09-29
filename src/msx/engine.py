"""Xero through the practice's own Azure app ("Digivolve Practice API").

The practice already runs a Xero app on Azure Functions (repo
``Digivolveaccs/digivolve-xero-api``, Function App ``digivolve-xero``). It
holds the one Xero token that covers every connected client organisation
(about 480 of them, cap 500, exempt from Xero's app pricing as an internal
app). Nothing on the payroll Mac therefore needs a Xero token, a PKCE app,
a browser login, a refresh-token keep-alive or a connection cap: ``msx``
talks to the Function App's ``/api/msx/xero/...`` pass-through, which
forwards an allow-listed set of Xero Accounting API calls (connections,
Organisation, Accounts, TaxRates, ManualJournals) under that token.

This module is deliberately small. ``EngineClient`` *is* ``XeroClient``:
every read, every gate (lock dates, account classes, duplicate guard,
read-back) and every write in ``poster``/``recon``/``onboard`` runs
unchanged. Only two things differ:

* the URL - ``https://api.xero.com/...`` is rewritten to the Function App;
* the credential - an Entra ID client-credentials token for the "Payroll
  Agent Client" app registration (the Function App's Easy Auth checks it
  and the route checks the app id), plus the route's function key.

Both secrets are entered once by a human (``msx auth login``) and kept in
the macOS Keychain or a 0600 file; the pipeline never prints them.
"""
import getpass
import json
import time
import urllib.parse

from .xero_client import (API_BASE, CONNECTIONS_URL, USER_AGENT, AuthRequired,
                          FileTokenStore, KeychainTokenStore, UrllibTransport,
                          XeroClient, XeroError)

ENTRA_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
PROXY_PATH = "/api/msx/xero/"


class EngineTransport:
    """Rewrites Xero URLs to the Function App and swaps the credentials."""

    def __init__(self, base_url, *, function_key, inner=None):
        self.base_url = base_url.rstrip("/")
        self.function_key = function_key
        self.inner = inner or UrllibTransport()

    def rewrite(self, url):
        if url.startswith(CONNECTIONS_URL):
            rest = "connections" + url[len(CONNECTIONS_URL):]
        elif url.startswith(API_BASE + "/"):
            rest = url[len(API_BASE) + 1:]
        else:
            raise XeroError(f"engine transport refuses a non-Xero URL: {url}")
        return self.base_url + PROXY_PATH + rest

    def request(self, method, url, headers=None, data=None, timeout=120):
        h = dict(headers or {})
        h["x-functions-key"] = self.function_key
        return self.inner.request(method, self.rewrite(url), headers=h,
                                  data=data, timeout=timeout)


class EngineClient(XeroClient):
    """``XeroClient`` whose bearer token is an Entra ID app token."""

    def __init__(self, *, base_url, entra_tenant_id, entra_client_id,
                 entra_scope, credential_store, transport=None,
                 sleep=time.sleep, clock=time.time, log=None):
        self.base_url = base_url.rstrip("/")
        self.entra_tenant_id = entra_tenant_id
        self.entra_client_id = entra_client_id
        self.entra_scope = entra_scope
        self.credential_store = credential_store
        self._inner = transport or UrllibTransport()
        self._creds = None
        self._entra = None
        super().__init__(client_id=entra_client_id, token_store=_NoTokens(),
                         transport=None, sleep=sleep, clock=clock, log=log)
        self.transport = _LazyTransport(self)

    # ---------------------------------------------------------- credentials

    def credentials(self):
        if self._creds is None:
            self._creds = self.credential_store.load()
        if not self._creds or not self._creds.get("function_key") \
                or not self._creds.get("client_secret"):
            raise AuthRequired("no engine credentials stored - run: msx auth "
                               "login (it asks for the Function App key and "
                               "the Payroll Agent Client secret)")
        return self._creds

    def store_credentials(self, *, function_key, client_secret):
        creds = {"function_key": function_key.strip(),
                 "client_secret": client_secret.strip(),
                 "saved_at": int(self._clock())}
        self.credential_store.save(creds)
        self._creds = creds
        self._entra = None

    def login(self, *, open_browser=True, timeout=300, prompt=None):
        """Interactive: a human pastes the two secrets; nothing opens Xero.
        Returns the tenant list the engine can see."""
        prompt = prompt or (lambda label: getpass.getpass(label + ": "))
        print("Engine backend - the Xero login lives in the Digivolve Practice "
              "API on Azure.\nPaste the two secrets (input is hidden):")
        key = prompt("Function App key for the msx route (portal -> Function "
                     "App -> App keys, or the route's function key)")
        secret = prompt("Entra client secret for the 'Payroll Agent Client' "
                        "app registration")
        if not key.strip() or not secret.strip():
            raise AuthRequired("both secrets are required")
        self.store_credentials(function_key=key, client_secret=secret)
        return self.tenants()

    # ------------------------------------------------------------- tokens

    def _ensure_token(self):
        with self._lock:
            tok = self._entra
            if tok and self._clock() < tok["expires_at"] - 120:
                return tok["access_token"]
            self._entra = self._entra_token_locked()
            return self._entra["access_token"]

    def _entra_token_locked(self):
        creds = self.credentials()
        body = {"grant_type": "client_credentials",
                "client_id": self.entra_client_id,
                "client_secret": creds["client_secret"],
                "scope": self.entra_scope}
        url = ENTRA_TOKEN_URL.format(tenant=self.entra_tenant_id)
        status, _, raw = self._inner.request(
            "POST", url,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT},
            data=urllib.parse.urlencode(body).encode())
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {"raw": raw[:200].decode(errors="replace")}
        if status != 200 or not data.get("access_token"):
            raise AuthRequired(
                "Entra token request failed (" + str(status) + "): "
                + str(data.get("error_description") or data.get("error")
                      or data)[:300]
                + " - check xero.engine.entra.* in the config and the "
                "Payroll Agent Client secret (msx auth login)")
        return {"access_token": data["access_token"],
                "expires_at": self._clock() + int(data.get("expires_in", 3600))}

    def force_refresh(self):
        with self._lock:
            self._entra = None
        return True

    def token_status(self):
        try:
            self.credentials()
        except AuthRequired:
            return {"has_token": False, "engine": self.base_url}
        return {"has_token": True, "engine": self.base_url,
                "refresh_expires_in_days": None,
                "custom_connection": False}


class _NoTokens:
    """The Xero token never exists on this machine."""

    def load(self):
        return None

    def save(self, tokens):
        raise AuthRequired("engine backend never stores a Xero token")

    def clear(self):
        pass


class _LazyTransport:
    """Built on first use so the function key is only read when needed."""

    def __init__(self, client):
        self.client = client
        self._real = None

    def request(self, method, url, headers=None, data=None, timeout=120):
        if self._real is None:
            self._real = EngineTransport(
                self.client.base_url,
                function_key=self.client.credentials()["function_key"],
                inner=self.client._inner)
        return self._real.request(method, url, headers=headers, data=data,
                                  timeout=timeout)


def credential_store_for(engine_cfg):
    """Keychain by default on a Mac; a 0600 JSON file otherwise."""
    if (engine_cfg.get("credential_store") or "keychain") == "keychain":
        return KeychainTokenStore(service="digivolve-msx-engine",
                                  account="credentials")
    return FileTokenStore(engine_cfg.get("credential_file")
                          or "~/.config/msx/engine-credentials.json")


def client_from_config(cfg, *, transport=None, log=None):
    """Build an ``EngineClient`` from ``cfg.xero.engine``; Hold on gaps."""
    from .errors import Hold
    e = cfg.xero.get("engine") or {}
    entra = e.get("entra") or {}
    missing = [k for k, v in (("xero.engine.base_url", e.get("base_url")),
                              ("xero.engine.entra.tenant_id", entra.get("tenant_id")),
                              ("xero.engine.entra.client_id", entra.get("client_id")),
                              ("xero.engine.entra.scope", entra.get("scope")))
               if not str(v or "").strip() or "PUT-" in str(v)]
    if missing:
        raise Hold("engine backend is not configured: fill in "
                   + ", ".join(missing) + " in the config", stage="config")
    return EngineClient(base_url=e["base_url"], entra_tenant_id=entra["tenant_id"],
                        entra_client_id=entra["client_id"],
                        entra_scope=entra["scope"],
                        credential_store=credential_store_for(e),
                        transport=transport, log=log)
