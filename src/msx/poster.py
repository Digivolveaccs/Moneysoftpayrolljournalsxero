"""Post one journal into one Xero organisation - safely, once.

Order of operations (each step can Hold; nothing is written until step 7):

 1. Ledger says already posted/draft with a journal id  -> Skip
 2. Mode 'shadow'                                       -> record, no Xero
 3. Resolve the tenant (mapping.tenant_id, else exact org-name match)
 4. Organisation lock dates: journal date must be after PeriodLockDate
 5. Chart of accounts: every code exists, is ACTIVE, is not a system account
 6. Duplicate guard: list manual journals dated in the period's month
      - exact narration match -> adopt it (crash recovery) or Skip
      - any other wages-looking journal -> Hold (repeating journal? manual?)
 7. Write-ahead: ledger row 'posting' with payload fingerprint
 8. POST /ManualJournals with an Idempotency-Key
 9. Ledger row 'draft' or 'posted' with the ManualJournalID

Mode 'draft' creates a DRAFT for a human to approve in Xero; mode 'post'
creates it POSTED. Promotion of an existing draft is ``approve()``.
"""
import datetime
import hashlib
import json
import re

from .errors import Hold, Skip
from .journal_builder import period_parts
from .mapping import normalise_name
from .xero_client import XeroError

WAGES_RE = re.compile(r"wage|payroll|salar|paye|moneysoft|brightpay|staff cost",
                      re.I)
SYSTEM_ACCOUNTS_BLOCKED = {"DEBTORS", "CREDITORS", "GST", "GSTONIMPORTS",
                           "BANKCURRENCYGAIN", "RETAINEDEARNINGS",
                           "ROUNDING", "TRACKINGTRANSFERS", "UNPAIDEXPCLM",
                           "UNREALISEDCURRENCYGAIN", "WAGEPAYABLES",
                           "CISASSETS", "CISASSET", "CISLABOUR",
                           "CISLABOUREXPENSE", "CISLABOURINCOME",
                           "CISLIABILITY", "CISMATERIALS"}


class PostResult:
    def __init__(self, outcome, *, message, xero_journal_id=None,
                 tenant_id=None, status=None, checks=None):
        self.outcome = outcome            # shadow / draft / posted / skipped
        self.message = message
        self.xero_journal_id = xero_journal_id
        self.tenant_id = tenant_id
        self.status = status
        self.checks = checks or {}

    def as_dict(self):
        return {"outcome": self.outcome, "message": self.message,
                "xero_journal_id": self.xero_journal_id,
                "tenant_id": self.tenant_id, "status": self.status,
                "checks": self.checks}


def payload_fingerprint(payload):
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def resolve_tenant(mapping, tenants):
    """tenant_id from the mapping, else the single tenant whose name
    matches xero.org_name after normalisation. Never fuzzy."""
    xero_cfg = mapping.cfg.get("xero") or {}
    tid = (xero_cfg.get("tenant_id") or "").strip()
    if tid:
        hit = [t for t in tenants if t["tenant_id"] == tid]
        if not hit:
            raise Hold(f"{mapping.client}: xero.tenant_id {tid} is not among "
                       "the organisations this Xero app is connected to - "
                       "reconnect the org (msx auth login) or fix the mapping",
                       stage="tenant")
        return hit[0]
    name = (xero_cfg.get("org_name") or "").strip()
    if not name:
        raise Hold(f"{mapping.client}: mapping has neither xero.tenant_id nor "
                   "xero.org_name", stage="tenant")
    key = normalise_name(name)
    hits = [t for t in tenants if normalise_name(t["name"] or "") == key]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise Hold(f"{mapping.client}: no connected Xero organisation is "
                   f"named '{name}' (connected: "
                   + ", ".join(sorted(t["name"] or "?" for t in tenants))
                   + ") - connect it, or set xero.tenant_id", stage="tenant")
    raise Hold(f"{mapping.client}: {len(hits)} connected organisations match "
               f"'{name}' - set xero.tenant_id explicitly", stage="tenant")


def check_lock_dates(org, journal):
    jdate = datetime.date.fromisoformat(journal.iso_date())
    problems = []
    for key, label in (("period_lock_date", "period lock date"),
                       ("end_of_year_lock_date", "end of year lock date")):
        val = org.get(key)
        if val:
            lock = datetime.date.fromisoformat(val)
            if jdate <= lock:
                problems.append(f"journal date {jdate} is on or before the "
                                f"org's {label} {lock}")
    if problems:
        raise Hold(f"{org.get('name')}: " + "; ".join(problems)
                   + " - unlock the period in Xero, or set journal_date to "
                   "the first open day (dd/mm/yyyy) in the client mapping "
                   "for this month and revert it afterwards",
                   stage="lock")


