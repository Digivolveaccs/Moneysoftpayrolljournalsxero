"""Xero Accounting API client - stdlib only (urllib), PKCE auth, token store.

Designed for an unattended script on a staff Mac:

* **Auth**: OAuth 2.0 authorization-code flow with PKCE (public client, no
  client secret to leak). ``login()`` opens the browser once; after that the
  rotating refresh token keeps the connection alive as long as the script
  runs at least once every 60 days. Custom Connections (client_credentials,
  one org each) are supported as a second auth mode for single-org setups.
* **Token store**: JSON file with 0600 permissions, written atomically
  (temp file + os.replace) so a crash mid-refresh cannot lose the only copy
  of a rotated refresh token. macOS Keychain storage is available via
  ``KeychainTokenStore``.
* **Rate limits**: 429 honoured via Retry-After; 5xx retried with jittered
  backoff; everything else raised as ``XeroError`` with the ValidationErrors
  text Xero returns.
* **Duplicate guard**: ``find_manual_journals`` lists journals in a date
  window so the caller can refuse to post when one already exists.

Endpoints used: /connections, /api.xro/2.0/Organisation, /Accounts,
/TaxRates, /ManualJournals. Nothing else.
"""
import base64
import hashlib
import http.server
import json
import os
import random
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

# Granular scopes (required for apps created after Xero's scope split);
# an app created earlier may use the broad "accounting.transactions" instead
# - override with config xero.scopes.
DEFAULT_SCOPES = ("offline_access", "accounting.manualjournals",
                  "accounting.manualjournals.read", "accounting.settings.read")

USER_AGENT = "digivolve-msx/0.1 (moneysoft-to-xero payroll journals)"


