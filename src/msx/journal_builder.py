"""Build the Digivolve house-shape wages journal from a parsed payroll dict.

Proven lineage: the builder from Digivolve's browns-payroll-journal skill,
converted to an importable module. Returns a ``Journal`` or raises ``Hold``.

Journal shape (fixed - do not vary)::

    Dr  gross pay          one NAMED line per employee (basic + hourly + any
                           holiday/sick/parenting/rounding additions)
    Dr  dividends          one NAMED line per employee taking a dividend
    Dr  Employer NIC       one NAMED line per employee (full cost, pre-EA)
    Dr  Employer pension   one NAMED line per employee
    Cr  net wages          one NAMED line per employee
    Cr  dividend tax etc.  one NAMED line per employee, to their DLA
    Cr  PAYE payable       ONE line: PAYE tax + EE NIC + ER NIC (pre-EA)
                           + student/postgrad loan + attachments*
    Cr  pensions payable   one NAMED line per employee (EE + ER, split shown)
    Dr  PAYE payable /     the Employment Allowance, shown explicitly as its
    Cr  Employer NIC       own two-line pair - never silently netted

    * attachments of earnings go to ``attachments_payable`` when the mapping
      defines it (they are paid to the court/DWP, not HMRC); otherwise they
      stay inside the PAYE line exactly as the legacy builder did, so the
      control account still ties to the report's Total Tax & NIC Due.

Nothing is returned unless every one of these holds, to the penny:
  * every employee's own figures re-derive their net pay
  * every addition and deduction is explained by a named component
  * the per-employee figures sum to the report's Total row
  * PAYE tax + EE NIC + ER NIC (+SL/PGL/AoE) - EA = Total Tax & NIC Due
  * EE + ER pension = Total Other Payments
  * net pay + tax/NIC due + pensions = TOTAL NET OUTLAY
  * the journal balances to 0.00
Any failure raises Hold listing every problem found. Never edit a figure.
"""
import calendar
import csv
import datetime
import io
import json
import re
from decimal import Decimal

from .errors import Hold, Skip

CSV_HEADERS = ["*Narration", "*Date", "Description", "*AccountCode",
               "*TaxRate", "*Amount", "TrackingName1", "TrackingOption1",
               "TrackingName2", "TrackingOption2"]

MAX_LINES = 300
TOL = Decimal("0.005")
ZERO = Decimal("0.00")

# additions that make up "Total Additions", and the label used on the line
ADDITIONS = [("dividend", "Dividend"), ("holiday", "Holiday pay"),
             ("sick", "Sick pay"), ("parenting", "Parenting pay"),
             ("rounding_addition", "Rounding")]
# deductions that make up "Total Deductions" (pension/SL/PGL/attachments are
# reported in their own columns and handled separately)
DEDUCTIONS = [("dividend_tax", "Dividend tax"), ("overpayment", "Overpayment"),
              ("payroll_giving", "Payroll giving"), ("childcare", "Childcare"),
              ("loans_repayment", "Loan repayment"),
              ("rounding_deduction", "Rounding")]

SUMMED = ["total_payments", "basic", "hourly", "additions", "total_deductions",
          "ee_pension", "student_loan", "postgrad_loan", "attachments",
          "ee_nic", "tax", "net", "er_nic", "er_pension"]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def D(value):
    return Decimal(str(round(float(value or 0.0), 2))).quantize(Decimal("0.01"))


def get(rec, key):
    return D(rec.get(key, 0.0))


def period_parts(period):
    """'Apr-2026' -> ('April 2026', 1, '30/04/2026', date(2026,4,30))."""
    dt = datetime.datetime.strptime(period, "%b-%Y")
    month_name = dt.strftime("%B %Y")
    tax_month = (dt.month - 4) % 12 + 1
    last = calendar.monthrange(dt.year, dt.month)[1]
    end = datetime.date(dt.year, dt.month, last)
    return month_name, tax_month, end.strftime("%d/%m/%Y"), end


