"""Core parser + builder tests (unittest, no third-party dependencies).

    python3 -m unittest discover -s tests -v
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from msx import summary_parser, journal_builder  # noqa: E402
from msx.errors import Hold, Skip  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def load_mapping():
    with open(os.path.join(FIX, "browns_mapping_legacy.json")) as fh:
        cfg = json.load(fh)
    return cfg


class ParserTabbed(unittest.TestCase):
    def setUp(self):
        self.pay = summary_parser.parse_files(
            [os.path.join(FIX, "browns_apr2026_tabbed.txt")])

    def test_shape(self):
        self.assertEqual(len(self.pay["employees"]), 11)
        self.assertEqual(self.pay["period"], "Apr-2026")
        self.assertEqual(self.pay["tax_year"], "2026-27")
        self.assertEqual(self.pay["report_totals"]["total_payments"], 37255.47)
        self.assertEqual(self.pay["report_totals"]["dividend_tax"], 1656.28)
        self.assertEqual(self.pay["employer_totals"]["employment_allowance"],
                         -1946.01)
        self.assertEqual(self.pay["employer_totals"]["total_tax_nic_due"],
                         3311.93)
        self.assertEqual(self.pay["employer_totals"]["hmrc_payment_for_period"],
                         3251.20)
        self.assertEqual(sorted(self.pay["layouts_seen"]),
                         ["additions", "deductions", "medium"])


class ParserFixedWidth(unittest.TestCase):
    def setUp(self):
        self.pay = summary_parser.parse_files(
            [os.path.join(FIX, "browns_jul2026_layout.txt")])

    def test_shape(self):
        names = [e["name"] for e in self.pay["employees"]]
        self.assertEqual(len(names), 11)
        self.assertIn("Ylva Alexandersson", names)   # split name merged
        self.assertEqual(self.pay["period"], "Jul-2026")
        self.assertEqual(self.pay["report_totals"]["ee_pension"], 500.83)
        self.assertEqual(self.pay["employer_totals"]["total_tax_nic_due"],
                         3268.13)
        self.assertEqual(self.pay["employer_totals"]["paye_tax"], 2494.00)

    def test_unknown_column_holds(self):
        with open(os.path.join(FIX, "browns_jul2026_layout.txt")) as fh:
            text = fh.read()
        bad = text.replace("Attachments", "Widgets", 1)
        with self.assertRaises(Hold):
            summary_parser.parse_text(bad)


class Builder(unittest.TestCase):
    def setUp(self):
        self.pay = summary_parser.parse_files(
            [os.path.join(FIX, "browns_apr2026_tabbed.txt")])
        self.cfg = load_mapping()

    def test_clean_build(self):
        j = journal_builder.build(self.pay, self.cfg)
        self.assertEqual(len(j.lines), 55)
        self.assertEqual(j.balance, 0)
        self.assertEqual(f"{j.total_debits:.2f}", "41463.73")
        paye = sum(l.amount for l in j.lines if l.account_code == "2210")
        self.assertEqual(f"{paye:.2f}", "-3311.93")
        self.assertEqual(sum("PAYE/NIC payable" in l.description
                             for l in j.lines), 1)
        self.assertEqual(sum("Employment allowance" in l.description
                             for l in j.lines), 2)
        pens = [l for l in j.lines if l.account_code == "2211"]
        self.assertEqual(len(pens), 7)
        self.assertEqual(f"{sum(l.amount for l in pens):.2f}", "-737.87")
        self.assertTrue(all("(EE " in l.description and " / ER " in l.description
                            for l in pens))
        self.assertEqual(j.date, "30/04/2026")
        self.assertEqual(j.narration, "Payroll - April 2026 (M1)")
        self.assertEqual(j.meta["tax_year"], "2026-27")

    def test_csv_matches_xero_template(self):
        j = journal_builder.build(self.pay, self.cfg)
        rows = j.to_csv().strip().splitlines()
        self.assertEqual(rows[0], ",".join(journal_builder.CSV_HEADERS))
        self.assertEqual(len(rows) - 1, 55)
        self.assertTrue(all(r.split(",")[4] == "No VAT" for r in rows[1:]))
        self.assertTrue(all(r.split(",")[1] == "30/04/2026" for r in rows[1:]))

    def test_api_payload(self):
        j = journal_builder.build(self.pay, self.cfg)
        body = j.to_api_payload(status="DRAFT")
        self.assertEqual(body["Date"], "2026-04-30")
        self.assertEqual(body["LineAmountTypes"], "NoTax")
        self.assertEqual(len(body["JournalLines"]), 55)
        self.assertAlmostEqual(sum(l["LineAmount"] for l in body["JournalLines"]),
                               0.0, places=2)
        self.assertTrue(all(l["TaxType"] == "NONE" for l in body["JournalLines"]))

    def test_tampered_figures_hold(self):
        for mutate in (
            lambda p: p["employees"][2].__setitem__("net", 9999.0),
            lambda p: p["employees"][2].__setitem__("er_nic", 400.0),
            lambda p: p["employer_totals"].pop("employment_allowance"),
            lambda p: p["employer_totals"].__setitem__("total_tax_nic_due", 1.0),
            lambda p: p["employees"].append(
                {"name": "New Starter", "total_payments": 100.0, "basic": 100.0,
                 "net": 100.0}),
        ):
            bad = copy.deepcopy(self.pay)
            mutate(bad)
            with self.assertRaises(Hold):
                journal_builder.build(bad, self.cfg)

    def test_placeholder_codes(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["codes"]["er_pension_cost"] = "TBC-ER-PENSION"
        with self.assertRaises(Hold):
            journal_builder.build(self.pay, cfg)
        j = journal_builder.build(self.pay, cfg, allow_placeholders=True)
        self.assertTrue(j.meta["placeholders"])

    def test_nil_payroll_skips(self):
        nil = {"client": "X Ltd", "period": "May-2026", "employees": [],
               "report_totals": {}, "employer_totals": {}}
        with self.assertRaises(Skip):
            journal_builder.build(nil, self.cfg)

    def test_fixed_width_build(self):
        pay = summary_parser.parse_files(
            [os.path.join(FIX, "browns_jul2026_layout.txt")])
        j = journal_builder.build(pay, self.cfg)
        self.assertEqual(len(j.lines), 57)          # Susannah joined the scheme
        paye = sum(l.amount for l in j.lines if l.account_code == "2210")
        self.assertEqual(f"{paye:.2f}", "-3268.13")
        self.assertEqual(sum(1 for l in j.lines if l.account_code == "2211"), 8)

    def test_attachments_separate_creditor(self):
        pay = copy.deepcopy(self.pay)
        # give one employee a 50.00 attachment and rebalance their net pay
        e = pay["employees"][2]
        e["attachments"] = 50.0
        e["net"] = round(e["net"] - 50.0, 2)
        pay["report_totals"]["attachments"] = 50.0
        pay["report_totals"]["net"] = round(pay["report_totals"]["net"] - 50.0, 2)
        pay["employer_totals"]["total_net_pay"] = round(
            pay["employer_totals"]["total_net_pay"] - 50.0, 2)
        # The attachment is owed to the court, not HMRC: Total Tax & NIC Due
        # is unchanged. Without a creditor code the build must HOLD; with
        # one it builds, balances, and the PAYE line still ties to the P30.
        pay["employer_totals"]["total_net_outlay"] = round(
            pay["employer_totals"]["total_net_outlay"] - 50.0, 2)
        with self.assertRaises(Hold) as cm:
            journal_builder.build(pay, self.cfg)
        self.assertIn("attachments_payable", str(cm.exception))
        cfg = copy.deepcopy(self.cfg)
        cfg["codes"]["attachments_payable"] = "2215"
        j = journal_builder.build(pay, cfg)
        att = [l for l in j.lines if l.account_code == "2215"]
        self.assertEqual(len(att), 1)
        self.assertEqual(f"{att[0].amount:.2f}", "-50.00")
        self.assertEqual(j.balance, 0)
        paye = sum(l.amount for l in j.lines if l.account_code == "2210")
        self.assertEqual(f"{paye:.2f}", "-3311.93")
        # the outlay presentation that counts the attachment is also accepted
        pay["employer_totals"]["total_net_outlay"] = round(
            pay["employer_totals"]["total_net_outlay"] + 50.0, 2)
        j2 = journal_builder.build(pay, cfg)
        self.assertEqual(j2.meta["outlay_convention"],
                         "net+hmrc+pensions+attachments")


if __name__ == "__main__":
    unittest.main(verbosity=2)
