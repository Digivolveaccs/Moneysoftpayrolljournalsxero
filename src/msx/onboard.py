"""Propose a client mapping from the client's OWN history in Xero.

The single biggest source of wrong journals is a guessed nominal code.
The practice rule is: copy the codes from the client's last posted wages
journal. This module automates that copy for the ~260 clients:

1. Parse the client's latest Employer's Summary (employee names, whether
   dividends / pensions / attachments appear).
2. In the client's Xero org, list manual journals for the last N months and
   keep the wages-looking ones (narration matches WAGES_RE).
3. Fetch each journal's lines and infer, per line, which employee and which
   kind of amount it is (gross / net / employer NIC / PAYE / pension ...),
   using the description text and, where the description is silent, the
   account's name and class.
4. Write ``clients/<slug>.json`` in mode ``shadow`` with every code that
   could be inferred filled in, everything else as ``TBC-...``, and a
   ``source`` note naming the journal(s) the codes came from.

The proposal is a starting point for a human: nothing is posted until the
mapping has no placeholders and mode is changed by a person.
"""
import datetime
import json
import os
import re

from .mapping import slugify
from .poster import WAGES_RE, month_window, resolve_tenant

KIND_PATTERNS = [
    ("er_nic_cost", re.compile(r"employer'?s? ?(nic|ni|national insurance)|"
                               r"\bers? nic\b|employer ni", re.I)),
    ("er_pension_cost", re.compile(r"employer'?s? ?pension|\ber pension|"
                                   r"pension (cost|contribution)", re.I)),
    ("pensions_payable", re.compile(r"pensions? payable|nest|pension liab|"
                                    r"pension (control|creditor)", re.I)),
    ("paye_payable", re.compile(r"paye|hmrc|tax (&|and) ni|nic payable|"
                                r"income tax", re.I)),
    ("wages_payable", re.compile(r"net (pay|wages)|wages payable|"
                                 r"net salar", re.I)),
    ("dividend_code", re.compile(r"dividend(?! tax)", re.I)),
    ("dividend_tax_code", re.compile(r"dividend tax", re.I)),
    ("attachments_payable", re.compile(r"attachment|earnings order|dea\b|"
                                       r"court order", re.I)),
    ("pay_code", re.compile(r"gross|salar|wage|remuneration|basic pay|"
                            r"directors? pay", re.I)),
]

ACCOUNT_NAME_HINTS = [                       # most specific first
    ("er_nic_cost", re.compile(r"employer.*(ni|nic|national insurance)", re.I)),
    ("pensions_payable", re.compile(r"pension.*(payable|liab|control)", re.I)),
    ("er_pension_cost", re.compile(r"pension", re.I)),
    ("paye_payable", re.compile(r"paye|hmrc", re.I)),
    ("wages_payable", re.compile(r"wages? payable|payroll", re.I)),
    ("dividend_code", re.compile(r"dividend", re.I)),
    ("pay_code", re.compile(r"salar|wage|remuneration", re.I)),
]


def name_tokens(name):
    return [t for t in re.split(r"[^a-z]+", name.lower()) if t]


def match_employee(description, employees):
    """Employee whose full name, or initial+surname, appears in the text."""
    d = description.lower()
    for name in employees:
        if name.lower() in d:
            return name
    for name in employees:
        toks = name_tokens(name)
        if len(toks) >= 2:
            init_sur = f"{toks[0][0]} {toks[-1]}"
            if init_sur in d or (toks[-1] in d and toks[0][0] + "." in d):
                return name
    return None


def classify_line(line, accounts, employees):
    desc = line.get("Description") or ""
    code = str(line.get("AccountCode") or "")
    amt = float(line.get("LineAmount") or 0)
    who = match_employee(desc, employees)
    kind = None
    for k, rx in KIND_PATTERNS:
        if rx.search(desc):
            kind = k
            break
    if kind is None:
        acct = accounts.get(code) or {}
        name = acct.get("name") or ""
        cls = (acct.get("class") or "").upper()
        for k, rx in ACCOUNT_NAME_HINTS:
            if rx.search(name):
                kind = k
                break
        if kind is None and cls == "EXPENSE" and amt > 0:
            kind = "pay_code"
        elif kind is None and cls == "LIABILITY" and amt < 0:
            kind = "wages_payable" if who else "paye_payable"
    # sign sanity: costs are debits, payables credits; and a "cost" on a
    # liability account (or a payable on an expense account) is not evidence
    acct_cls = ((accounts.get(code) or {}).get("class") or "").upper()
    if kind in ("wages_payable", "paye_payable", "pensions_payable",
                "attachments_payable", "dividend_tax_code") and amt > 0 \
            and "allowance" not in desc.lower():
        kind = None
    if kind in ("pay_code", "er_nic_cost", "er_pension_cost") and amt < 0 \
            and "allowance" not in desc.lower():
        kind = None
    if kind in ("er_nic_cost", "er_pension_cost", "pay_code") \
            and acct_cls in ("LIABILITY", "ASSET", "REVENUE"):
        kind = None
    if kind in ("paye_payable", "pensions_payable", "attachments_payable") \
            and acct_cls in ("EXPENSE", "REVENUE"):
        kind = None
    return {"code": code, "kind": kind, "employee": who, "amount": amt,
            "description": desc}