def tax_year_of(period):
    """'Apr-2026' -> '2026-27'; 'Jan-2027' -> '2026-27'."""
    dt = datetime.datetime.strptime(period, "%b-%Y")
    start = dt.year if dt.month >= 4 else dt.year - 1
    return f"{start}-{str(start + 1)[2:]}"


class Line:
    __slots__ = ("description", "account_code", "amount", "kind", "employee")

    def __init__(self, description, account_code, amount, kind, employee=None):
        self.description = description
        self.account_code = str(account_code) if account_code is not None else None
        self.amount = amount          # Decimal, positive = debit
        self.kind = kind              # gross / dividend / er_nic / ...
        self.employee = employee

    def as_dict(self):
        return {"description": self.description,
                "account_code": self.account_code,
                "amount": f"{self.amount:.2f}", "kind": self.kind,
                "employee": self.employee}


class Journal:
    def __init__(self, *, client, period, narration, date, lines, meta):
        self.client = client
        self.period = period
        self.narration = narration
        self.date = date              # 'dd/mm/yyyy'
        self.lines = lines
        self.meta = meta

    @property
    def total_debits(self):
        return sum((l.amount for l in self.lines if l.amount > 0), ZERO)

    @property
    def balance(self):
        return sum((l.amount for l in self.lines), ZERO)

    def iso_date(self):
        d = datetime.datetime.strptime(self.date, "%d/%m/%Y").date()
        return d.isoformat()

    def to_csv(self, tax_rate="No VAT"):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(CSV_HEADERS)
        for l in self.lines:
            w.writerow([self.narration, self.date, l.description,
                        l.account_code, tax_rate, f"{l.amount:.2f}",
                        "", "", "", ""])
        return buf.getvalue()

    def to_api_payload(self, *, status="DRAFT", tax_type="NONE",
                       show_on_cash_basis=None, url=None):
        if show_on_cash_basis is None:
            show_on_cash_basis = bool(self.meta.get("show_on_cash_basis", False))
        """Xero Accounting API ManualJournals body (positive = debit)."""
        body = {
            "Narration": self.narration,
            "Date": self.iso_date(),
            "Status": status,
            "LineAmountTypes": "NoTax",
            "ShowOnCashBasisReports": bool(show_on_cash_basis),
            "JournalLines": [
                {"LineAmount": float(l.amount),
                 "AccountCode": l.account_code,
                 "Description": l.description,
                 "TaxType": tax_type}
                for l in self.lines
            ],
        }
        if url:
            body["Url"] = url
        return body

    def figures_fingerprint(self):
        """SHA-256 of what the journal *is* (date, narration, lines) -
        independent of DRAFT/POSTED status, so a mode change is never
        mistaken for a payroll re-run and a re-run is never hidden by one."""
        import hashlib
        canon = json.dumps({"d": self.iso_date(), "n": self.narration,
                            "l": [[l.account_code, f"{l.amount:.2f}",
                                   l.description] for l in self.lines]},
                           sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def summary(self):
        m = self.meta
        return {
            "client": self.client, "period": self.period,
            "narration": self.narration, "date": self.date,
            "lines": len(self.lines),
            "total_debits": f"{self.total_debits:.2f}",
            "gross": f"{m['gross']:.2f}", "dividends": f"{m['dividends']:.2f}",
            "er_nic": f"{m['er_nic']:.2f}",
            "paye_pre_ea": f"{m['paye_pre_ea']:.2f}",
            "employment_allowance": f"{m['ea']:.2f}",
            "paye_due": f"{m['paye_due']:.2f}",
            "pensions": f"{m['pensions']:.2f}",
            "net_wages": f"{m['net']:.2f}",
            "employees": m["employees"],
        }

    def as_dict(self):
        return {"client": self.client, "period": self.period,
                "narration": self.narration, "date": self.date,
                "lines": [l.as_dict() for l in self.lines],
                "summary": self.summary()}


DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def resolve_journal_date(cfg, month_end, *, date_override=None, pay_date=None,
                         holds):
    """The mapping's journal_date is a RULE: 'month_end' (default) or
    'pay_date' (needs the period's pay date, dd/mm/yyyy or dd-Mon-yyyy).
    A literal dd/mm/yyyy in the mapping is honoured for compatibility.
    ``date_override`` (the --date flag) always wins."""
    if date_override:
        if not DATE_RE.match(date_override):
            holds.append(f"--date '{date_override}' must be dd/mm/yyyy")
        return date_override
    rule = (cfg.get("journal_date") or "month_end").strip()
    if rule == "month_end":
        return month_end
    if rule == "pay_date":
        if not pay_date:
            holds.append("mapping says journal_date=pay_date but no pay date "
                         "is available from the report - use month_end or "
                         "pass --date")
            return month_end
        for fmt in ("%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(pay_date, fmt).strftime(
                    "%d/%m/%Y")
            except ValueError:
                continue
        holds.append(f"pay date '{pay_date}' is not a recognised date")
        return month_end
    if DATE_RE.match(rule):
        return rule
    holds.append(f"journal_date '{rule}' must be month_end, pay_date or "
                 "dd/mm/yyyy")
    return month_end


def expand_code(code, tax_year):
    """Tax-year templates in codes: '821-{yy}-{yy1}' -> '821-26-27' for
    2026-27. Plain codes pass through unchanged."""
    if not isinstance(code, str) or "{" not in code:
        return code
    start, end = tax_year.split("-")
    return code.format(yy=start[2:], yy1=end, yyyy=start, yyyy1=str(int(start) + 1))


def _code_for(cfg, emp_cfg, key, who, label, holds):
    code = emp_cfg.get(key) or cfg.get("codes", {}).get(key)
    code = expand_code(code, cfg.get("_tax_year") or "0000-00")
    if not code:
        holds.append(f"no {key} account code for {who} ({label}) - add it to "
                     "the client mapping file")
        return None
    if str(code).upper().startswith("TBC"):
        holds.append(f"placeholder account code '{code}' still in the mapping "
                     f"for {who} ({label}) - replace it with the real Xero code")
    return str(code)


def is_nil(payroll):
    """True when the report shows nothing paid at all (a nil month)."""
    emps = payroll.get("employees", [])
    totals = payroll.get("report_totals", {})
    if not emps and not totals:
        return True
    keys = ("total_payments", "net", "tax", "ee_nic", "er_nic",
            "ee_pension", "er_pension")
    if all(abs(D(totals.get(k, 0.0))) < TOL for k in keys):
        return all(all(abs(get(e, k)) < TOL for k in keys) for e in emps)
    return False


def build(payroll, cfg, *, date_override=None, allow_placeholders=False):
    """payroll dict + client mapping -> Journal. Raises Hold / Skip."""
    holds = []
    codes = cfg.get("codes", {})
    emps = payroll.get("employees", [])
    totals = payroll.get("report_totals", {})
    employer = payroll.get("employer_totals", {})

    if is_nil(payroll):
        raise Skip(f"nil payroll for {payroll.get('client')} "
                   f"{payroll.get('period')} - no journal, no client email",
                   reason="nil")
    cfg = dict(cfg)
    cfg["_tax_year"] = tax_year_of(payroll["period"])
    codes = cfg.get("codes", {})
    warnings = []

    month_name, tax_month, month_end, _ = period_parts(payroll["period"])
    tag = f"{month_name} (M{tax_month})"
    date = resolve_journal_date(cfg, month_end, date_override=date_override,
                                pay_date=payroll.get("pay_date"), holds=holds)
    narration = cfg.get("narration", "Payroll - {tag}").format(
        tag=tag, month=month_name, period=payroll["period"],
        client=payroll.get("client", ""))

    # ---------- per-employee integrity ----------
    for e in emps:
        who = e["name"]
        gross = get(e, "basic") + get(e, "hourly") + get(e, "additions")
        if abs(gross - get(e, "total_payments")) > TOL:
            holds.append(f"{who}: basic+hourly+additions {gross} != total "
                         f"payments {get(e, 'total_payments')}")
        add_named = sum((get(e, k) for k, _ in ADDITIONS), ZERO)
        if abs(add_named - get(e, "additions")) > TOL:
            holds.append(f"{who}: additions {get(e, 'additions')} not "
                         f"explained by named components ({add_named}) - "
                         "supply the Additions layout, or add the missing "
                         "column")
        ded_named = sum((get(e, k) for k, _ in DEDUCTIONS), ZERO)
        if abs(ded_named - get(e, "total_deductions")) > TOL:
            holds.append(f"{who}: deductions {get(e, 'total_deductions')} not "
                         f"explained by named components ({ded_named}) - "
                         "supply the Deductions layout, or add the missing "
                         "column")
        net = (get(e, "total_payments") - get(e, "ee_pension")
               - get(e, "student_loan") - get(e, "postgrad_loan")
               - get(e, "attachments") - get(e, "ee_nic") - get(e, "tax")
               - get(e, "total_deductions"))
        if abs(net - get(e, "net")) > TOL:
            holds.append(f"{who}: payments less deductions {net} != reported "
                         f"net pay {get(e, 'net')}")

    # ---------- per-employee figures vs the report's Total row ----------
    for key in SUMMED:
        if key not in totals:
            continue
        got = sum((get(e, key) for e in emps), ZERO)
        if abs(got - D(totals[key])) > TOL:
            holds.append(f"per-employee {key} {got} != report total "
                         f"{D(totals[key])}")

    tax_t = sum((get(e, "tax") for e in emps), ZERO)
    ee_nic_t = sum((get(e, "ee_nic") for e in emps), ZERO)
    er_nic_t = sum((get(e, "er_nic") for e in emps), ZERO)
    ee_pen_t = sum((get(e, "ee_pension") for e in emps), ZERO)
    er_pen_t = sum((get(e, "er_pension") for e in emps), ZERO)
    sl_t = sum((get(e, "student_loan") + get(e, "postgrad_loan")
                for e in emps), ZERO)
    att_t = sum((get(e, "attachments") for e in emps), ZERO)
    net_t = sum((get(e, "net") for e in emps), ZERO)

    # ---------- PAYE control must tie to the P30 / employer totals ----------
    ea = D(abs(employer.get("employment_allowance", 0.0)))
    if ea > er_nic_t + TOL:
        # legitimate when the EA claim was made part-way through the year
        # (HMRC applies it from 6 April, so the claim month carries the
        # catch-up); the tie to Total Tax & NIC Due below still proves it
        warnings.append(f"employment allowance {ea} exceeds this month's "
                        f"employer NIC {er_nic_t} - year-to-date catch-up "
                        "after a mid-year claim? The employer NIC cost code "
                        "goes net negative this month")
    # EPS items (see docs/research/05): each one changes what HMRC is owed
    stat_rec = D(abs(employer.get("statutory_recovery", 0.0)))
    ser_comp = D(abs(employer.get("ser_compensation", 0.0)))
    cis_suff = D(abs(employer.get("cis_suffered", 0.0)))
    levy = D(abs(employer.get("apprenticeship_levy", 0.0)))
    if stat_rec and not codes.get("statutory_recovery_code"):
        holds.append(f"statutory pay recovery {stat_rec} on the report but "
                     "the mapping has no 'statutory_recovery_code' (usually "
                     "the statutory pay / gross cost code)")
    if ser_comp and not codes.get("ser_compensation_code"):
        holds.append(f"small employers' relief compensation {ser_comp} on the "
                     "report but the mapping has no 'ser_compensation_code' "
                     "(employer NIC cost or other income)")
    if levy and not codes.get("apprenticeship_levy_cost"):
        holds.append(f"apprenticeship levy {levy} on the report but the "
                     "mapping has no 'apprenticeship_levy_cost'")
    # Everything owed to HMRC through PAYE: tax, both NICs, student and
    # postgraduate loan deductions, apprenticeship levy. Attachments of
    # earnings are owed to the court / DWP, never to HMRC.
    paye_pre_ea = tax_t + ee_nic_t + er_nic_t + sl_t + levy
    if att_t and not codes.get("attachments_payable"):
        holds.append(f"attachments of earnings {att_t} on the report but the "
                     "mapping has no 'attachments_payable' code - add the "
                     "creditor code (court / DWP) before posting")
    # CIS suffered is set off on the EPS but is booked in Xero from the
    # receipts side (cis-tax-deducted: Dr PAYE / Cr 1300). The wages journal
    # must therefore credit PAYE control BEFORE the CIS set-off, and the
    # reconciliation adds the EPS figure back.
    hmrc_movement = paye_pre_ea - ea - stat_rec - ser_comp
    if "total_tax_nic_due" in employer:
        due = D(employer["total_tax_nic_due"])
        if abs(hmrc_movement - cis_suff - due) > TOL:
            holds.append(
                f"PAYE tax {tax_t} + EE NIC {ee_nic_t} + ER NIC {er_nic_t}"
                + (f" + student/postgrad loan {sl_t}" if sl_t else "")
                + (f" + apprenticeship levy {levy}" if levy else "")
                + f" - employment allowance {ea}"
                + (f" - statutory recovery {stat_rec}" if stat_rec else "")
                + (f" - SER compensation {ser_comp}" if ser_comp else "")
                + (f" - CIS suffered {cis_suff}" if cis_suff else "")
                + f" = {hmrc_movement - cis_suff}, but the report says Total "
                f"Tax & NIC Due {due} (an EPS item - statutory recovery, CIS "
                "suffered, apprenticeship levy - the parser did not capture? "
                "Check the Employer Totals block and docs/research/05)")
        if "hmrc_due_for_period" in employer:
            if abs(D(employer["hmrc_due_for_period"]) - due) > TOL:
                holds.append(
                    f"'Tax & NIC due for period' "
                    f"{D(employer['hmrc_due_for_period'])} does not match "
                    f"Total Tax & NIC Due {due} - is this report a single "
                    "PAYE month?")
    else:
        holds.append("employer totals block has no 'Total Tax & NIC Due' - "
                     "it is what proves the PAYE control line against the "
                     "P30; export the full Employer's Summary")
    if "total_other_payments" in employer:
        if abs(ee_pen_t + er_pen_t - D(employer["total_other_payments"])) > TOL:
            holds.append(f"pensions {ee_pen_t + er_pen_t} != Total Other "
                         f"Payments {D(employer['total_other_payments'])}")
    if "total_net_pay" in employer:
        if abs(net_t - D(employer["total_net_pay"])) > TOL:
            holds.append(f"net pay {net_t} != Total Net Pay "
                         f"{D(employer['total_net_pay'])}")
    outlay_convention = "net+hmrc+pensions"
    if "total_net_outlay" in employer and "total_tax_nic_due" in employer:
        want = D(employer["total_net_outlay"])
        outlay = net_t + D(employer["total_tax_nic_due"]) + ee_pen_t + er_pen_t
        if abs(outlay - want) > TOL:
            # Moneysoft may or may not count court-bound attachments in the
            # outlay; accept either presentation, never anything else.
            if att_t and abs(outlay + att_t - want) <= TOL:
                outlay_convention = "net+hmrc+pensions+attachments"
            else:
                holds.append(f"net {net_t} + tax/NIC due + pensions = "
                             f"{outlay} != TOTAL NET OUTLAY {want}")

    # ---------- employee mapping ----------
    known = dict(cfg.get("employees", {}))
    squashed = {re.sub(r"\s+", "", k).lower(): k for k in known}
    for e in emps:
        if e["name"] not in known:
            alias = squashed.get(re.sub(r"\s+", "", e["name"]).lower())
            if alias:
                known[e["name"]] = known[alias]
    unknown = [e["name"] for e in emps if e["name"] not in known]
    if unknown:
        holds.append("employee(s) not in the client mapping: "
                     + ", ".join(unknown)
                     + " - add their pay code (new starter?) before posting")

    gross_lines, div_lines, ernic_lines, erpen_lines = [], [], [], []
    net_lines, dedn_lines = [], []
    for e in emps:
        who, ec = e["name"], known.get(e["name"], {})
        add_codes = {**cfg.get("addition_codes", {}),
                     **ec.get("addition_codes", {})}
        pay_amt = get(e, "basic") + get(e, "hourly")
        buckets = {}
        if pay_amt:
            buckets.setdefault(_code_for(cfg, ec, "pay_code", who,
                                         "gross pay", holds),
                               []).append(("Gross pay", pay_amt))
        for key, label in ADDITIONS:
            amt = get(e, key)
            if not amt:
                continue
            if key == "dividend":
                code = _code_for(cfg, ec, "dividend_code", who, "dividend",
                                 holds)
            elif add_codes.get(key):
                code = str(add_codes[key])
            else:
                code = _code_for(cfg, ec, "pay_code", who, label.lower(),
                                 holds)
            buckets.setdefault(code, []).append((label, amt))
        for code, items in buckets.items():
            for label, amt in items:
                target = div_lines if label == "Dividend" else gross_lines
                kind = "dividend" if label == "Dividend" else "gross"
                target.append(Line(f"{label} - {who} - {tag}", code, amt,
                                   kind, who))
        if get(e, "er_nic"):
            ernic_lines.append(Line(
                f"Employer NIC - {who} - {tag}",
                _code_for(cfg, ec, "er_nic_cost", who, "employer NIC", holds),
                get(e, "er_nic"), "er_nic", who))
        if get(e, "er_pension"):
            erpen_lines.append(Line(
                f"Employer pension - {who} - {tag}",
                _code_for(cfg, ec, "er_pension_cost", who, "employer pension",
                          holds),
                get(e, "er_pension"), "er_pension", who))
        if get(e, "net"):
            net_lines.append(Line(
                f"Net wages - {who} - {tag}",
                _code_for(cfg, ec, "wages_payable", who, "net wages", holds),
                -get(e, "net"), "net", who))
        for key, label in DEDUCTIONS:
            amt = get(e, key)
            if not amt:
                continue
            ckey = {"dividend_tax": "dividend_tax_code"}.get(key, f"{key}_code")
            dedn_lines.append(Line(
                f"{label} - {who} - {tag}",
                _code_for(cfg, ec, ckey, who, label.lower(), holds),
                -amt, key, who))

    lines = gross_lines + div_lines + ernic_lines + erpen_lines \
        + net_lines + dedn_lines

    # attachments of earnings: a separate creditor (court / DWP), one named
    # line per employee, exactly like pensions payable
    paye_line_amount = paye_pre_ea
    if att_t and codes.get("attachments_payable"):
        for e in emps:
            a = get(e, "attachments")
            if a:
                lines.append(Line(
                    f"Attachment of earnings - {e['name']} - {tag}",
                    str(codes["attachments_payable"]), -a, "attachments",
                    e["name"]))
    if paye_line_amount:
        # one credit for the gross liability; EA, statutory recovery and SER
        # compensation are explicit Dr pairs below, so the code nets to the
        # amount HMRC is owed before any CIS set-off
        lines.append(Line(f"PAYE/NIC payable - {tag}",
                          _code_for(cfg, {}, "paye_payable", "the payroll",
                                    "PAYE control", holds),
                          -paye_line_amount, "paye"))
    # pensions payable: one NAMED line per employee so the code reconciles
    # member by member against the NEST (or other provider) schedule
    for e in emps:
        ee_p, er_p = get(e, "ee_pension"), get(e, "er_pension")
        if not (ee_p + er_p):
            continue
        lines.append(Line(
            f"Pensions payable - {e['name']} (EE {ee_p:.2f} / ER {er_p:.2f})"
            f" - {tag}",
            _code_for(cfg, known.get(e["name"], {}), "pensions_payable",
                      e["name"], "pensions payable", holds),
            -(ee_p + er_p), "pension_payable", e["name"]))
    if ea:
        lines.append(Line(f"Employment allowance - {tag}",
                          _code_for(cfg, {}, "paye_payable", "the payroll",
                                    "employment allowance", holds), ea,
                          "ea_paye"))
        lines.append(Line(f"Employment allowance - {tag}",
                          _code_for(cfg, {}, "er_nic_cost", "the payroll",
                                    "employment allowance", holds), -ea,
                          "ea_er_nic"))
    if stat_rec and codes.get("statutory_recovery_code"):
        lines.append(Line(f"Statutory pay recovered - {tag}",
                          _code_for(cfg, {}, "paye_payable", "the payroll",
                                    "statutory recovery", holds), stat_rec,
                          "stat_rec_paye"))
        lines.append(Line(f"Statutory pay recovered - {tag}",
                          _code_for(cfg, {}, "statutory_recovery_code",
                                    "the payroll", "statutory recovery",
                                    holds), -stat_rec, "stat_rec_cost"))
    if ser_comp and codes.get("ser_compensation_code"):
        lines.append(Line(f"Small employers' relief compensation - {tag}",
                          _code_for(cfg, {}, "paye_payable", "the payroll",
                                    "SER compensation", holds), ser_comp,
                          "ser_paye"))
        lines.append(Line(f"Small employers' relief compensation - {tag}",
                          _code_for(cfg, {}, "ser_compensation_code",
                                    "the payroll", "SER compensation", holds),
                          -ser_comp, "ser_income"))
    if levy and codes.get("apprenticeship_levy_cost"):
        lines.append(Line(f"Apprenticeship levy - {tag}",
                          _code_for(cfg, {}, "apprenticeship_levy_cost",
                                    "the payroll", "apprenticeship levy",
                                    holds), levy, "levy_cost"))

    balance = sum((l.amount for l in lines), ZERO)
    if balance != ZERO:
        holds.append(f"journal does not balance - out by {balance}")
    if any(l.account_code in (None, "None", "") for l in lines):
        holds.append("one or more lines have no account code")

    placeholder = [m for m in holds if "placeholder account code" in m]
    blocking = [m for m in holds if m not in placeholder]
    if blocking or (placeholder and not allow_placeholders):
        raise Hold("HOLD - nothing built:\n  - "
                   + "\n  - ".join(dict.fromkeys(holds)),
                   stage="build", details={"problems": list(dict.fromkeys(holds))})
    if len(lines) > MAX_LINES:
        raise Hold(f"{len(lines)} lines exceeds Xero's {MAX_LINES}-line "
                   "journal limit - split the payroll", stage="build")

    meta = {
        "gross": sum((l.amount for l in gross_lines), ZERO),
        "dividends": sum((l.amount for l in div_lines), ZERO),
        "er_nic": er_nic_t, "paye_pre_ea": paye_pre_ea, "ea": ea,
        "paye_due": hmrc_movement,
        "statutory_recovery": stat_rec, "ser_compensation": ser_comp,
        "cis_suffered_eps": cis_suff, "apprenticeship_levy": levy,
        "warnings": warnings,
        "pensions": ee_pen_t + er_pen_t, "ee_pension": ee_pen_t,
        "er_pension": er_pen_t, "net": net_t,
        "attachments": att_t, "student_loans": sl_t,
        "employees": len(emps), "tax_month": tax_month,
        "tax_year": tax_year_of(payroll["period"]),
        "outlay_convention": outlay_convention,
        "show_on_cash_basis": bool(cfg.get("show_on_cash_basis", False)),
        "placeholders": placeholder,
    }
    return Journal(client=payroll.get("client"), period=payroll["period"],
                   narration=narration, date=date, lines=lines, meta=meta)