# what each line kind must NOT land on: a payable that is really an expense
# account, or a cost that is really income, is a wrong mapping even though
# Xero would accept the journal (814 = PAYE Payable at Brandtek is the
# canonical example - see docs/research/06 M4)
LIABILITY_KINDS = {"net", "paye", "pension_payable", "attachments", "ea_paye",
                   "stat_rec_paye", "ser_paye", "dividend_tax", "overpayment",
                   "payroll_giving", "childcare", "loans_repayment"}
COST_KINDS = {"gross", "dividend", "er_nic", "er_pension", "ea_er_nic",
              "stat_rec_cost", "levy_cost"}
NAME_CONFLICTS = {"net": ("paye", "nic", "hmrc"),
                  "paye": ("wages payable", "net wages", "pension"),
                  "pension_payable": ("paye", "wages")}


def check_accounts(accounts, journal, org_name, expect_names=None):
    problems = []
    expect_names = expect_names or {}
    by_code = {}
    for l in journal.lines:
        by_code.setdefault(l.account_code, set()).add(l.kind)
    for code in sorted(by_code):
        a = accounts.get(code)
        if not a:
            problems.append(f"code {code} does not exist in {org_name}")
            continue
        name = a.get("name") or ""
        if (a.get("status") or "ACTIVE") != "ACTIVE":
            problems.append(f"code {code} ({name}) is {a.get('status')}")
        sys_acc = (a.get("system_account") or "").upper()
        if sys_acc in SYSTEM_ACCOUNTS_BLOCKED:
            problems.append(f"code {code} ({name}) is the system account "
                            f"{sys_acc} - Xero rejects manual journals to it")
        cls = (a.get("class") or "").upper()
        kinds = by_code[code]
        if cls:
            # payables owed to HMRC / providers / courts must be liabilities;
            # net wages, dividend tax and loan repayments may also go to a
            # director's loan or employee loan account (liability or asset)
            strict_liab = kinds & {"paye", "pension_payable", "attachments",
                                   "ea_paye", "stat_rec_paye", "ser_paye"}
            loose_liab = kinds & (LIABILITY_KINDS - {"paye", "pension_payable",
                                                     "attachments", "ea_paye",
                                                     "stat_rec_paye", "ser_paye"})
            if strict_liab and cls != "LIABILITY":
                problems.append(f"code {code} ({name}) is a {cls} account but "
                                f"the mapping uses it as a payable "
                                f"({', '.join(sorted(strict_liab))})")
            if loose_liab and cls not in ("LIABILITY", "ASSET"):
                problems.append(f"code {code} ({name}) is a {cls} account but "
                                f"the mapping credits {', '.join(sorted(loose_liab))} "
                                "to it")
            costs = kinds & (COST_KINDS - {"dividend"})
            if costs and cls != "EXPENSE":
                problems.append(f"code {code} ({name}) is a {cls} account but "
                                f"the mapping uses it as a cost "
                                f"({', '.join(sorted(costs))})")
            if "dividend" in kinds and cls not in ("EXPENSE", "EQUITY"):
                problems.append(f"code {code} ({name}) is a {cls} account but "
                                "the mapping debits dividends to it")
        low = name.lower()
        for kind, bad_words in NAME_CONFLICTS.items():
            if kind in kinds and any(w in low for w in bad_words):
                problems.append(f"code {code} is named '{name}' but the "
                                f"mapping uses it for {kind} - the same code "
                                "means something else in this org")
        want = expect_names.get(code)
        if want and want.strip().lower() != low.strip():
            problems.append(f"code {code} is now named '{name}' but the "
                            f"mapping expects '{want}' - the chart changed; "
                            "re-confirm the mapping")
    if problems:
        raise Hold("chart of accounts check failed: " + "; ".join(problems)
                   + " - fix the client mapping or the Xero chart",
                   stage="accounts")
    return {code: accounts[code].get("name") for code in sorted(by_code)}


