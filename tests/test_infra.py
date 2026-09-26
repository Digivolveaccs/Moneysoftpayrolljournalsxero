"""Mapping, Xero client (fake transport) and ledger tests.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from msx import mapping, state, xero_client  # noqa: E402
from msx.errors import Hold  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def browns_cfg():
    with open(os.path.join(FIX, "browns_mapping_legacy.json")) as fh:
        cfg = json.load(fh)
    cfg["slug"] = "browns-garage-haywards-heath"
    cfg["moneysoft_employer"] = "Browns Garage (Haywards Heath) Limited"
    cfg["xero"] = {"org_name": "Browns Garage (Haywards Heath) Ltd",
                   "tenant_id": "11111111-2222-3333-4444-555555555555"}
    cfg["mode"] = "draft"
    return cfg


class MappingTests(unittest.TestCase):
    def test_valid_and_fingerprint_stable(self):
        m = mapping.Mapping(browns_cfg(), "x.json")
        self.assertEqual(m.problems, [])
        m2 = mapping.Mapping(json.loads(json.dumps(browns_cfg())), "y.json")
        self.assertEqual(m.fingerprint, m2.fingerprint)
        cfg = browns_cfg()
        cfg["codes"]["wages_payable"] = "2201"
        self.assertNotEqual(mapping.Mapping(cfg).fingerprint, m.fingerprint)

    def test_problems(self):
        cfg = browns_cfg()
        cfg["mode"] = "yolo"
        cfg["codes"]["paye_payable"] = ""
        cfg["codes"]["bogus"] = "1"
        cfg["employees"]["Nobody"] = {}
        cfg["narration"] = "Payroll"
        p = mapping.Mapping(cfg).problems
        self.assertTrue(any("mode" in x for x in p))
        self.assertTrue(any("paye_payable" in x for x in p))
        self.assertTrue(any("bogus" in x for x in p))
        self.assertTrue(any("Nobody" in x for x in p))
        self.assertTrue(any("narration" in x for x in p))
        with self.assertRaises(Hold):
            mapping.Mapping(cfg).ensure_valid()

    def test_post_mode_needs_xero(self):
        cfg = browns_cfg()
        cfg["mode"] = "post"
        cfg["xero"] = {}
        self.assertTrue(any("xero" in x for x in mapping.Mapping(cfg).problems))

    def test_matching_report_header(self):
        m = mapping.Mapping(browns_cfg())
        self.assertTrue(m.matches_employer(
            "Browns Garage (Haywards Heath) Limited"))
        self.assertTrue(m.matches_employer("Browns Garage (Haywards Heath) Ltd"))
        self.assertFalse(m.matches_employer("Brown Garage Ltd"))
        found = mapping.find_for_report({"a": m},
                                        "Browns Garage (Haywards Heath) Limited")
        self.assertIs(found, m)
        with self.assertRaises(Hold) as cm:
            mapping.find_for_report({"a": m}, "Acme Ltd")
        self.assertIn("clients/acme.json", str(cm.exception))

    def test_placeholders_and_load_all(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = browns_cfg()
            cfg["codes"]["pensions_payable"] = "TBC-2211"
            with open(os.path.join(d, "browns.json"), "w") as fh:
                json.dump(cfg, fh)
            with open(os.path.join(d, "_template.json"), "w") as fh:
                json.dump({"client": "T", "codes": {}, "employees": {}}, fh)
            all_ = mapping.load_all(d)
            self.assertEqual(list(all_), ["browns-garage-haywards-heath"])
            self.assertEqual(mapping.placeholders(cfg),
                             ["codes.pensions_payable=TBC-2211"])


class FakeTransport:
    """Scripted responses keyed by (method, path-prefix)."""

    def __init__(self):
        self.calls = []
        self.script = []

    def expect(self, method, path, status=200, body=None, headers=None):
        self.script.append((method, path, status, body, headers or {}))

    def request(self, method, url, headers=None, data=None, timeout=60):
        self.calls.append((method, url, headers or {}, data))
        for i, (m, p, status, body, h) in enumerate(self.script):
            if m == method and p in url:
                self.script.pop(i)
                raw = json.dumps(body).encode() if body is not None else b""
                return status, h, raw
        raise AssertionError(f"unexpected {method} {url}")


class XeroClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = xero_client.FileTokenStore(
            os.path.join(self.tmp.name, "tokens.json"))
        self.transport = FakeTransport()
        self.sleeps = []
        self.now = [1_000_000]
        self.client = xero_client.XeroClient(
            client_id="cid", token_store=self.store, transport=self.transport,
            sleep=self.sleeps.append, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def seed_tokens(self, age=0):
        self.store.save({"access_token": "A1", "refresh_token": "R1",
                         "expires_in": 1800, "obtained_at": self.now[0] - age})

    def test_no_token_requires_login(self):
        with self.assertRaises(xero_client.AuthRequired):
            self.client.tenants()

    def test_refresh_is_written_before_use(self):
        self.seed_tokens(age=1800)
        self.transport.expect("POST", "identity.xero.com/connect/token",
                              body={"access_token": "A2", "refresh_token": "R2",
                                    "expires_in": 1800})
        self.transport.expect("GET", "api.xero.com/connections",
                              body=[{"tenantId": "t1", "tenantName": "Org 1",
                                     "tenantType": "ORGANISATION"}])
        tenants = self.client.tenants()
        self.assertEqual(tenants[0]["name"], "Org 1")
        saved = self.store.load()
        self.assertEqual(saved["refresh_token"], "R2")
        self.assertEqual(oct(os.stat(self.store.path).st_mode)[-3:], "600")
        # the API call carried the NEW access token
        self.assertEqual(self.transport.calls[-1][2]["Authorization"],
                         "Bearer A2")

    def test_429_honours_retry_after(self):
        self.seed_tokens()
        self.transport.expect("GET", "/Organisation", status=429, body={},
                              headers={"Retry-After": "7",
                                       "X-Rate-Limit-Problem": "minute"})
        self.transport.expect("GET", "/Organisation",
                              body={"Organisations": [{
                                  "Name": "Org 1",
                                  "PeriodLockDate": "/Date(1743379200000+0000)/"}]})
        org = self.client.organisation("t1")
        self.assertEqual(org["name"], "Org 1")
        self.assertEqual(org["period_lock_date"], "2025-03-31")
        self.assertTrue(7 <= self.sleeps[0] < 8.5)

    def test_validation_error_surfaces(self):
        self.seed_tokens()
        self.transport.expect("POST", "/ManualJournals", status=400, body={
            "ErrorNumber": 10, "Type": "ValidationException",
            "Message": "A validation exception occurred",
            "Elements": [{"ValidationErrors": [
                {"Message": "Account code '9999' is not a valid code"}]}]})
        with self.assertRaises(xero_client.XeroError) as cm:
            self.client.create_manual_journal(
                "t1", {"Narration": "x", "JournalLines": []},
                idempotency_key="abc")
        self.assertIn("9999", str(cm.exception))
        self.assertEqual(self.transport.calls[-1][2]["Idempotency-Key"], "abc")
        self.assertEqual(self.transport.calls[-1][2]["Xero-Tenant-Id"], "t1")

    def test_create_and_post(self):
        self.seed_tokens()
        self.transport.expect("POST", "/ManualJournals", body={
            "ManualJournals": [{"ManualJournalID": "MJ1", "Status": "DRAFT",
                                "Narration": "Payroll - April 2026 (M1)"}]})
        j = self.client.create_manual_journal("t1", {"Narration": "n"})
        self.assertEqual(j["ManualJournalID"], "MJ1")
        self.transport.expect("POST", "/ManualJournals/MJ1", body={
            "ManualJournals": [{"ManualJournalID": "MJ1", "Status": "POSTED"}]})
        j2 = self.client.set_manual_journal_status("t1", "MJ1", "POSTED")
        self.assertEqual(j2["Status"], "POSTED")
        body = json.loads(self.transport.calls[-1][3])
        self.assertEqual(body["ManualJournals"][0]["Status"], "POSTED")

    def test_find_manual_journals_filters_deleted_and_pages(self):
        self.seed_tokens()
        page1 = [{"ManualJournalID": f"J{i}", "Narration": "Payroll - April 2026 (M1)",
                  "Status": "POSTED", "Date": "/Date(1777507200000+0000)/"}
                 for i in range(100)]
        page2 = [{"ManualJournalID": "JX", "Narration": "old", "Status": "DELETED",
                  "Date": "/Date(1777507200000+0000)/"}]
        self.transport.expect("GET", "/ManualJournals", body={"ManualJournals": page1})
        self.transport.expect("GET", "/ManualJournals", body={"ManualJournals": page2})
        rows = self.client.find_manual_journals("t1", "2026-04-01", "2026-04-30")
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["date"], "2026-04-30")
        url = self.transport.calls[-2][1]
        self.assertIn("Date%3E%3DDateTime%282026%2C4%2C1%29", url)

    def test_401_triggers_single_refresh(self):
        self.seed_tokens()
        self.transport.expect("GET", "/Accounts", status=401, body={})
        self.transport.expect("POST", "identity.xero.com/connect/token",
                              body={"access_token": "A2", "refresh_token": "R2",
                                    "expires_in": 1800})
        self.transport.expect("GET", "/Accounts", body={"Accounts": [
            {"Code": "477", "Name": "Salaries", "Status": "ACTIVE",
             "Type": "EXPENSE"}]})
        acc = self.client.accounts("t1")
        self.assertEqual(acc["477"]["name"], "Salaries")


class LedgerTests(unittest.TestCase):
    def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            led = state.Ledger(os.path.join(d, "state.sqlite"), machine="mac1")
            led.start_run("run-1")
            self.assertIsNone(led.get("acme", "Apr-2026"))
            led.mark_intent("acme", "Apr-2026", narration="Payroll - April 2026 (M1)",
                            journal_date="2026-04-30", total_debits="100.00",
                            mapping_sha="abc", payload_sha="def",
                            xero_tenant_id="t1", mode="post")
            row = led.get("acme", "Apr-2026")
            self.assertEqual(row["status"], "posting")
            led.mark_posted("acme", "Apr-2026", xero_journal_id="MJ1")
            row = led.get("acme", "Apr-2026")
            self.assertEqual(row["status"], "posted")
            self.assertEqual(row["xero_journal_id"], "MJ1")
            self.assertEqual(row["narration"], "Payroll - April 2026 (M1)")
            led.mark_held("beta", "Apr-2026", "no mapping")
            led.finish_run({"posted": 1, "held": 1})
            self.assertEqual(led.last_run()["run_id"], "run-1")
            ev = led.events_for("acme", "Apr-2026")
            self.assertEqual([e["kind"] for e in ev],
                             ["journal_posting", "journal_posted"])
            n = led.export_csv(os.path.join(d, "ledger.csv"))
            self.assertEqual(n, 2)
            with self.assertRaises(ValueError):
                led.upsert("x", "y", status="nonsense")
            led.close()

    def test_stale_intents(self):
        with tempfile.TemporaryDirectory() as d:
            led = state.Ledger(os.path.join(d, "s.sqlite"), machine="m")
            led.mark_intent("acme", "May-2026")
            self.assertEqual(led.stale_intents(older_than_minutes=0), [])
            led.db.execute("UPDATE journals SET updated_at='2000-01-01T00:00:00+00:00'")
            self.assertEqual(len(led.stale_intents(older_than_minutes=30)), 1)
            led.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
