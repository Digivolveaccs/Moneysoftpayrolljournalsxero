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

    def test_standby_role_is_shadow_only(self):
        self.set_mode("post")
        self.cfg.data["role"] = "standby"
        s = self.run_once()
        self.assertEqual(s["posted"], [])
        self.assertEqual(len(s["shadow"]), 2)
        self.assertTrue(any("standby" in w for w in s["warnings"]))
        s2 = self.run_once(take_over=True)
        self.assertEqual(len(s2["posted"]), 2)

    def test_mode_change_is_not_a_rerun_and_rerun_is_not_hidden(self):
        self.set_mode("draft")
        s = self.run_once()
        self.assertEqual(len(s["draft"]), 2)
        self.set_mode("post")                    # promotion: nothing re-posts
        s2 = self.run_once()
        self.assertEqual(s2["held"], [])
        self.assertEqual(len(self.xero.created), 2)
        # a genuine figure change after the promotion is still caught
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        p = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                         " - Employer's Summary for Jul-2026.txt")
        with open(p) as fh:
            text = fh.read()
        text = text.replace("Sally Jones            K96      702.00       702.00",
                            "Sally Jones            K96      802.00       802.00")
        with open(p, "w") as fh:
            fh.write(text)
        os.utime(p, (1_600_000_000, 1_600_000_000))
        s3 = self.run_once()
        # the edited report no longer reconciles to its own totals -> held at
        # build, or if it did reconcile -> held as a rerun; either way held
        self.assertEqual(len(s3["held"]), 1)

    def test_holds_reported_once_until_they_change(self):
        with open(os.path.join(self.clients, "browns-garage-haywards-heath.json")) as fh:
            cfg = json.load(fh)
        del cfg["employees"]["Sally Jones"]          # unmapped employee -> hold
        with open(os.path.join(self.clients, "browns-garage-haywards-heath.json"), "w") as fh:
            json.dump(cfg, fh)
        s = self.run_once()
        self.assertEqual(len(s["held"]), 2)
        self.assertTrue(runner.holds_changed(self.cfg.out_dir, s))
        self.assertTrue(runner.holds_changed(self.cfg.out_dir, s))   # not yet sent
        runner.mark_holds_reported(self.cfg.out_dir, s)
        self.assertFalse(runner.holds_changed(self.cfg.out_dir, s))
        self.assertEqual(runner.system_problems(s), [])
        s["held"].append({"client": "x", "period": "y", "stage": "bug",
                          "note": "boom"})
        self.assertEqual(len(runner.system_problems(s)), 1)

    def test_failed_report_is_retried_and_flagged(self):
        from msx import notify
        with open(os.path.join(self.clients, "browns-garage-haywards-heath.json")) as fh:
            cfg = json.load(fh)
        del cfg["employees"]["Sally Jones"]
        with open(os.path.join(self.clients, "browns-garage-haywards-heath.json"), "w") as fh:
            json.dump(cfg, fh)
        self.cfg.data["notify"] = {"missive_token_file": os.path.join(self.tmp.name, "nope"),
                                   "report_to": "m@x", "heartbeat_file":
                                   os.path.join(self.tmp.name, "hb.json")}
        attempts = []
        orig = notify.send_missive_report

        def failing(*a, **k):
            attempts.append(1)
            return {"ok": False, "error": "missing token"}
        notify.send_missive_report = failing
        try:
            s1 = runner.run_once(self.cfg, xero_factory=lambda a: self.xero,
                                 log=self.logs.append, notify_enabled=True)
            s2 = runner.run_once(self.cfg, xero_factory=lambda a: self.xero,
                                 log=self.logs.append, notify_enabled=True)
        finally:
            notify.send_missive_report = orig
        self.assertEqual(len(attempts), 2)                # retried, not silenced
        self.assertTrue(any("could not be emailed" in p for p in s2["system_problems"]))
        # once a send succeeds the standing hold stops being re-sent
        notify.send_missive_report = lambda *a, **k: {"ok": True}
        try:
            runner.run_once(self.cfg, xero_factory=lambda a: self.xero,
                            log=self.logs.append, notify_enabled=True)
            s4 = runner.run_once(self.cfg, xero_factory=lambda a: self.xero,
                                 log=self.logs.append, notify_enabled=True)
        finally:
            notify.send_missive_report = orig
        self.assertNotIn("notify", s4)

    def test_march_report_after_6_april_is_processed(self):
        import datetime as _dt
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        p = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                         " - Employer's Summary for Mar-2027.txt")
        with open(os.path.join(FIX, "browns_apr2026_tabbed.txt")) as fh:
            text = fh.read().replace("Apr-2026", "Mar-2027").replace("May-2026", "Apr-2027")
        with open(p, "w") as fh:
            fh.write(text)
        os.utime(p, (1_600_000_000, 1_600_000_000))
        s = self.run_once(clock=lambda: _dt.datetime(2027, 4, 10, 9, 0))
        self.assertIn("Mar-2027", [e["period"] for e in s["shadow"]])
        self.assertEqual(s["ignored"], [])
        # genuinely old periods are still ignored, but visibly
        s2 = self.run_once(clock=lambda: _dt.datetime(2028, 6, 1, 9, 0))
        self.assertTrue(s2["ignored"])
        self.assertTrue(any("ignored" in w for w in s2["warnings"]))

    def test_misnamed_report_file_holds(self):
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        src = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                           " - Employer's Summary for Apr-2026.txt")
        dst = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                           " - Employer's Summary for Jun-2026.txt")
        shutil.copy(src, dst)
        os.utime(dst, (1_600_000_000, 1_600_000_000))
        s = self.run_once()
        held = [h for h in s["held"] if h["period"] == "Jun-2026"]
        self.assertEqual(len(held), 1)
        self.assertIn("named for Jun-2026", held[0]["note"])

    def test_run_lock_refuses_second_run(self):
        from msx.errors import Hold
        with runner.RunLock(self.cfg.state_db + ".lock"):
            with self.assertRaises(Hold):
                self.run_once()

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


