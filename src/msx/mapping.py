"""Client mapping files: load, validate, fingerprint.

One JSON file per client in ``clients/<slug>.json``. The mapping is the only
place a nominal code ever comes from. It is versioned in git, validated on
every load, and its SHA-256 fingerprint is recorded against every journal
the pipeline posts, so a mapping change is always auditable.

Schema (see clients/_template.json and docs/architecture/client-mapping.md)::

    {
      "client": "Browns Garage (Haywards Heath) Ltd",
      "slug": "browns-garage-haywards-heath",
      "moneysoft_employer": "Browns Garage (Haywards Heath) Limited",
      "xero": {"org_name": "...", "tenant_id": "", "app": "default"},
      "mode": "shadow" | "draft" | "post",
      "journal_date": "month_end" | "pay_date",
      "narration": "Payroll - {tag}",
      "codes": {"wages_payable": "2200", "paye_payable": "2210",
                "pensions_payable": "2211", "er_nic_cost": "6002",
                "er_pension_cost": "6001", "dividend_code": "471",
                "attachments_payable": ""},
      "addition_codes": {},
      "employees": {"Exact Name As Printed": {"pay_code": "230", ...}},
      "paye_frequency": "monthly" | "quarterly",
      "notes": [], "source": "...", "approved_by": "", "approved_on": ""
    }
"""
import glob
import hashlib
import json
import os
import re

from .errors import Hold

MODES = ("shadow", "draft", "post")
# 'pay_date' is not offered until the parser can read a pay date from the
# filed reports; a literal dd/mm/yyyy is accepted for a one-off re-date.
DATE_RULES = ("month_end",)
REQUIRED_CODES = ("wages_payable", "paye_payable", "er_nic_cost")
OPTIONAL_CODES = ("pensions_payable", "er_pension_cost", "dividend_code",
                  "attachments_payable", "overpayment_code",
                  "payroll_giving_code", "childcare_code",
                  "loans_repayment_code", "rounding_deduction_code",
                  "statutory_recovery_code", "ser_compensation_code",
                  "apprenticeship_levy_cost")
