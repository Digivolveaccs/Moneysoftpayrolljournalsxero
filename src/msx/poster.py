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
                   + " - unlock the period in Xero or re-date the journal "
                   "(--date) and note the true period in the narration",
                   stage="lock")


def check_accounts(accounts, journal, org_name):
    problems = []
    for code in sorted({l.account_code for l in journal.lines}):
        a = accounts.get(code)
        if not a:
            problems.append(f"code {code} does not exist in {org_name}")
            continue
        if (a.get("status") or "ACTIVE") != "ACTIVE":
            problems.append(f"code {code} ({a.get('name')}) is "
                            f"{a.get('status')}")
        sys_acc = (a.get("system_account") or "").upper()
        if sys_acc in SYSTEM_ACCOUNTS_BLOCKED:
            problems.append(f"code {code} ({a.get('name')}) is the system "
                            f"account {sys_acc} - Xero rejects manual "
                            "journals to it")
    if problems:
        raise Hold("chart of accounts check failed: " + "; ".join(problems)
                   + " - fix the client mapping or the Xero chart",
                   stage="accounts")
    return {code: accounts[code].get("name") for code in
            sorted({l.account_code for l in journal.lines})}


def month_window(period):
    _, _, month_end, end = period_parts(period)
    start = end.replace(day=1)
    return start.isoformat(), end.isoformat()


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
    payload_sha = payload_fingerprint(payload)
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

    tenant = resolve_tenant(mapping, xero.tenants())
    tid = tenant["tenant_id"]
    checks["tenant"] = tenant["name"]
    org = xero.organisation(tid)
    check_lock_dates(org, journal)
    checks["lock_dates"] = {"period": org.get("period_lock_date"),
                            "year_end": org.get("end_of_year_lock_date")}
    checks["accounts"] = check_accounts(xero.accounts(tid), journal,
                                        org.get("name"))
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

    idem = hashlib.sha256(f"{tid}|{journal.narration}|{journal.iso_date()}|"
                          f"{payload_sha}".encode()).hexdigest()
    ledger.mark_intent(slug, period, xero_tenant_id=tid, **base)
    try:
        created = xero.create_manual_journal(tid, payload, idempotency_key=idem)
    except XeroError as exc:
        ledger.mark_failed(slug, period, f"Xero refused: {exc}",
                           xero_tenant_id=tid, **base)
        raise Hold(f"{mapping.client} {period}: {exc}", stage="post",
                   details={"xero": exc.body})
    jid = created.get("ManualJournalID")
    xstatus = created.get("Status") or payload["Status"]
    status = "posted" if xstatus == "POSTED" else "draft"
    ledger.mark_posted(slug, period, xero_journal_id=jid, status=status,
                       xero_tenant_id=tid, note="", **base)
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