def wages_journals(xero, tenant_id, months=6, today=None):
    today = today or datetime.date.today()
    start = (today.replace(day=1) - datetime.timedelta(days=31 * months)).replace(day=1)
    rows = xero.find_manual_journals(tenant_id, start.isoformat(),
                                     today.isoformat())
    return [r for r in rows if WAGES_RE.search(r["narration"] or "")
            and r["status"] in ("POSTED", "DRAFT")]


def propose(payroll, xero, mapping_stub, *, months=6, today=None):
    """-> (mapping dict, report dict). Never raises on inference gaps."""
    employees = [e["name"] for e in payroll.get("employees", [])]
    tenants = xero.tenants()
    tenant = resolve_tenant(_Stub(mapping_stub), tenants)
    tid = tenant["tenant_id"]
    accounts = xero.accounts(tid)
    journals = wages_journals(xero, tid, months=months, today=today)
    codes = {}
    emp_codes = {n: {} for n in employees}
    evidence = []
    unresolved = []
    for j in journals[-3:]:
        full = xero.manual_journal(tid, j["id"]) or {}
        for line in full.get("JournalLines", []) or []:
            c = classify_line(line, accounts, employees)
            if not c["kind"]:
                unresolved.append(c)
                continue
            if c["kind"] in ("pay_code", "dividend_code", "dividend_tax_code") \
                    and c["employee"]:
                emp_codes[c["employee"]].setdefault(c["kind"], c["code"])
                if c["kind"] == "dividend_code":
                    codes.setdefault("dividend_code", c["code"])
            elif c["kind"] == "pay_code" and not c["employee"]:
                codes.setdefault("_pay_code_default", c["code"])
            else:
                codes.setdefault(c["kind"], c["code"])
            evidence.append(f"{j['narration']} ({j['date']}): {c['kind']} "
                            f"<- {c['code']} '{c['description'][:60]}'")
    default_pay = codes.pop("_pay_code_default", None)
    mapping = json.loads(json.dumps(mapping_stub))
    mapping.setdefault("xero", {})["tenant_id"] = tid
    mapping["xero"]["org_name"] = tenant["name"]
    mapping["mode"] = "shadow"
    mapping.setdefault("journal_date", "month_end")
    mapping.setdefault("narration", "Payroll - {tag}")
    mapping.setdefault("tax_rate", "No VAT")
    mapping["codes"] = {
        "wages_payable": codes.get("wages_payable", "TBC-wages-payable"),
        "paye_payable": codes.get("paye_payable", "TBC-paye-payable"),
        "er_nic_cost": codes.get("er_nic_cost", "TBC-employer-nic"),
    }
    totals = payroll.get("report_totals", {})
    if abs(totals.get("ee_pension", 0)) + abs(totals.get("er_pension", 0)) > 0.005:
        mapping["codes"]["pensions_payable"] = codes.get("pensions_payable",
                                                         "TBC-pensions-payable")
        mapping["codes"]["er_pension_cost"] = codes.get("er_pension_cost",
                                                        "TBC-employer-pension")
    if abs(totals.get("dividend", 0)) > 0.005:
        mapping["codes"]["dividend_code"] = codes.get("dividend_code",
                                                      "TBC-dividends")
    if abs(totals.get("attachments", 0)) > 0.005:
        mapping["codes"]["attachments_payable"] = codes.get(
            "attachments_payable", "TBC-attachments-payable")
    mapping["employees"] = {}
    for e in payroll.get("employees", []):
        rec = {"pay_code": emp_codes[e["name"]].get("pay_code")
               or default_pay or "TBC-pay-code"}
        own_div = emp_codes[e["name"]].get("dividend_code")
        if abs(e.get("dividend", 0)) > 0.005 and own_div \
                and own_div != mapping["codes"].get("dividend_code"):
            rec["dividend_code"] = own_div
        if abs(e.get("dividend_tax", 0)) > 0.005:
            rec["dividend_tax_code"] = emp_codes[e["name"]].get(
                "dividend_tax_code", "TBC-directors-loan")
        mapping["employees"][e["name"]] = rec
    src = ("Proposed by msx onboard on "
           f"{(today or datetime.date.today()).isoformat()} from "
           + (", ".join(f"'{j['narration']}' ({j['date']}, {j['status']})"
                        for j in journals[-3:]) or "no wages journals found "
              "in the last %d months - every code is a placeholder" % months))
    mapping["source"] = src
    mapping["approved_by"] = ""
    mapping["approved_on"] = ""
    report = {"tenant": tenant, "journals_seen": len(journals),
              "evidence": evidence, "unresolved_lines": unresolved,
              "placeholders": [k for k, v in mapping["codes"].items()
                               if str(v).startswith("TBC")]
              + [f"{n}.{k}" for n, r in mapping["employees"].items()
                 for k, v in r.items() if str(v).startswith("TBC")]}
    return mapping, report


class _Stub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = cfg.get("client", "?")


def stub_for(payroll, org_name=None):
    client = payroll.get("client") or "Client"
    return {"client": client, "slug": slugify(client),
            "moneysoft_employer": client,
            "xero": {"org_name": org_name or client, "tenant_id": "",
                     "app": "default"}}


def write_mapping(mapping, clients_dir, *, overwrite=False):
    path = os.path.join(clients_dir, mapping["slug"] + ".json")
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path