FREQUENCIES = ("monthly", "quarterly")

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(name):
    s = name.lower()
    s = re.sub(r"\b(ltd|limited|llp|plc)\b", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def normalise_name(name):
    """Company-name key for matching report headers to mappings."""
    s = (name or "").lower()
    s = re.sub(r"\b(ltd|limited|llp|plc|the)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def fingerprint(cfg):
    canon = json.dumps(cfg, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def validate(cfg, *, path="<mapping>"):
    """Return a list of problems (empty = valid). Placeholders are allowed
    but reported separately by ``placeholders()``."""
    problems = []
    for key in ("client", "slug", "codes", "employees"):
        if key not in cfg:
            problems.append(f"{path}: missing '{key}'")
    if problems:
        return problems
    if not SLUG_RE.match(str(cfg["slug"])):
        problems.append(f"{path}: slug '{cfg['slug']}' must be lower-case "
                        "words joined by hyphens")
    mode = cfg.get("mode", "shadow")
    if mode not in MODES:
        problems.append(f"{path}: mode '{mode}' must be one of {MODES}")
    rule = str(cfg.get("journal_date", "month_end"))
    if rule not in DATE_RULES and not re.match(r"^\d{2}/\d{2}/\d{4}$", rule):
        problems.append(f"{path}: journal_date '{rule}' must be month_end or a "
                        "one-off dd/mm/yyyy")
    freq = cfg.get("paye_frequency", "monthly")
    if freq not in FREQUENCIES:
        problems.append(f"{path}: paye_frequency '{freq}' must be one of "
                        f"{FREQUENCIES}")
    codes = cfg.get("codes") or {}
    for key in REQUIRED_CODES:
        if not str(codes.get(key, "")).strip():
            problems.append(f"{path}: codes.{key} is empty")
    for key in codes:
        if key not in REQUIRED_CODES + OPTIONAL_CODES:
            problems.append(f"{path}: codes.{key} is not a known code key")
    emps = cfg.get("employees") or {}
    if not isinstance(emps, dict):
        problems.append(f"{path}: employees must be an object keyed by name")
    else:
        for name, rec in emps.items():
            if not name.strip():
                problems.append(f"{path}: an employee has an empty name")
            if not isinstance(rec, dict):
                problems.append(f"{path}: employee '{name}' must be an object")
                continue
            if not str(rec.get("pay_code", "")).strip():
                problems.append(f"{path}: employee '{name}' has no pay_code")
    xero = cfg.get("xero") or {}
    if mode != "shadow" and not (xero.get("tenant_id") or xero.get("org_name")):
        problems.append(f"{path}: mode '{mode}' needs xero.tenant_id or "
                        "xero.org_name")
    if "{tag}" not in cfg.get("narration", "Payroll - {tag}") and \
            "{period}" not in cfg.get("narration", "") and \
            "{month}" not in cfg.get("narration", ""):
        problems.append(f"{path}: narration must include {{tag}}, {{month}} "
                        "or {{period}} so each month's journal is unique")
    return problems


def placeholders(cfg):
    out = []
    for key, val in (cfg.get("codes") or {}).items():
        if str(val).upper().startswith("TBC"):
            out.append(f"codes.{key}={val}")
    for name, rec in (cfg.get("employees") or {}).items():
        for key, val in rec.items():
            if isinstance(val, str) and val.upper().startswith("TBC"):
                out.append(f"employees.{name}.{key}={val}")
    return out


class Mapping:
    def __init__(self, cfg, path=None):
        self.cfg = cfg
        self.path = path
        self.problems = validate(cfg, path=path or "<mapping>")
        self.fingerprint = fingerprint(cfg)

    @property
    def slug(self):
        return self.cfg["slug"]

    @property
    def client(self):
        return self.cfg["client"]

    @property
    def mode(self):
        return self.cfg.get("mode", "shadow")

    @property
    def employer_names(self):
        names = {self.cfg["client"]}
        if self.cfg.get("moneysoft_employer"):
            names.add(self.cfg["moneysoft_employer"])
        for alias in self.cfg.get("aliases", []) or []:
            names.add(alias)
        return names

    def active_for(self, period):
        """True unless active_from / active_to (Mon-YYYY) exclude the period."""
        from .sources import period_sort_key
        k = period_sort_key(period)
        af = self.cfg.get("active_from")
        at = self.cfg.get("active_to")
        if af and k < period_sort_key(af):
            return False
        if at and k > period_sort_key(at):
            return False
        return True

    def matches_employer(self, report_client_name):
        key = normalise_name(report_client_name)
        return any(normalise_name(n) == key for n in self.employer_names)

    def ensure_valid(self):
        if self.problems:
            raise Hold("client mapping invalid:\n  - "
                       + "\n  - ".join(self.problems), stage="mapping")
        return self


def load(path):
    with open(path, encoding="utf-8") as fh:
        try:
            cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            raise Hold(f"{path}: not valid JSON ({exc})", stage="mapping")
    if "slug" not in cfg:
        cfg["slug"] = os.path.splitext(os.path.basename(path))[0]
    return Mapping(cfg, path)


def load_all(clients_dir):
    """Every mapping in the folder, keyed by slug. Template files skipped."""
    out = {}
    for path in sorted(glob.glob(os.path.join(clients_dir, "*.json"))):
        if os.path.basename(path).startswith("_"):
            continue
        m = load(path)
        if m.slug in out:
            raise Hold(f"two mapping files share slug '{m.slug}': "
                       f"{out[m.slug].path} and {path}", stage="mapping")
        out[m.slug] = m
    return out


def find_for_report(mappings, report_client_name):
    """Exactly one mapping whose employer names match the report header."""
    hits = [m for m in mappings.values() if m.matches_employer(report_client_name)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise Hold(f"no client mapping matches the report header "
                   f"'{report_client_name}' - create clients/"
                   f"{slugify(report_client_name)}.json from "
                   "clients/_template.json (copy the codes from the client's "
                   "last posted wages journal in Xero)", stage="mapping")
    raise Hold(f"{len(hits)} client mappings match '{report_client_name}': "
               + ", ".join(m.path or m.slug for m in hits)
               + " - fix moneysoft_employer / aliases so exactly one matches",
               stage="mapping")