class ReviewRoundTwoRunnerTests(RunnerTests):
    def folder(self):
        return os.path.join(self.pdf_root,
                            "Browns Garage (Haywards Heath) Limited 2026-27")

    def test_nil_rerun_after_posting_holds(self):
        self.set_mode("post")
        self.run_once()
        p = os.path.join(self.folder(), "Browns Garage (Haywards Heath) Limited"
                         " - Employer's Summary for Apr-2026.txt")
        nil = ("Browns Garage (Haywards Heath) Limited 2026-27\nEmployer's Summary\n"
               "Apr-2026\nAll Employees, Layout: Medium\n\n"
               "Employer Totals:\n\tPAYE Month\t\nTotal Net Pay\t0.00\t\n"
               "Total Tax & NIC Due\t0.00\t\nTOTAL NET OUTLAY\t0.00\t\n")
        with open(p, "w") as fh:
            fh.write(nil)
        os.utime(p, (1_600_000_000, 1_600_000_000))
        s = self.run_once()
        held = [h for h in s["held"] if h["period"] == "Apr-2026"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["stage"], "rerun")
        row = state.Ledger(self.cfg.state_db).get("browns-garage-haywards-heath", "Apr-2026")
        self.assertEqual(row["status"], "posted")            # never downgraded
        # the same hold is reported on every run until cleared
        s2 = self.run_once()
        self.assertEqual(len([h for h in s2["held"] if h["period"] == "Apr-2026"]), 1)

    def test_post_write_mismatch_is_reheld_every_run(self):
        self.set_mode("draft")
        self.xero.tamper = lambda full: full["JournalLines"].pop()
        s1 = self.run_once()
        self.assertEqual(len(s1["held"]), 2)
        self.xero.tamper = None
        s2 = self.run_once()
        self.assertEqual(len(s2["held"]), 2)
        self.assertTrue(all(h["stage"] == "verify" for h in s2["held"]))
        self.assertEqual(len(self.xero.created), 2)

    def test_posting_row_survives_a_prewrite_hold(self):
        self.set_mode("post")
        led = state.Ledger(self.cfg.state_db)
        led.mark_intent("browns-garage-haywards-heath", "Apr-2026",
                        xero_tenant_id="T1", idempotency_key="K")
        led.close()
        self.xero.org["period_lock_date"] = "2026-12-31"     # lock -> Hold
        s = self.run_once()
        self.assertTrue(any(h["stage"] == "lock" for h in s["held"]))
        row = state.Ledger(self.cfg.state_db).get("browns-garage-haywards-heath", "Apr-2026")
        self.assertEqual(row["status"], "posting")

    def test_definite_refusal_not_retried_until_something_changes(self):
        from msx.xero_client import XeroError
        self.set_mode("post")
        self.xero.fail_create = XeroError("400 bad code", status=400)
        s1 = self.run_once()
        self.assertEqual(len(s1["held"]), 2)
        n = len(self.xero.created)
        self.xero.fail_create = None
        s2 = self.run_once()                 # nothing changed: no new PUT
        self.assertEqual(len(self.xero.created), n)
        self.assertEqual(len(s2["failed"]), 2)
        self.set_mode("draft")               # mapping changed -> retried
        s3 = self.run_once()
        self.assertEqual(len(s3["draft"]), 2)

    def test_p30_syncing_is_pending_and_quarterly_is_informational(self):
        self.set_mode("post")
        p30 = os.path.join(self.folder(), "Browns Garage (Haywards Heath) Limited"
                           " - P30 Employer's Payslip for Apr-2026 to Jun-2026.txt")
        with open(p30, "w") as fh:
            fh.write("Employer's Payslip for Apr-2026 to Jun-2026\n"
                     "Tax & NIC due for Apr-2026 to Jun-2026   9,000.00\n")
        self.cfg.data["settle_seconds"] = 60
        s = self.run_once()                  # P30 mtime = now -> not stable
        self.assertTrue(any("P30 still syncing" in e["note"] for e in s["pending"]))
        self.assertNotIn("Apr-2026", [e["period"] for e in s["posted"]])
        os.utime(p30, (1_600_000_000, 1_600_000_000))
        s2 = self.run_once()
        apr = [e for e in s2["posted"] if e["period"] == "Apr-2026"][0]
        self.assertEqual(apr["p30"], "informational")
        # Jul-2026 (no P30 at all) posted in the first run with a warning
        self.assertTrue(any("without a P30" in w for w in s["warnings"]))

    def test_inactive_window_holds(self):
        p = os.path.join(self.clients, "browns-garage-haywards-heath.json")
        with open(p) as fh:
            cfg = json.load(fh)
        cfg["active_to"] = "Jun-2026"
        with open(p, "w") as fh:
            json.dump(cfg, fh)
        s = self.run_once()
        self.assertEqual([h["period"] for h in s["held"]], ["Jul-2026"])
        self.assertEqual([e["period"] for e in s["shadow"]], ["Apr-2026"])

    def test_csv_only_written_when_not_held(self):
        with open(os.path.join(self.clients, "browns-garage-haywards-heath.json")) as fh:
            cfg = json.load(fh)
        del cfg["employees"]["Sally Jones"]
        with open(os.path.join(self.clients, "browns-garage-haywards-heath.json"), "w") as fh:
            json.dump(cfg, fh)
        self.run_once()
        d = os.path.join(self.cfg.out_dir, "browns-garage-haywards-heath", "Apr-2026")
        self.assertFalse(os.path.exists(os.path.join(d, "journal.csv")))

    def test_run_level_bug_still_reports(self):
        with open(os.path.join(self.clients, "zzz.json"), "w") as fh:
            fh.write("[1, 2]")
        s = self.run_once()
        self.assertTrue(any(h["client"] == "(run)" for h in s["held"]))
        self.assertTrue(os.path.exists(os.path.join(self.cfg.out_dir, "HOLDS.md")))
        self.assertTrue(runner.system_problems(s))

    def test_unchanged_source_is_not_reparsed(self):
        self.set_mode("post")
        self.run_once()
        import msx.summary_parser as sp
        orig = sp.parse_files
        calls = []
        sp.parse_files = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
        try:
            self.run_once()
        finally:
            sp.parse_files = orig
        self.assertEqual(calls, [])


