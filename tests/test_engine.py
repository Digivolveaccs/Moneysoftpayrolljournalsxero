"""The engine backend: Xero through the practice's Azure app."""
import json
import unittest
import urllib.parse

from msx import engine
from msx.errors import Hold
from msx.xero_client import API_BASE, AuthRequired, XeroError


class FakeStore:
    def __init__(self, creds=None):
        self.creds = creds
        self.saved = []

    def load(self):
        return self.creds

    def save(self, creds):
        self.creds = creds
        self.saved.append(creds)


class FakeHttp:
    """Scripted responses keyed by (method, url prefix)."""

    def __init__(self):
        self.calls = []
        self.token_responses = [(200, {}, json.dumps(
            {"access_token": "ENTRA-1", "expires_in": 3600}).encode())]
        self.responses = []

    def request(self, method, url, headers=None, data=None, timeout=60):
        self.calls.append((method, url, dict(headers or {}), data))
        if url.startswith("https://login.microsoftonline.com/"):
            return self.token_responses.pop(0)
        return self.responses.pop(0)


def make(creds=None, clock=None):
    http = FakeHttp()
    t = [1000.0]
    client = engine.EngineClient(
        base_url="https://digivolve-xero.azurewebsites.net/",
        entra_tenant_id="TENANT", entra_client_id="PA-APP",
        entra_scope="api://EASYAUTH/.default",
        credential_store=FakeStore(creds if creds is not None else
                                   {"function_key": "FK", "client_secret": "CS"}),
        transport=http, sleep=lambda s: None,
        clock=clock or (lambda: t[0]))
    return client, http, t


class TransportRewrite(unittest.TestCase):
    def test_connections_and_api_paths_are_rewritten(self):
        tr = engine.EngineTransport("https://f.example/", function_key="K",
                                    inner=FakeHttp())
        self.assertEqual(tr.rewrite("https://api.xero.com/connections"),
                         "https://f.example/api/msx/xero/connections")
        self.assertEqual(tr.rewrite(API_BASE + "/ManualJournals?page=2"),
                         "https://f.example/api/msx/xero/ManualJournals?page=2")

    def test_foreign_urls_are_refused(self):
        tr = engine.EngineTransport("https://f.example", function_key="K",
                                    inner=FakeHttp())
        with self.assertRaises(XeroError):
            tr.rewrite("https://identity.xero.com/connect/token")


class Calls(unittest.TestCase):
    def test_tenants_go_through_engine_with_entra_bearer_and_function_key(self):
        client, http, _ = make()
        http.responses.append((200, {}, json.dumps(
            [{"tenantId": "T1", "tenantName": "MPH", "tenantType": "ORGANISATION"}]).encode()))
        ts = client.tenants()
        self.assertEqual(ts[0]["tenant_id"], "T1")
        method, url, headers, data = http.calls[-1]
        self.assertEqual(url, "https://digivolve-xero.azurewebsites.net/api/msx/xero/connections")
        self.assertEqual(headers["Authorization"], "Bearer ENTRA-1")
        self.assertEqual(headers["x-functions-key"], "FK")
        # the Entra request carried the secret, the scope and nothing of Xero's
        tm, turl, th, tdata = http.calls[0]
        self.assertTrue(turl.startswith("https://login.microsoftonline.com/TENANT/"))
        body = dict(urllib.parse.parse_qsl(tdata.decode()))
        self.assertEqual(body["client_secret"], "CS")
        self.assertEqual(body["scope"], "api://EASYAUTH/.default")

    def test_entra_token_is_cached_until_near_expiry(self):
        client, http, t = make()
        http.responses += [(200, {}, b"[]"), (200, {}, b"[]"), (200, {}, b"[]")]
        http.token_responses.append((200, {}, json.dumps(
            {"access_token": "ENTRA-2", "expires_in": 3600}).encode()))
        client.tenants(); client.tenants()
        self.assertEqual(sum(1 for c in http.calls if "microsoftonline" in c[1]), 1)
        t[0] += 3600 - 60                       # inside the 120s margin
        client.tenants()
        self.assertEqual(http.calls[-1][2]["Authorization"], "Bearer ENTRA-2")

    def test_401_from_engine_drops_the_cached_token_once(self):
        client, http, _ = make()
        http.token_responses.append((200, {}, json.dumps(
            {"access_token": "ENTRA-2", "expires_in": 3600}).encode()))
        http.responses += [(401, {}, b'{"code":401}'), (200, {}, b"[]")]
        client.tenants()
        self.assertEqual(http.calls[-1][2]["Authorization"], "Bearer ENTRA-2")

    def test_xero_errors_pass_through_with_their_text(self):
        client, http, _ = make()
        http.responses.append((400, {}, json.dumps({"Elements": [{"ValidationErrors": [
            {"Message": "Account code '999' is not a valid code"}]}]}).encode()))
        with self.assertRaises(XeroError) as cm:
            client.create_manual_journal("T1", {"Narration": "x"}, idempotency_key="k")
        self.assertIn("999", str(cm.exception))
        self.assertEqual(http.calls[-1][2]["Idempotency-Key"], "k")
        self.assertEqual(http.calls[-1][2]["Xero-Tenant-Id"], "T1")

    def test_missing_credentials_is_auth_required_not_a_crash(self):
        client, http, _ = make(creds={})
        with self.assertRaises(AuthRequired) as cm:
            client.tenants()
        self.assertIn("msx auth login", str(cm.exception))
        self.assertFalse(client.token_status()["has_token"])

    def test_entra_refusal_names_the_config(self):
        client, http, _ = make()
        http.token_responses = [(401, {}, json.dumps(
            {"error": "invalid_client", "error_description": "AADSTS7000215 bad secret"}).encode())]
        with self.assertRaises(AuthRequired) as cm:
            client.tenants()
        self.assertIn("AADSTS7000215", str(cm.exception))
        self.assertIn("msx auth login", str(cm.exception))

    def test_login_stores_both_secrets_and_lists_tenants(self):
        client, http, _ = make(creds=None)
        http.responses.append((200, {}, b"[]"))
        answers = iter(["  fkey ", "csecret"])
        client.login(prompt=lambda label: next(answers))
        self.assertEqual(client.credential_store.saved[-1]["function_key"], "fkey")
        self.assertEqual(client.credential_store.saved[-1]["client_secret"], "csecret")
        self.assertEqual(http.calls[-1][2]["x-functions-key"], "fkey")


class FromConfig(unittest.TestCase):
    class Cfg:
        def __init__(self, xero):
            self.xero = xero

    def test_placeholders_hold(self):
        cfg = self.Cfg({"backend": "engine", "engine": {
            "base_url": "https://f", "credential_store": "file",
            "entra": {"tenant_id": "t", "client_id": "PUT-ME",
                      "scope": "api://x/.default"}}})
        with self.assertRaises(Hold) as cm:
            engine.client_from_config(cfg)
        self.assertIn("entra.client_id", str(cm.exception))

    def test_complete_config_builds_a_client(self):
        cfg = self.Cfg({"backend": "engine", "engine": {
            "base_url": "https://f/", "credential_store": "file",
            "credential_file": "/nonexistent/creds.json",
            "entra": {"tenant_id": "t", "client_id": "c",
                      "scope": "api://x/.default"}}})
        c = engine.client_from_config(cfg)
        self.assertEqual(c.base_url, "https://f")
        self.assertFalse(c.token_status()["has_token"])


if __name__ == "__main__":
    unittest.main()
