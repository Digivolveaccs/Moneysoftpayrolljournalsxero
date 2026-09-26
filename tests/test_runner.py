"""End-to-end run over a fixture folder with a fake Xero (no network)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from msx import config as config_mod, runner, state  # noqa: E402
from test_poster import FakeXero  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.pdf_root = os.path.join(root, "PDF attachments")
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        os.makedirs(folder)
        shutil.copy(os.path.join(FIX, "browns_apr2026_tabbed.txt"),
                    os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                                 " - Employer's Summary for Apr-2026.txt"))
        shutil.copy(os.path.join(FIX, "browns_jul2026_layout.txt"),
                    os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                                 " - Employer's Summary for Jul-2026.txt"))
        # make the files look settled
        old = 1_600_000_000
        for n in os.listdir(folder):
            os.utime(os.path.join(folder, n), (old, old))
        self.clients = os.path.join(root, "clients")
        os.makedirs(self.clients)
        shutil.copy(os.path.join(os.path.dirname(HERE), "clients",
                                 "browns-garage-haywards-heath.json"),
                    os.path.join(self.clients, "browns-garage-haywards-heath.json"))
        self.cfg = config_mod.Config({
            "pdf_root": self.pdf_root, "clients_dir": self.clients,
            "state_db": os.path.join(root, "state.sqlite"),
            "out_dir": os.path.join(root, "out"),
            "ledger_csv": os.path.join(root, "ledger.csv"),
            "settle_seconds": 0, "stability_sample_seconds": 0,
            "machine_name": "test-mac",
            "notify": {}}, path="test")
        self.xero = FakeXero()
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def set_mode(self, mode):
        p = os.path.join(self.clients, "browns-garage-haywards-heath.json")
        with open(p) as fh:
            cfg = json.load(fh)
        cfg["mode"] = mode
        with open(p, "w") as fh:
            json.dump(cfg, fh)

    def run_once(self, **kw):
        return runner.run_once(self.cfg, xero_factory=lambda app: self.xero,
                               log=self.logs.append, notify_enabled=False,
                               **kw)

    def test_shadow_then_draft_then_idempotent(self):
        s = self.run_once()
        self.assertEqual([e["period"] for e in s["shadow"]],
                         ["Apr-2026", "Jul-2026"])
        self.assertEqual(s["held"], [])
        self.assertEqual(self.xero.created, [])
        self.assertTrue(os.path.exists(os.path.join(
            self.cfg.out_dir, "browns-garage-haywards-heath", "Apr-2026",
            "journal.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.cfg.out_dir, "HOLDS.md")))
        self.assertTrue(os.path.exists(self.cfg.ledger_csv))
        # second shadow run reports nothing new
        s2 = self.run_once()
        self.assertEqual(s2["shadow"], [])
        self.assertEqual(s2["already_shadow"], 2)
        # switch to draft: both months go to Xero as drafts
        self.set_mode("draft")
        s3 = self.run_once()
        self.assertEqual(len(s3["draft"]), 2)
        self.assertEqual(len(self.xero.created), 2)
        self.assertEqual(self.xero.created[0][1]["Status"], "DRAFT")
        # idempotent: nothing more is created, nothing reported
        s4 = self.run_once()
        self.assertEqual(len(self.xero.created), 2)
        self.assertEqual(s4["draft"], [])
        self.assertEqual(s4["skipped"], [])
        led = state.Ledger(self.cfg.state_db)
        row = led.get("browns-garage-haywards-heath", "Apr-2026")
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["xero_journal_id"], "MJ1")
        led.close()

    def test_rerun_after_posting_holds(self):
        self.set_mode("post")
        s = self.run_once()
        self.assertEqual(len(s["posted"]), 2)
        # the payroll is re-run: the filed report now shows different figures
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        p = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                         " - Employer's Summary for Apr-2026.txt")
        with open(p) as fh:
            text = fh.read()
        text = text.replace("Sally Jones\tK92*\t702.00\t702.00", "Sally Jones\tK92*\t802.00\t802.00")
        text = text.replace("155.80\t \t546.20", "175.80\t \t626.20")
        # keep the report's own totals consistent with the edit
        text = text.replace("37,255.47\t20,596.67", "37,355.47\t20,696.67")
        text = text.replace("2,537.80\t0.00\t31,865.63", "2,557.80\t0.00\t31,945.63")
        text = text.replace("Total Net Pay\t31,865.63", "Total Net Pay\t31,945.63")
        text = text.replace("PAYE Tax\t2,537.80", "PAYE Tax\t2,557.80")
        text = text.replace("Total Tax Due\t2,537.80", "Total Tax Due\t2,557.80")
        text = text.replace("Total Tax & NIC Due\t3,311.93", "Total Tax & NIC Due\t3,331.93")
        text = text.replace("Tax & NIC due for Apr-2026\t3,311.93", "Tax & NIC due for Apr-2026\t3,331.93")
        text = text.replace("TOTAL NET OUTLAY\t35,915.43", "TOTAL NET OUTLAY\t36,015.43")
        with open(p, "w") as fh:
            fh.write(text)
        os.utime(p, (1_600_000_000, 1_600_000_000))
        s2 = self.run_once()
        self.assertEqual(len(s2["held"]), 1)
        self.assertEqual(s2["held"][0]["stage"], "rerun")
        self.assertIn("re-run after posting", s2["held"][0]["note"])
        self.assertEqual(len(self.xero.created), 2)

    def test_unknown_client_holds_with_instruction(self):
        folder = os.path.join(self.pdf_root, "Acme Ltd 2026-27")
        os.makedirs(folder)
        shutil.copy(os.path.join(FIX, "browns_apr2026_tabbed.txt"),
                    os.path.join(folder, "Acme Ltd - Employer's Summary for Apr-2026.txt"))
        os.utime(os.path.join(folder, "Acme Ltd - Employer's Summary for Apr-2026.txt"),
                 (1_600_000_000, 1_600_000_000))
        s = self.run_once()
        held = [h for h in s["held"] if h["client"] == "Acme Ltd"]
        self.assertEqual(len(held), 1)
        self.assertIn("clients/acme.json", held[0]["note"])
        self.assertEqual(len(s["shadow"]), 2)     # others still processed

    def test_conflicted_copy_is_held(self):
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        shutil.copy(os.path.join(FIX, "browns_apr2026_tabbed.txt"),
                    os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                                 " - Employer's Summary for Apr-2026 (conflicted copy).txt"))
        s = self.run_once()
        self.assertTrue(any(h["stage"] == "discover" for h in s["held"]))

    def test_old_periods_ignored_and_filters(self):
        s = self.run_once(min_period="Jun-2026")
        self.assertEqual([e["period"] for e in s["shadow"]], ["Jul-2026"])
        self.assertEqual(s["ignored_old"], 1)
        s2 = self.run_once(only_periods=["Apr-2026"], min_period="Apr-2026")
        self.assertEqual([e["period"] for e in s2["shadow"]], ["Apr-2026"])

    def test_circuit_breaker_defers_when_xero_is_down(self):
        from msx.xero_client import XeroError
        self.set_mode("post")
        self.xero.fail_create = XeroError("503", status=503, retryable=True)
        # add a third client-period so the breaker (3 failures) trips
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        p = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                         " - Employer's Summary for May-2026.txt")
        with open(os.path.join(FIX, "browns_apr2026_tabbed.txt")) as fh:
            text = fh.read().replace("Apr-2026", "May-2026").replace("May-2026\t60.73", "Jun-2026\t60.73")
        with open(p, "w") as fh:
            fh.write(text)
        os.utime(p, (1_600_000_000, 1_600_000_000))
        s = self.run_once()
        self.assertEqual(len(s["pending"]), 3)
        self.assertEqual(s["held"], [])
        self.assertTrue(any("deferred" in w for w in s["warnings"]))
        # Xero back: everything posts on the next run, keys reused
        self.xero.fail_create = None
        s2 = self.run_once()
        self.assertEqual(len(s2["posted"]), 3)

    def test_pending_when_file_not_settled(self):
        self.cfg.data["settle_seconds"] = 10 ** 9
        s = self.run_once()
        self.assertEqual(len(s["pending"]), 2)
        self.assertEqual(s["shadow"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class YearToDateEaTests(unittest.TestCase):
    def test_cumulative_ea_over_annual_max_holds(self):
        import shutil as _sh
        from msx import state as _state
        tmp = tempfile.TemporaryDirectory()
        try:
            root = tmp.name
            pdf_root = os.path.join(root, "PDF attachments")
            folder = os.path.join(pdf_root, "Browns Garage (Haywards Heath) Limited 2026-27")
            os.makedirs(folder)
            p = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                             " - Employer's Summary for Aug-2026.txt")
            with open(os.path.join(FIX, "browns_apr2026_tabbed.txt")) as fh:
                text = fh.read().replace("Apr-2026", "Aug-2026").replace("May-2026", "Sep-2026")
            with open(p, "w") as fh:
                fh.write(text)
            os.utime(p, (1_600_000_000, 1_600_000_000))
            clients = os.path.join(root, "clients")
            os.makedirs(clients)
            _sh.copy(os.path.join(os.path.dirname(HERE), "clients",
                                  "browns-garage-haywards-heath.json"), clients)
            cfg = config_mod.Config({
                "pdf_root": pdf_root, "clients_dir": clients,
                "state_db": os.path.join(root, "state.sqlite"),
                "out_dir": os.path.join(root, "out"), "settle_seconds": 0,
                "stability_sample_seconds": 0, "notify": {}}, path="test")
            led = _state.Ledger(cfg.state_db)
            for m in ("Apr-2026", "May-2026", "Jun-2026", "Jul-2026"):
                led.upsert("browns-garage-haywards-heath", m, status="posted",
                           ea="2300.00", er_nic="2300.00", xero_journal_id="x")
            led.close()
            s = runner.run_once(cfg, xero_factory=lambda a: FakeXero(),
                                log=lambda m: None, notify_enabled=False)
            self.assertEqual(len(s["held"]), 1)
            self.assertIn("annual maximum", s["held"][0]["note"])
        finally:
            tmp.cleanup()