def verify_written(xero, tenant_id, journal_id, journal, expected_status):
    """Read the journal back and prove it is what we sent. A mismatch is a
    human-level HOLD (never auto-void)."""
    full = xero.manual_journal(tenant_id, journal_id) or {}
    lines = full.get("JournalLines", []) or []
    got_debits = round(sum(float(l.get("LineAmount", 0)) for l in lines
                           if float(l.get("LineAmount", 0)) > 0), 2)
    problems = []
    if abs(got_debits - float(journal.total_debits)) > 0.005:
        problems.append(f"debits {got_debits:.2f} != {journal.total_debits}")
    if len(lines) != len(journal.lines):
        problems.append(f"{len(lines)} lines != {len(journal.lines)} sent")
    if (full.get("Status") or expected_status) != expected_status:
        problems.append(f"status {full.get('Status')} != {expected_status}")
    got_date = _date_of(full.get("Date"))
    if got_date and got_date != journal.iso_date():
        problems.append(f"date {got_date} != {journal.iso_date()}")
    warnings = [w.get("Message", "") for w in full.get("Warnings", []) or []]
    if warnings:
        problems.append("Xero warnings: " + "; ".join(warnings))
    return problems


def _date_of(value):
    from .xero_client import _xero_date
    return _xero_date(value)


def month_window(period, tail_days=10):
    """Calendar month of the period plus a tail into the next month: a
    wages journal for tax month 'Apr-2026' (6 Apr-5 May) may legitimately be
    dated on a pay date up to the 5th of May, and must still be seen."""
    _, _, month_end, end = period_parts(period)
    start = end.replace(day=1)
    return start.isoformat(), (end + datetime.timedelta(days=tail_days)).isoformat()


def duplicate_guard(xero, tenant_id, journal, ledger_row):
    """Returns ('none', None) | ('adopt', existing) | raises Skip/Hold."""
    start, end = month_window(journal.period)
    existing = xero.find_manual_journals(tenant_id, start, end)
    exact = [e for e in existing if e["narration"].strip() == journal.narration]
    if exact:
        e = exact[0]
        if len(exact) > 1:
            raise Hold(f"{len(exact)} journals in Xero already carry the "
                       f"narration '{journal.narration}' - a human must "
                       "remove the duplicates", stage="duplicate")
        full = xero.manual_journal(tenant_id, e["id"]) or {}
        total = sum(float(l.get("LineAmount", 0)) for l in
                    full.get("JournalLines", []) if float(l.get("LineAmount", 0)) > 0)
        if abs(total - float(journal.total_debits)) > 0.005:
            raise Hold(f"Xero already has '{journal.narration}' ({e['status']}, "
                       f"id {e['id']}) with debits {total:.2f}, but this "
                       f"build totals {journal.total_debits} - figures differ; "
                       "a human must decide (re-run? correction?)",
                       stage="duplicate")
        if ledger_row and ledger_row.get("status") == "posting":
            return "adopt", e          # crash between POST and record
        raise Skip(f"already in Xero as {e['status']} journal {e['id']} "
                   f"('{journal.narration}')", reason="duplicate")
    wages_like = [e for e in existing if WAGES_RE.search(e["narration"])]
    if wages_like:
        desc = "; ".join(f"'{e['narration']}' ({e['status']}, {e['date']})"
                         for e in wages_like[:5])
        raise Hold(f"another wages-looking journal already sits in "
                   f"{journal.period}: {desc} - is this month already posted "
                   "(manual or repeating journal)? A human must check before "
                   "this journal is posted", stage="duplicate")
    return "none", None