class AutoOnboardTests(unittest.TestCase):
    """A report from a client with no mapping maps itself from Xero."""

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
        old = 1_600_000_000
        for n in os.listdir(folder):
            os.utime(os.path.join(folder, n), (old, old))
        self.clients = os.path.join(root, "clients")
        os.makedirs(self.clients)                      # no mapping at all
        self.cfg = config_mod.Config({
            "pdf_root": self.pdf_root, "clients_dir": self.clients,
            "state_db": os.path.join(root, "state.sqlite"),
            "out_dir": os.path.join(root, "out"),
            "ledger_csv": os.path.join(root, "ledger.csv"),
            "settle_seconds": 0, "stability_sample_seconds": 0,
            "machine_name": "test-mac", "notify": {}}, path="test")
        self.xero = FakeXero()
        self.xero.tenant_list = [{"tenant_id": "T1", "type": "ORGANISATION",
                                  "name": "Browns Garage (Haywards Heath) Limited"}]
        self.xero.org["period_lock_date"] = None
        for code, name, cls in (("230", "Directors Remuneration", "EXPENSE"),
                                ("381", "Salaries", "EXPENSE"),
                                ("6000", "Productive Labour", "EXPENSE"),
                                ("471", "Dividends", "EQUITY"),
                                ("2200", "Wages Payable", "LIABILITY"),
                                ("2210", "PAYE Payable", "LIABILITY"),
                                ("2211", "Pensions Payable", "LIABILITY"),
                                ("6002", "Employers NIC", "EXPENSE"),
                                ("6001", "Pensions", "EXPENSE"),
                                ("836", "Directors Loan - Rob", "LIABILITY"),
                                ("837", "Directors Loan - Darren", "LIABILITY")):
            self.xero.chart[code] = {"name": name, "status": "ACTIVE",
                                     "class": cls, "system_account": None}
        self.xero.existing = [{"id": "J1", "narration": "Payroll - March 2026 (M12)",
                               "status": "POSTED", "date": "2026-03-31"}]
        self.lines = [
            {"Description": "Gross pay - Chris Jones - March 2026 (M12)", "AccountCode": "230", "LineAmount": 788.0},
            {"Description": "Gross pay - Sally Jones - March 2026 (M12)", "AccountCode": "381", "LineAmount": 702.0},
            {"Description": "Gross pay - Trevor Donohue - March 2026 (M12)", "AccountCode": "6000", "LineAmount": 3333.33},
            {"Description": "Employer NIC - Trevor Donohue - March 2026 (M12)", "AccountCode": "6002", "LineAmount": 437.45},
            {"Description": "Employer pension - Trevor Donohue - March 2026 (M12)", "AccountCode": "6001", "LineAmount": 84.40},
            {"Description": "Net wages - Chris Jones - March 2026 (M12)", "AccountCode": "2200", "LineAmount": -1153.88},
            {"Description": "PAYE/NIC payable - March 2026 (M12)", "AccountCode": "2210", "LineAmount": -5000.0},
            {"Description": "Pensions payable - Trevor Donohue (EE 112.54 / ER 84.40) - March 2026 (M12)", "AccountCode": "2211", "LineAmount": -196.94},
            {"Description": "Dividend - Robert Jones - March 2026 (M12)", "AccountCode": "471", "LineAmount": 4020.95},
            {"Description": "Dividend tax - Robert Jones - March 2026 (M12)", "AccountCode": "836", "LineAmount": -281.25},
        ]
        self.xero.full["J1"] = {"JournalLines": list(self.lines)}
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, **kw):
        return runner.run_once(self.cfg, xero_factory=lambda app: self.xero,
                               log=self.logs.append, notify_enabled=False, **kw)

    def mapping(self):
        names = [n for n in os.listdir(self.clients) if n.endswith(".json")]
        self.assertEqual(len(names), 1, names)
        with open(os.path.join(self.clients, names[0])) as fh:
            return json.load(fh)

    def test_gaps_write_a_shadow_mapping_and_hold(self):
        s = self.run_once()
        self.assertEqual(len(s["onboarded"]), 1)
        m = self.mapping()
        self.assertEqual(m["mode"], "shadow")
        self.assertEqual(m["codes"]["paye_payable"], "2210")
        self.assertEqual(m["employees"]["Simon Thompson"]["pay_code"], "TBC-pay-code")
        self.assertEqual([h["stage"] for h in s["held"]], ["mapping"])
        self.assertIn("Simon Thompson", s["held"][0]["note"])
        self.assertEqual(self.xero.created, [])
        # the next run finds the mapping file, not a second onboarding
        s2 = self.run_once()
        self.assertEqual(s2["onboarded"], [])

    def test_every_code_proven_goes_straight_to_a_draft(self):
        from msx import summary_parser
        pay = summary_parser.parse_files([os.path.join(FIX, "browns_apr2026_tabbed.txt")])
        for e in pay["employees"]:                    # history covers everyone
            n = e["name"]
            if n not in ("Chris Jones", "Sally Jones", "Trevor Donohue"):
                self.xero.full["J1"]["JournalLines"].append(
                    {"Description": f"Gross pay - {n} - March 2026 (M12)", "AccountCode": "381", "LineAmount": 500.0})
            if n != "Robert Jones":
                self.xero.full["J1"]["JournalLines"] += [
                    {"Description": f"Dividend - {n} - March 2026 (M12)", "AccountCode": "471", "LineAmount": 100.0},
                    {"Description": f"Dividend tax - {n} - March 2026 (M12)", "AccountCode": "837", "LineAmount": -50.0}]
        s = self.run_once()
        m = self.mapping()
        self.assertEqual(m["mode"], "draft", s["held"])
        self.assertIn("auto-onboard", m["approved_by"])
        self.assertEqual(len(s["onboarded"]), 1)
        self.assertEqual([h["stage"] for h in s["held"] if h["stage"] == "mapping"], [])

    def test_dry_run_and_disabled_do_not_write_a_mapping(self):
        s = self.run_once(dry_run=True)
        self.assertEqual(s["onboarded"], [])
        self.assertEqual([h["stage"] for h in s["held"]], ["mapping"])
        self.assertEqual(os.listdir(self.clients), [])
        self.cfg.data["auto_onboard"] = {"enabled": False}
        s = self.run_once()
        self.assertEqual(os.listdir(self.clients), [])

    def test_unmatched_org_holds_with_the_manual_command(self):
        self.xero.tenant_list = [{"tenant_id": "T9", "type": "ORGANISATION",
                                  "name": "Something Else Ltd"}]
        s = self.run_once()
        self.assertEqual([h["stage"] for h in s["held"]], ["mapping"])
        self.assertIn("msx onboard", s["held"][0]["note"])
        self.assertEqual(os.listdir(self.clients), [])
