import datetime
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from msx import mapping, recon, state  # noqa: E402
from test_poster import FakeXero, browns_mapping  # noqa: E402


class ReconTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = state.Ledger(os.path.join(self.tmp.name, "s.sqlite"), machine="t")
        self.m = browns_mapping(mode="post")
        self.xero = FakeXero()
        self.today = datetime.date(2026, 9, 26)

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def add(self, jid, narration, status, date, total):
        self.xero.existing.append({"id": jid, "narration": narration,
                                   "status": status, "date": date})
        self.xero.full[jid] = {"JournalLines": [{"LineAmount": total},
                                                {"LineAmount": -total}]}

    def sweep(self, periods=("Apr-2026",)):
        return recon.sweep({"browns": self.m}, self.led, lambda m: self.xero,
                           periods=list(periods), today=self.today)

    def test_clean(self):
        self.led.mark_posted("browns", "Apr-2026", xero_journal_id="MJ1",
                             total_debits="41463.73", narration="Payroll - April 2026 (M1)")
        self.add("MJ1", "Payroll - April 2026 (M1)", "POSTED", "2026-04-30", 41463.73)
        self.assertEqual(self.sweep(), [])

    def test_missing_mismatch_orphan_duplicate(self):
        self.led.mark_posted("browns", "Apr-2026", xero_journal_id="MJ1",
                             total_debits="41463.73")
        self.add("MJ1", "Payroll - April 2026 (M1)", "VOIDED", "2026-04-30", 41463.73)
        self.add("REP", "Wages April 2026", "POSTED", "2026-04-28", 2095.00)
        self.add("REP2", "Wages April 2026", "POSTED", "2026-04-28", 2095.00)
        kinds = sorted(f["kind"] for f in self.sweep())
        self.assertEqual(kinds, ["duplicate", "missing", "orphan", "orphan"])

    def test_mismatch_and_draft_aging(self):
        self.led.mark_posted("browns", "Apr-2026", xero_journal_id="MJ1",
                             status="draft", total_debits="41463.73")
        self.led.db.execute("UPDATE journals SET created_at='2026-08-01T00:00:00+00:00'")
        self.add("MJ1", "Payroll - April 2026 (M1)", "DRAFT", "2026-04-30", 41000.00)
        kinds = sorted(f["kind"] for f in self.sweep())
        self.assertEqual(kinds, ["draft-aging", "mismatch"])

    def test_shadow_clients_with_nothing_posted_are_skipped(self):
        self.m.cfg["mode"] = "shadow"
        self.add("REP", "Wages April 2026", "POSTED", "2026-04-28", 2095.00)
        self.assertEqual(self.sweep(), [])
        self.assertIn("no findings", recon.render([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