def post_journal(journal, mapping, ledger, xero, *, mode=None, dry_run=False,
                 url=None):
    mode = mode or mapping.mode
    slug, period = mapping.slug, journal.period
    row = ledger.get(slug, period)
    checks = {}
    if row and row.get("xero_journal_id") and row.get("status") in ("posted",
                                                                     "draft"):
        raise Skip(f"ledger says {row['status']} as Xero journal "
                   f"{row['xero_journal_id']} on {row['updated_at']}",
                   reason="ledger")

    payload = journal.to_api_payload(
        status="POSTED" if mode == "post" else "DRAFT", url=url)
    payload_sha = journal.figures_fingerprint()      # status-independent
    request_sha = payload_fingerprint(payload)       # exact bytes sent
    base = dict(narration=journal.narration, journal_date=journal.iso_date(),
                total_debits=f"{journal.total_debits:.2f}",
                paye_due=f"{journal.meta['paye_due']:.2f}",
                mapping_sha=mapping.fingerprint, payload_sha=payload_sha,
                mode=mode)

    if mode == "shadow":
        ledger.upsert(slug, period, status="shadow",
                      note="shadow mode - built and reconciled, not sent",
                      **base)
        return PostResult("shadow", message="shadow mode: journal built and "
                          "reconciled, nothing sent to Xero", checks=checks)
    if (mapping.cfg.get("xero") or {}).get("client_posts_own_journal"):
        ledger.upsert(slug, period, status="skipped",
                      note="client posts their own wages journal - recon only",
                      **base)
        raise Skip(f"{mapping.client}: client posts their own wages journal "
                   "(xero.client_posts_own_journal) - built for the record, "
                   "not sent", reason="client-posts-own")

    tenant = resolve_tenant(mapping, xero.tenants())
    tid = tenant["tenant_id"]
    checks["tenant"] = tenant["name"]
    org = xero.organisation(tid)
    check_lock_dates(org, journal)
    checks["lock_dates"] = {"period": org.get("period_lock_date"),
                            "year_end": org.get("end_of_year_lock_date")}
    checks["accounts"] = check_accounts(
        xero.accounts(tid), journal, org.get("name"),
        expect_names=(mapping.cfg.get("xero") or {}).get("expect_names"))
    action, existing = duplicate_guard(xero, tid, journal, row)
    if action == "adopt":
        status = "posted" if existing["status"] == "POSTED" else "draft"
        ledger.mark_posted(slug, period, xero_journal_id=existing["id"],
                           status=status, xero_tenant_id=tid,
                           note="adopted after interrupted post", **base)
        return PostResult(status, message=f"adopted existing {existing['status']} "
                          f"journal {existing['id']} left by an interrupted "
                          "run", xero_journal_id=existing["id"], tenant_id=tid,
                          status=existing["status"], checks=checks)
    if dry_run:
        return PostResult("dry-run", message="all checks passed; would POST "
                          f"{'POSTED' if mode == 'post' else 'DRAFT'} journal "
                          f"'{journal.narration}' to {tenant['name']}",
                          tenant_id=tid, checks=checks)

    # reuse the key of an interrupted attempt so a retry is byte-identical
    idem = None
    if row and row.get("status") == "posting" and row.get("idempotency_key") \
            and row.get("payload_sha") == payload_sha:
        idem = row["idempotency_key"]
    idem = idem or hashlib.sha256(
        f"{tid}|{journal.narration}|{journal.iso_date()}|{request_sha}"
        .encode()).hexdigest()
    ledger.mark_intent(slug, period, xero_tenant_id=tid, idempotency_key=idem,
                       **base)
    try:
        created = xero.create_manual_journal(tid, payload, idempotency_key=idem)
    except XeroError as exc:
        if exc.retryable:
            # the request may or may not have reached Xero: leave the row in
            # 'posting' so the next run adopts or re-sends with the same key
            raise Hold(f"{mapping.client} {period}: Xero unavailable ({exc}) - "
                       "will retry next run", stage="post-retry",
                       details={"xero": exc.body})
        ledger.mark_failed(slug, period, f"Xero refused: {exc}",
                           xero_tenant_id=tid, **base)
        raise Hold(f"{mapping.client} {period}: {exc}", stage="post",
                   details={"xero": exc.body})
    jid = created.get("ManualJournalID")
    xstatus = created.get("Status") or payload["Status"]
    status = "posted" if xstatus == "POSTED" else "draft"
    ledger.mark_posted(slug, period, xero_journal_id=jid, status=status,
                       xero_tenant_id=tid, note="", **base)
    problems = verify_written(xero, tid, jid, journal, payload["Status"])
    if problems:
        ledger.upsert(slug, period, note="POST-WRITE MISMATCH: "
                      + "; ".join(problems))
        raise Hold(f"{mapping.client} {period}: journal {jid} was created in "
                   f"{tenant['name']} but reads back differently: "
                   + "; ".join(problems) + " - a human must inspect it in "
                   "Xero (do not re-run until resolved)", stage="verify",
                   details={"xero_journal_id": jid})
    return PostResult(status, message=f"{xstatus} journal {jid} created in "
                      f"{tenant['name']}: '{journal.narration}' "
                      f"Dr=Cr={journal.total_debits}",
                      xero_journal_id=jid, tenant_id=tid, status=xstatus,
                      checks=checks)


def approve(slug, period, ledger, xero):
    """Promote a DRAFT the pipeline created to POSTED."""
    row = ledger.get(slug, period)
    if not row or not row.get("xero_journal_id"):
        raise Hold(f"{slug} {period}: no Xero journal recorded in the ledger",
                   stage="approve")
    if row["status"] == "posted":
        raise Skip(f"{slug} {period}: already posted", reason="ledger")
    j = xero.set_manual_journal_status(row["xero_tenant_id"],
                                       row["xero_journal_id"], "POSTED")
    ledger.mark_posted(slug, period, xero_journal_id=row["xero_journal_id"],
                       status="posted", note="approved")
    return j