class XeroError(Exception):
    def __init__(self, message, *, status=None, body=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable

    def __str__(self):
        return self.args[0]


class AuthRequired(XeroError):
    """No usable token: a human must run ``msx auth login`` once."""


# ------------------------------------------------------------------ tokens

class FileTokenStore:
    """JSON token file, 0600, atomic writes."""

    def __init__(self, path):
        self.path = os.path.expanduser(path)

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise AuthRequired(f"token file {self.path} is corrupt: {exc}")

    def save(self, tokens):
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tokens-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(tokens, fh, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def clear(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class KeychainTokenStore:
    """macOS Keychain via the ``security`` CLI (generic password item)."""

    def __init__(self, service="digivolve-msx-xero", account="tokens"):
        self.service, self.account = service, account

    def load(self):
        r = subprocess.run(["security", "find-generic-password", "-s",
                            self.service, "-a", self.account, "-w"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        try:
            return json.loads(base64.b64decode(r.stdout.strip()).decode())
        except Exception as exc:
            raise AuthRequired(f"keychain token item is corrupt: {exc}")

    def save(self, tokens):
        blob = base64.b64encode(json.dumps(tokens).encode()).decode()
        subprocess.run(["security", "add-generic-password", "-U", "-s",
                        self.service, "-a", self.account, "-w", blob],
                       check=True, capture_output=True)

    def clear(self):
        subprocess.run(["security", "delete-generic-password", "-s",
                        self.service, "-a", self.account],
                       capture_output=True)


# --------------------------------------------------------------- transport

class UrllibTransport:
    def request(self, method, url, headers=None, data=None, timeout=60):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(q.query))
        _CallbackHandler.result = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in params
        self.wfile.write((
            "<html><body style='font-family:sans-serif'><h2>"
            + ("Xero connected - you can close this tab." if ok
               else "Xero authorisation failed: " + params.get("error", "?"))
            + "</h2></body></html>").encode("utf-8"))

    def log_message(self, *a):  # silence
        pass


class XeroClient:
    def __init__(self, *, client_id, token_store, redirect_uri=None,
                 scopes=DEFAULT_SCOPES, client_secret=None, transport=None,
                 sleep=time.sleep, clock=time.time, log=None):
        self.client_id = client_id
        self.client_secret = client_secret        # only for custom connections
        self.token_store = token_store
        self.redirect_uri = redirect_uri or "http://localhost:8400/callback"
        self.scopes = list(scopes)
        self.transport = transport or UrllibTransport()
        self._sleep = sleep
        self._clock = clock
        self._log = log or (lambda msg: None)
        self._tokens = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ auth

    def login(self, *, open_browser=True, timeout=300):
        """Interactive PKCE login. Returns the tenant list."""
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code", "client_id": self.client_id,
            "redirect_uri": self.redirect_uri, "scope": " ".join(self.scopes),
            "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
        u = urllib.parse.urlparse(self.redirect_uri)
        port = u.port or 80
        _CallbackHandler.result = {}
        server = http.server.HTTPServer((u.hostname or "localhost", port),
                                        _CallbackHandler)
        server.timeout = 1
        print("Open this URL in a browser signed in to Xero as a practice "
              "user with access to the client organisations:\n  " + url)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        deadline = self._clock() + timeout
        while self._clock() < deadline and not _CallbackHandler.result:
            server.handle_request()
        server.server_close()
        res = _CallbackHandler.result
        if not res:
            raise AuthRequired("no callback received from Xero within "
                               f"{timeout}s")
        if res.get("state") != state:
            raise AuthRequired("state mismatch on the Xero callback - "
                               "possible CSRF, try again")
        if "code" not in res:
            raise AuthRequired("Xero returned error: "
                               + res.get("error_description",
                                         res.get("error", "?")))
        body = {"grant_type": "authorization_code", "client_id": self.client_id,
                "code": res["code"], "redirect_uri": self.redirect_uri,
                "code_verifier": verifier}
        self._tokens = self._token_request(body)
        self.token_store.save(self._tokens)
        return self.tenants()

    def _token_request(self, body):
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "User-Agent": USER_AGENT}
        if self.client_secret:
            basic = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers["Authorization"] = "Basic " + basic
        status, _, raw = self.transport.request(
            "POST", TOKEN_URL, headers=headers,
            data=urllib.parse.urlencode(body).encode())
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {"raw": raw[:200].decode(errors="replace")}
        if status != 200:
            raise AuthRequired(f"token endpoint returned {status}: "
                               f"{data.get('error_description') or data.get('error') or data}")
        data["obtained_at"] = int(self._clock())
        return data

    def _ensure_token(self):
        with self._lock:
            if self._tokens is None:
                self._tokens = self.token_store.load()
            if self._tokens is None:
                if self.client_secret:      # custom connection: no refresh
                    self._tokens = self._token_request(
                        {"grant_type": "client_credentials",
                         "scope": " ".join(sc for sc in self.scopes
                                           if sc not in ("openid", "profile",
                                                         "email",
                                                         "offline_access"))})
                    self.token_store.save(self._tokens)
                else:
                    raise AuthRequired("no Xero token - run: msx auth login")
            age = self._clock() - self._tokens.get("obtained_at", 0)
            ttl = int(self._tokens.get("expires_in", 1800))
            if age >= ttl - 120:
                self._refresh_locked()
            return self._tokens["access_token"]

    def _refresh_locked(self):
        if self.client_secret:
            self._tokens = self._token_request(
                {"grant_type": "client_credentials",
                 "scope": " ".join(sc for sc in self.scopes
                                   if sc not in ("openid", "profile", "email",
                                                 "offline_access"))})
            self.token_store.save(self._tokens)
            return
        rt = self._tokens.get("refresh_token")
        if not rt:
            raise AuthRequired("token has no refresh_token - run: msx auth login")
        try:
            new = self._token_request({"grant_type": "refresh_token",
                                       "client_id": self.client_id,
                                       "refresh_token": rt})
        except AuthRequired as exc:
            # a lost/used refresh token cannot be recovered without a human
            raise AuthRequired("refresh failed (" + str(exc) + ") - the "
                               "refresh token is invalid or expired (60 days "
                               "unused). Run: msx auth login")
        # WRITE-AHEAD: persist the rotated token before using it
        self.token_store.save(new)
        self._tokens = new

    def token_status(self):
        """{'has_token', 'obtained_at', 'refresh_expires_in_days'} - the
        rotating refresh token dies 60 days after it was last used."""
        tokens = self._tokens or self.token_store.load()
        if not tokens:
            return {"has_token": False}
        obtained = tokens.get("obtained_at", 0)
        days_left = (obtained + 60 * 86400 - self._clock()) / 86400
        return {"has_token": True, "obtained_at": obtained,
                "refresh_expires_in_days": round(days_left, 1),
                "custom_connection": bool(self.client_secret)}

    def force_refresh(self):
        with self._lock:
            if self._tokens is None:
                self._tokens = self.token_store.load()
            if self._tokens is None:
                raise AuthRequired("no Xero token - run: msx auth login")
            self._refresh_locked()
        return True

    # ---------------------------------------------------------- requests

    def _call(self, method, url, *, tenant_id=None, body=None, params=None,
              idempotency_key=None, max_attempts=6):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        attempt = 0
        while True:
            attempt += 1
            token = self._ensure_token()
            headers = {"Authorization": "Bearer " + token,
                       "Accept": "application/json",
                       "User-Agent": USER_AGENT}
            if tenant_id:
                headers["Xero-Tenant-Id"] = tenant_id
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode("utf-8")
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            status, resp_headers, raw = self.transport.request(
                method, url, headers=headers, data=data)
            text = raw.decode("utf-8", errors="replace") if raw else ""
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"raw": text[:500]}
            if 200 <= status < 300:
                return payload
            lower = {k.lower(): v for k, v in (resp_headers or {}).items()}
            if status == 401 and attempt == 1 and not self.client_secret:
                self._log("401 from Xero - refreshing token once")
                self.force_refresh()
                continue
            if status == 429 and attempt < max_attempts:
                wait = float(lower.get("retry-after", 5) or 5)
                self._log(f"429 rate limited ({lower.get('x-rate-limit-problem', '?')}) "
                          f"- waiting {wait}s")
                self._sleep(min(wait, 120) + random.uniform(0, 1))
                continue
            if status >= 500 and attempt < max_attempts:
                wait = min(2 ** attempt, 60) + random.uniform(0, 1)
                self._log(f"{status} from Xero - retry in {wait:.0f}s")
                self._sleep(wait)
                continue
            raise XeroError(self._describe(status, payload, method, url),
                            status=status, body=payload,
                            retryable=status in (429, 500, 502, 503, 504))

    @staticmethod
    def _describe(status, payload, method, url):
        msgs = []
        if isinstance(payload, dict):
            if payload.get("Message"):
                msgs.append(str(payload["Message"]))
            if payload.get("Detail"):
                msgs.append(str(payload["Detail"]))
            for el in payload.get("Elements", []) or []:
                for ve in el.get("ValidationErrors", []) or []:
                    msgs.append(ve.get("Message", ""))
            for ve in payload.get("ValidationErrors", []) or []:
                msgs.append(ve.get("Message", ""))
            if payload.get("Title") and not msgs:
                msgs.append(str(payload["Title"]))
            if payload.get("raw") and not msgs:
                msgs.append(payload["raw"])
        return (f"Xero {method} {url.split('/api.xro/2.0/')[-1]} -> {status}: "
                + ("; ".join(m for m in msgs if m) or "no detail"))

    # ------------------------------------------------------------- reads

    def tenants(self):
        data = self._call("GET", CONNECTIONS_URL)
        return [{"tenant_id": t.get("tenantId"), "name": t.get("tenantName"),
                 "type": t.get("tenantType"), "id": t.get("id")}
                for t in data or []]

    def organisation(self, tenant_id):
        data = self._call("GET", f"{API_BASE}/Organisation", tenant_id=tenant_id)
        orgs = data.get("Organisations") or []
        if not orgs:
            raise XeroError("Organisation endpoint returned nothing")
        o = orgs[0]
        return {"name": o.get("Name"), "legal_name": o.get("LegalName"),
                "short_code": o.get("ShortCode"),
                "period_lock_date": _xero_date(o.get("PeriodLockDate")),
                "end_of_year_lock_date": _xero_date(o.get("EndOfYearLockDate")),
                "financial_year_end_day": o.get("FinancialYearEndDay"),
                "financial_year_end_month": o.get("FinancialYearEndMonth"),
                "base_currency": o.get("BaseCurrency"),
                "organisation_id": o.get("OrganisationID")}

    def accounts(self, tenant_id):
        data = self._call("GET", f"{API_BASE}/Accounts", tenant_id=tenant_id)
        out = {}
        for a in data.get("Accounts") or []:
            code = a.get("Code")
            if code is None:
                continue
            out[str(code)] = {"name": a.get("Name"), "status": a.get("Status"),
                              "type": a.get("Type"), "class": a.get("Class"),
                              "system_account": a.get("SystemAccount"),
                              "account_id": a.get("AccountID")}
        return out

    def tax_rates(self, tenant_id):
        data = self._call("GET", f"{API_BASE}/TaxRates", tenant_id=tenant_id)
        return {(t.get("TaxType") or ""): t.get("Name")
                for t in data.get("TaxRates") or []}

    def find_manual_journals(self, tenant_id, date_from, date_to,
                             include_deleted=False):
        """Journals dated in [date_from, date_to] (ISO dates), summary rows.

        Xero's list endpoint omits JournalLines; call ``manual_journal`` for
        the full document. Filters DELETED/VOIDED unless asked."""
        where = (f"Date>=DateTime({date_from[:4]},{int(date_from[5:7])},"
                 f"{int(date_from[8:10])})&&Date<=DateTime({date_to[:4]},"
                 f"{int(date_to[5:7])},{int(date_to[8:10])})")
        out, page = [], 1
        while True:
            data = self._call("GET", f"{API_BASE}/ManualJournals",
                              tenant_id=tenant_id,
                              params={"where": where, "page": page})
            rows = data.get("ManualJournals") or []
            for j in rows:
                if not include_deleted and j.get("Status") in ("DELETED",
                                                               "VOIDED"):
                    continue
                out.append({"id": j.get("ManualJournalID"),
                            "narration": j.get("Narration") or "",
                            "status": j.get("Status"),
                            "date": _xero_date(j.get("Date")),
                            "url": j.get("Url"),
                            "has_attachments": j.get("HasAttachments")})
            if len(rows) < 100:
                break
            page += 1
        return out

    def manual_journal(self, tenant_id, journal_id):
        data = self._call("GET", f"{API_BASE}/ManualJournals/{journal_id}",
                          tenant_id=tenant_id)
        rows = data.get("ManualJournals") or []
        return rows[0] if rows else None

    # ------------------------------------------------------------ writes

    def create_manual_journal(self, tenant_id, payload, *, idempotency_key=None):
        """PUT one manual journal (create-only, never update). Returns the
        created journal dict."""
        data = self._call("PUT", f"{API_BASE}/ManualJournals",
                          tenant_id=tenant_id, body={"ManualJournals": [payload]},
                          params={"summarizeErrors": "false"},
                          idempotency_key=idempotency_key, max_attempts=1)
        rows = data.get("ManualJournals") or []
        if not rows:
            raise XeroError("Xero accepted the request but returned no "
                            "journal", body=data)
        j = rows[0]
        errs = j.get("ValidationErrors") or []
        if errs or j.get("HasErrors"):
            raise XeroError("Xero rejected the journal: "
                            + "; ".join(e.get("Message", "") for e in errs),
                            status=400, body=data)
        return j

    def set_manual_journal_status(self, tenant_id, journal_id, status):
        data = self._call("POST", f"{API_BASE}/ManualJournals/{journal_id}",
                          tenant_id=tenant_id,
                          body={"ManualJournals": [{"ManualJournalID": journal_id,
                                                    "Status": status}]},
                          params={"summarizeErrors": "false"}, max_attempts=1)
        rows = data.get("ManualJournals") or []
        j = rows[0] if rows else {}
        errs = j.get("ValidationErrors") or []
        if errs:
            raise XeroError("Xero refused the status change: "
                            + "; ".join(e.get("Message", "") for e in errs),
                            status=400, body=data)
        return j


def _xero_date(value):
    """'/Date(1745884800000+0000)/' or ISO -> 'YYYY-MM-DD' (or None)."""
    if not value:
        return None
    s = str(value)
    if s.startswith("/Date("):
        try:
            ms = int(s[6:].split("+")[0].split("-")[0].rstrip(")/"))
            return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))
        except ValueError:
            return None
    return s[:10]


def free_port_or(default=8400):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", default))
            return default
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
