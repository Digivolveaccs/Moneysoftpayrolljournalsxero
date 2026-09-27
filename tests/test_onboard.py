"""Mapping proposal from the client's own Xero history."""
import datetime
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from msx import onboard, summary_parser, mapping  # noqa: E402
from test_poster import FakeXero  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class OnboardTests(unittest.TestCase):
    def setUp(self):
        self.pay = summary_parser.parse_files(
            [os.path.join(FIX, "browns_apr2026_tabbed.txt")])
        self.xero = FakeXero()
        self.xero.tenant_list = [{"tenant_id": "T1",
                                  "name": "Browns Garage (Haywards Heath) Limited",
                                  "type": "ORGANISATION"}]
        for code, name, cls in (("230", "Directors Remuneration", "EXPENSE"),
                                ("381", "Salaries", "EXPENSE"),
                                ("6000", "Productive Labour", "EXPENSE"),
                                ("471", "Dividends", "EQUITY"),
                                ("2200", "Wages Payable", "LIABILITY"),
                                ("2210", "PAYE Payable", "LIABILITY"),
                                ("2211", "Pensions Payable", "LIABILITY"),
                                ("6002", "Employers NIC", "EXPENSE"),
                                ("6001", "Pensions", "EXPENSE"),
                                ("836", "Directors Loan - Rob", "LIABILITY")):
            self.xero.chart[code] = {"name": name, "status": "ACTIVE",
                                     "class": cls, "system_account": None}
        self.xero.existing = [
            {"id": "J1", "narration": "Payroll - March 2026 (M12)",
             "status": "POSTED", "date": "2026-03-31"},
            {"id": "D1", "narration": "Depreciation March 2026",
             "status": "POSTED", "date": "2026-03-31"}]
        self.xero.full["J1"] = {"JournalLines": [
            {"Description": "Gross pay - Chris Jones - March 2026 (M12)", "AccountCode": "230", "LineAmount": 788.0},
            {"Description": "Gross pay - Sally Jones - March 2026 (M12)", "AccountCode": "381", "LineAmount": 702.0},
            {"Description": "Gross pay - Trevor Donohue - March 2026 (M12)", "AccountCode": "6000", "LineAmount": 3333.33},
            {"Description": "Dividend - Robert Jones - March 2026 (M12)", "AccountCode": "471", "LineAmount": 4020.95},
            {"Description": "Employer NIC - Trevor Donohue - March 2026 (M12)", "AccountCode": "6002", "LineAmount": 437.45},
            {"Description": "Employer pension - Trevor Donohue - March 2026 (M12)", "AccountCode": "6001", "LineAmount": 84.40},
            {"Description": "Net wages - Chris Jones - March 2026 (M12)", "AccountCode": "2200", "LineAmount": -1153.88},
            {"Description": "Dividend tax - Robert Jones - March 2026 (M12)", "AccountCode": "836", "LineAmount": -281.25},
            {"Description": "PAYE/NIC payable - March 2026 (M12)", "AccountCode": "2210", "LineAmount": -5000.0},
            {"Description": "Pensions payable - Trevor Donohue (EE 112.54 / ER 84.40) - March 2026 (M12)", "AccountCode": "2211", "LineAmount": -196.94},
        ]}

    def test_propose_from_history(self):
        stub = onboard.stub_for(self.pay)
        m, report = onboard.propose(self.pay, self.xero, stub,
                                    today=datetime.date(2026, 5, 1))
        self.assertEqual(m["mode"], "shadow")
        self.assertEqual(m["xero"]["tenant_id"], "T1")
        self.assertEqual(m["codes"]["wages_payable"], "2200")
        self.assertEqual(m["codes"]["paye_payable"], "2210")
        self.assertEqual(m["codes"]["er_nic_cost"], "6002")
        self.assertEqual(m["codes"]["er_pension_cost"], "6001")
        self.assertEqual(m["codes"]["pensions_payable"], "2211")
        self.assertEqual(m["codes"]["dividend_code"], "471")
        self.assertEqual(m["employees"]["Chris Jones"]["pay_code"], "230")
        self.assertEqual(m["employees"]["Sally Jones"]["pay_code"], "381")
        self.assertEqual(m["employees"]["Trevor Donohue"]["pay_code"], "6000")
        self.assertEqual(m["employees"]["Robert Jones"]["dividend_tax_code"], "836")
        # people with no evidence get placeholders, never a guess
        self.assertEqual(m["employees"]["Simon Thompson"]["pay_code"], "TBC-pay-code")
        self.assertEqual(m["employees"]["Darren Jones"]["dividend_tax_code"],
                         "TBC-directors-loan")
        self.assertIn("Payroll - March 2026 (M12)", m["source"])
        self.assertTrue(report["placeholders"])
        # the proposal is a syntactically valid mapping
        self.assertEqual(mapping.Mapping(m).problems, [])
        with tempfile.TemporaryDirectory() as d:
            path = onboard.write_mapping(m, d)
            self.assertTrue(path.endswith("browns-garage-haywards-heath.json"))
            with self.assertRaises(FileExistsError):
                onboard.write_mapping(m, d)

    def test_pensions_payable_account_is_not_taken_as_the_cost(self):
        self.xero.chart["858"] = {"name": "Pensions Payable", "status": "ACTIVE",
                                  "class": "LIABILITY", "system_account": None}
        self.xero.full["J1"]["JournalLines"] = [
            {"Description": "T Donohue", "AccountCode": "858", "LineAmount": -196.94},
            {"Description": "T Donohue", "AccountCode": "6000", "LineAmount": 3333.33},
        ]
        m, _ = onboard.propose(self.pay, self.xero, onboard.stub_for(self.pay),
                               today=datetime.date(2026, 5, 1))
        self.assertEqual(m["codes"]["pensions_payable"], "858")
        self.assertEqual(m["codes"]["er_pension_cost"], "TBC-employer-pension")

    def test_no_history_all_placeholders(self):
        self.xero.existing = []
        m, report = onboard.propose(self.pay, self.xero, onboard.stub_for(self.pay),
                                    today=datetime.date(2026, 5, 1))
        self.assertTrue(all(str(v).startswith("TBC") for v in m["codes"].values()))
        self.assertIn("no wages journals found", m["source"])

    def test_brightpay_style_lines_use_account_names(self):
        self.xero.existing = [{"id": "B1", "narration": "Sent from BrightPay",
                               "status": "POSTED", "date": "2026-03-31"}]
        self.xero.full["B1"] = {"JournalLines": [
            {"Description": "Chris Jones", "AccountCode": "230", "LineAmount": 788.0},
            {"Description": "Chris Jones", "AccountCode": "2200", "LineAmount": -600.0},
            {"Description": "Chris Jones", "AccountCode": "2210", "LineAmount": -188.0},
        ]}
        m, report = onboard.propose(self.pay, self.xero, onboard.stub_for(self.pay),
                                    today=datetime.date(2026, 5, 1))
        self.assertEqual(m["employees"]["Chris Jones"]["pay_code"], "230")
        self.assertEqual(m["codes"]["wages_payable"], "2200")
        self.assertEqual(m["codes"]["paye_payable"], "2210")


if __name__ == "__main__":
    unittest.main(verbosity=2)
