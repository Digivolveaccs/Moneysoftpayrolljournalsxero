"""Poster orchestration tests with a scripted fake Xero."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from msx import journal_builder, mapping, poster, state, summary_parser, sources, p30  # noqa: E402
from msx.errors import Hold, Skip  # noqa: E402
from msx.xero_client import XeroError  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class FakeXero:
    def __init__(self):
        self.tenant_list = [{"tenant_id": "T1", "name": "Browns Garage (Haywards Heath) Ltd",
                             "type": "ORGANISATION"}]
        self.org = {"name": "Browns Garage (Haywards Heath) Ltd",
                    "period_lock_date": "2026-03-31",
                    "end_of_year_lock_date": "2025-03-31"}
        self.chart = {}
        for code in ("230", "381", "6000", "471", "2200", "2210", "2211",
                     "6002", "6001", "836", "837", "2023", "2022", "2215"):
            self.chart[code] = {"name": f"Acct {code}", "status": "ACTIVE",
                                "system_account": None}
        self.existing = []
        self.full = {}
        self.created = []
        self.fail_create = None
        self.tamper = None
        self.status_changes = []

    def tenants(self):
        return self.tenant_list

    def organisation(self, tid):
        return self.org

    def accounts(self, tid):
        return self.chart

    def find_manual_journals(self, tid, start, end, include_deleted=False):
        return [e for e in self.existing if start <= e["date"] <= end
                and (include_deleted or e["status"] not in ("DELETED", "VOIDED"))]

    def find_manual_journals_by_narration(self, tid, narration):
        return [e for e in self.existing if e["narration"] == narration
                and e["status"] not in ("DELETED", "VOIDED")]

    def manual_journal(self, tid, jid):
        return self.full.get(jid)

    def create_manual_journal(self, tid, payload, idempotency_key=None):
        if self.fail_create:
            raise self.fail_create
        jid = f"MJ{len(self.created) + 1}"
        self.created.append((tid, payload, idempotency_key))
        self.full[jid] = {"ManualJournalID": jid, "Status": payload["Status"],
                          "Date": payload.get("Date"),
                          "JournalLines": [dict(l) for l in payload["JournalLines"]]}
        if self.tamper:
            self.tamper(self.full[jid])
        return {"ManualJournalID": jid, "Status": payload["Status"]}

    def set_manual_journal_status(self, tid, jid, status):
        self.status_changes.append((tid, jid, status))
        if jid in self.full:
            self.full[jid]["Status"] = status
        return {"ManualJournalID": jid, "Status": status}


def browns_mapping(mode="draft"):
    with open(os.path.join(FIX, "browns_mapping_legacy.json")) as fh:
        cfg = json.load(fh)
    cfg.update({"slug": "browns", "mode": mode,
                "moneysoft_employer": "Browns Garage (Haywards Heath) Limited",
                "xero": {"org_name": "Browns Garage (Haywards Heath) Ltd"}})
    return mapping.Mapping(cfg, "browns.json").ensure_valid()


class PosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = state.Ledger(os.path.join(self.tmp.name, "s.sqlite"),
                                   machine="test")
        pay = summary_parser.parse_files(
            [os.path.join(FIX, "browns_apr2026_tabbed.txt")])
        self.map = browns_mapping()
        self.journal = journal_builder.build(pay, self.map.cfg)
        self.xero = FakeXero()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_shadow_mode_writes_nothing(self):
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero,
                                mode="shadow")
        self.assertEqual(r.outcome, "shadow")
        self.assertEqual(self.xero.created, [])
        self.assertEqual(self.ledger.get("browns", "Apr-2026")["status"], "shadow")

    def test_draft_then_post_modes(self):
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(r.outcome, "draft")
        tid, payload, idem = self.xero.created[0]
        self.assertEqual(tid, "T1")
        self.assertEqual(payload["Status"], "DRAFT")
        self.assertEqual(len(idem), 64)
        row = self.ledger.get("browns", "Apr-2026")
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["xero_journal_id"], "MJ1")
        self.assertEqual(row["mapping_sha"], self.map.fingerprint)
        # second run: ledger short-circuits before touching Xero
        with self.assertRaises(Skip):
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(len(self.xero.created), 1)
        # approve promotes the draft
        poster.approve("browns", "Apr-2026", self.ledger, self.xero)
        self.assertEqual(self.xero.status_changes, [("T1", "MJ1", "POSTED")])
        self.assertEqual(self.ledger.get("browns", "Apr-2026")["status"], "posted")

    def test_post_mode_creates_posted(self):
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero,
                                mode="post")
        self.assertEqual(r.outcome, "posted")
        self.assertEqual(self.xero.created[0][1]["Status"], "POSTED")

    def test_locked_period_holds(self):
        self.xero.org["period_lock_date"] = "2026-04-30"
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("lock", str(cm.exception))
        self.assertEqual(self.xero.created, [])

    def test_missing_or_archived_code_holds(self):
        del self.xero.chart["2211"]
        self.xero.chart["6002"]["status"] = "ARCHIVED"
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("2211", str(cm.exception))
        self.assertIn("ARCHIVED", str(cm.exception))

    def test_system_account_holds(self):
        self.xero.chart["2210"]["system_account"] = "CISLIABILITY"
        with self.assertRaises(Hold):
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)

    def test_exact_duplicate_skips(self):
        self.xero.existing = [{"id": "OLD", "narration": "Payroll - April 2026 (M1)",
                               "status": "POSTED", "date": "2026-04-30"}]
        self.xero.full["OLD"] = {"JournalLines": [
            {"LineAmount": float(self.journal.total_debits)},
            {"LineAmount": -float(self.journal.total_debits)}]}
        with self.assertRaises(Skip) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("OLD", str(cm.exception))
        self.assertEqual(self.xero.created, [])

    def test_exact_duplicate_with_different_total_holds(self):
        self.xero.existing = [{"id": "OLD", "narration": "Payroll - April 2026 (M1)",
                               "status": "POSTED", "date": "2026-04-30"}]
        self.xero.full["OLD"] = {"JournalLines": [{"LineAmount": 1047.50},
                                                  {"LineAmount": -1047.50}]}
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("figures differ", str(cm.exception))

    def test_other_wages_journal_in_month_holds(self):
        self.xero.existing = [{"id": "REP", "narration": "Wages April 2026",
                               "status": "POSTED", "date": "2026-04-28"}]
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("Wages April 2026", str(cm.exception))
        self.assertEqual(self.xero.created, [])

    def test_unrelated_journal_in_month_is_fine(self):
        self.xero.existing = [{"id": "X", "narration": "Depreciation April 2026",
                               "status": "POSTED", "date": "2026-04-30"}]
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(r.outcome, "draft")

    def test_crash_recovery_adopts(self):
        # a previous run wrote the intent, POSTed, then died before recording
        self.ledger.mark_intent("browns", "Apr-2026",
                                narration="Payroll - April 2026 (M1)")
        self.xero.existing = [{"id": "MJX", "narration": "Payroll - April 2026 (M1)",
                               "status": "DRAFT", "date": "2026-04-30"}]
        self.xero.full["MJX"] = {"JournalLines": [
            {"LineAmount": float(self.journal.total_debits)},
            {"LineAmount": -float(self.journal.total_debits)}]}
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(r.outcome, "draft")
        self.assertEqual(r.xero_journal_id, "MJX")
        self.assertEqual(self.xero.created, [])
        self.assertEqual(self.ledger.get("browns", "Apr-2026")["xero_journal_id"],
                         "MJX")

    def test_xero_rejection_records_failed(self):
        self.xero.fail_create = XeroError("Xero POST ManualJournals -> 400: "
                                          "Account code '2211' is not valid",
                                          status=400, body={"x": 1})
        with self.assertRaises(Hold):
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        row = self.ledger.get("browns", "Apr-2026")
        self.assertEqual(row["status"], "failed")
        self.assertIn("2211", row["note"])

    def test_tenant_resolution(self):
        self.xero.tenant_list.append({"tenant_id": "T2", "name": "Other Ltd",
                                      "type": "ORGANISATION"})
        t = poster.resolve_tenant(self.map, self.xero.tenants())
        self.assertEqual(t["tenant_id"], "T1")
        self.map.cfg["xero"]["org_name"] = "Nope Ltd"
        with self.assertRaises(Hold):
            poster.resolve_tenant(self.map, self.xero.tenants())
        self.map.cfg["xero"] = {"tenant_id": "T2"}
        self.assertEqual(poster.resolve_tenant(self.map, self.xero.tenants())["name"],
                         "Other Ltd")

    def test_post_write_mismatch_holds(self):
        self.xero.tamper = lambda full: full["JournalLines"].pop()
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(cm.exception.stage, "verify")
        row = self.ledger.get("browns", "Apr-2026")
        self.assertEqual(row["xero_journal_id"], "MJ1")
        self.assertIn("POST-WRITE MISMATCH", row["note"])

    def test_wrong_account_class_or_name_holds(self):
        self.xero.chart["2200"]["class"] = "EXPENSE"
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("credits net", str(cm.exception))
        self.xero.chart["2200"]["class"] = "LIABILITY"
        self.xero.chart["2200"]["name"] = "PAYE Payable"        # the Brandtek trap
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("means something else", str(cm.exception))
        self.xero.chart["2200"]["name"] = "Wages Payable"
        self.map.cfg["xero"]["expect_names"] = {"2210": "PAYE Control"}
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("chart changed", str(cm.exception))

    def test_retryable_error_keeps_intent_and_reuses_key(self):
        self.xero.fail_create = XeroError("503", status=503, retryable=True)
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(cm.exception.stage, "post-retry")
        row = self.ledger.get("browns", "Apr-2026")
        self.assertEqual(row["status"], "posting")
        key = row["idempotency_key"]
        self.xero.fail_create = None
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(r.outcome, "draft")
        self.assertEqual(self.xero.created[0][2], key)

    def test_client_posts_own_journal(self):
        self.map.cfg["xero"]["client_posts_own_journal"] = True
        with self.assertRaises(Skip) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(cm.exception.reason, "client-posts-own")
        self.assertEqual(self.xero.created, [])
        self.assertEqual(self.ledger.get("browns", "Apr-2026")["status"], "skipped")

    def test_pay_date_journal_after_month_end_is_seen_by_duplicate_guard(self):
        # tax month Apr-2026 runs 6 Apr - 5 May; a hand-posted wages journal
        # dated on the 5 May pay date must still block a second April journal
        self.xero.existing = [{"id": "HAND", "narration": "Wages April 2026",
                               "status": "POSTED", "date": "2026-05-05"}]
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(cm.exception.stage, "duplicate")
        self.assertEqual(self.xero.created, [])

    def test_cost_code_on_a_liability_account_holds(self):
        # 858 'Pensions Payable' used as the employer pension COST would net
        # the employer cost against the payable and never reach the P&L
        for code in self.xero.chart.values():
            code.setdefault("class", "")
        self.xero.chart["6001"]["class"] = "LIABILITY"
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("uses it as a cost", str(cm.exception))
        self.xero.chart["6001"]["class"] = "EXPENSE"
        self.xero.chart["2210"]["class"] = "ASSET"
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertIn("as a payable", str(cm.exception))
        # a director's loan (liability or asset) may take net pay / dividend tax
        self.xero.chart["2210"]["class"] = "LIABILITY"
        self.xero.chart["836"]["class"] = "ASSET"
        self.xero.chart["471"]["class"] = "EQUITY"
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(r.outcome, "draft")

    def test_dry_run(self):
        r = poster.post_journal(self.journal, self.map, self.ledger, self.xero,
                                dry_run=True)
        self.assertEqual(r.outcome, "dry-run")
        self.assertEqual(self.xero.created, [])
        self.assertIsNone(self.ledger.get("browns", "Apr-2026"))


class SourcesTests(unittest.TestCase):
    def test_discovery_and_stability(self):
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "Acme Ltd 2026-27")
            os.makedirs(folder)
            names = ["Acme Ltd - Employer's Summary for Apr-2026.pdf",
                     "Acme Ltd - Employer's Summary (Additions) for Apr-2026.pdf",
                     "Acme Ltd - Employer's Summary for May-2026 (conflicted copy).pdf",
                     "Acme Ltd - P30 Employer's Payslip for Apr-2026.pdf",
                     "Acme Ltd - P30 Employer's Payslip for Apr-2026 to Jun-2026.pdf",
                     "Acme Ltd - Employee Payslip for Apr-2026 for A Person.pdf"]
            for n in names:
                with open(os.path.join(folder, n), "wb") as fh:
                    fh.write(b"%PDF-1.4 fake")
            rows = sources.find_summaries(d)
            kinds = [r["kind"] for r in rows]
            self.assertEqual(kinds.count("summary"), 2)
            self.assertEqual(kinds.count("conflict"), 1)
            groups = sources.group_summaries(rows)
            self.assertEqual(list(groups), [("Acme Ltd", "Apr-2026")])
            self.assertEqual({r["layout"] for r in groups[("Acme Ltd", "Apr-2026")]},
                             {None, "additions"})
            p = sources.find_p30(folder, "Acme Ltd", "Apr-2026")
            self.assertTrue(p.endswith("for Apr-2026.pdf"))
            p2 = sources.find_p30(folder, "Acme Ltd", "May-2026")
            self.assertTrue(p2.endswith("to Jun-2026.pdf"))
            path = os.path.join(folder, names[0])
            t = [1_000_000.0]
            self.assertFalse(sources.stable(path, settle_seconds=30,
                                            clock=lambda: os.stat(path).st_mtime + 5,
                                            sleep=lambda s: None))
            self.assertTrue(sources.stable(path, settle_seconds=30,
                                           clock=lambda: os.stat(path).st_mtime + 60,
                                           sleep=lambda s: None))
            self.assertTrue(sources.is_pdf(path))
            fp = sources.source_fingerprint([path])
            self.assertEqual(len(fp), 16)


class P30Tests(unittest.TestCase):
    TEXT = ("Acme Ltd 2026-27\nEmployer's Payslip for Apr-2026\n"
            "Tax & NIC due for Apr-2026        3,311.93\n"
            "Payment for Apr-2026              3,251.20\n"
            "Reference 123PA000123452701\nTo reach HMRC by 22-May-2026\n"
            "PAYE Ref 123/AB45678\n")

    def test_parse_and_cross_check(self):
        d = p30.parse_p30_text(self.TEXT)
        self.assertEqual(d["span"], "Apr-2026")
        self.assertEqual(d["due_for_period"], 3311.93)
        self.assertEqual(d["deadline"], "22-May-2026")
        self.assertEqual(d["paye_ref"], "123/AB45678")
        pay = summary_parser.parse_files(
            [os.path.join(FIX, "browns_apr2026_tabbed.txt")])
        j = journal_builder.build(pay, browns_mapping().cfg)
        self.assertEqual(p30.cross_check(d, j)[0], "ties")
        d["due_for_period"] = 3000.00
        self.assertEqual(p30.cross_check(d, j)[0], "mismatch")
        d["span"] = "Apr-2026 to Jun-2026"
        self.assertEqual(p30.cross_check(d, j)[0], "informational")
        self.assertEqual(p30.cross_check({}, j)[0], "missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ReviewRoundTwoPosterTests(PosterTests):
    """Regressions for the second review round (idempotency / wrong-org)."""

    def test_pinned_tenant_must_match_org_name(self):
        self.xero.tenant_list.append({"tenant_id": "T2", "name": "Other Client Ltd",
                                      "type": "ORGANISATION"})
        self.map.cfg["xero"]["tenant_id"] = "T2"        # copy/paste slip
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(cm.exception.stage, "tenant")
        self.assertIn("Other Client Ltd", str(cm.exception))
        self.assertEqual(self.xero.created, [])

    def test_journal_dated_outside_month_is_still_found(self):
        # one-off re-date for a locked period: journal sits on 1 June
        self.map.cfg["journal_date_overrides"] = {"Apr-2026": "01/06/2026"}
        from msx import journal_builder, summary_parser
        pay = summary_parser.parse_files([os.path.join(FIX, "browns_apr2026_tabbed.txt")])
        j = journal_builder.build(pay, self.map.cfg)
        self.assertEqual(j.date, "01/06/2026")
        r = poster.post_journal(j, self.map, self.ledger, self.xero)
        self.assertEqual(r.outcome, "draft")
        self.xero.existing = [{"id": "MJ1", "narration": j.narration,
                               "status": "DRAFT", "date": "2026-06-01"}]
        # the override is removed: same lines, month-end date; ledger cleared
        self.ledger.db.execute("DELETE FROM journals")
        j2 = journal_builder.build(pay, browns_mapping().cfg)
        with self.assertRaises(Skip):
            poster.post_journal(j2, self.map, self.ledger, self.xero)
        self.assertEqual(len(self.xero.created), 1)

    def test_transport_error_keeps_posting_row(self):
        def boom(*a, **k):
            raise OSError("connection reset")
        self.xero.create_manual_journal = boom
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(cm.exception.stage, "post-retry")
        self.assertEqual(self.ledger.get("browns", "Apr-2026")["status"], "posting")

    def test_posting_row_with_different_tenant_holds(self):
        self.ledger.mark_intent("browns", "Apr-2026", xero_tenant_id="T9",
                                idempotency_key="K", payload_sha=self.journal.figures_fingerprint())
        with self.assertRaises(Hold) as cm:
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        self.assertEqual(cm.exception.stage, "tenant")
        self.assertEqual(self.xero.created, [])

    def test_shadow_does_not_clobber_posting_row(self):
        self.ledger.mark_intent("browns", "Apr-2026")
        with self.assertRaises(Hold):
            poster.post_journal(self.journal, self.map, self.ledger, self.xero, mode="shadow")
        self.assertEqual(self.ledger.get("browns", "Apr-2026")["status"], "posting")

    def test_approve_refuses_post_write_mismatch(self):
        self.xero.tamper = lambda full: full["JournalLines"].pop()
        with self.assertRaises(Hold):
            poster.post_journal(self.journal, self.map, self.ledger, self.xero)
        with self.assertRaises(Hold) as cm:
            poster.approve("browns", "Apr-2026", self.ledger, self.xero)
        self.assertEqual(cm.exception.stage, "verify")
        self.assertEqual(self.xero.status_changes, [])
