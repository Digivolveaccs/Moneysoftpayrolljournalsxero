"""Read-only reconciliation sweep: the ledger versus what Xero actually holds.

Modelled on the peer-practice control that caught silent Employment
Allowance omissions and duplicate journals. It NEVER writes to Xero or to
the ledger's journal rows; it produces findings for a human:

    duplicate   two wages-looking journals in one month with the same total
    missing     the ledger says posted/draft but Xero has no such journal
                (or it is VOIDED / DELETED)
    mismatch    the Xero journal's debit total differs from the ledger's
    orphan      a wages-looking journal in Xero that the ledger knows
                nothing about (posted by hand, by a repeating journal, or
                by another tool) in a month the pipeline covers
    draft-aging a DRAFT the pipeline created more than N days ago that
                nobody has approved
    unbalanced  a journal whose lines do not net to zero (should be
                impossible; Xero rejects them - reported if ever seen)
"""
import datetime

from .poster import WAGES_RE, month_window, resolve_tenant


def _period_end(period):
    from .journal_builder import period_parts
    return period_parts(period)[3]


def _total_debits(full):
    return round(sum(float(l.get("LineAmount", 0)) for l in
                     full.get("JournalLines", []) or []
                     if float(l.get("LineAmount", 0)) > 0), 2)


def _balance(full):
    return round(sum(float(l.get("LineAmount", 0)) for l in
                     full.get("JournalLines", []) or []), 2)


def sweep(mappings, ledger, xero_for, *, periods, draft_age_days=14,
          today=None):
    """-> list of finding dicts. ``periods`` = ['Aug-2026', ...]."""
    today = today or datetime.date.today()
    findings = []
    seen_journals = set()
    rows = {(r["slug"], r["period"]): r for r in ledger.all_rows()}
    for slug, m in sorted(mappings.items()):
        if m.mode == "shadow" and not any(
                rows.get((slug, p), {}).get("xero_journal_id") for p in periods):
            continue                       # nothing of ours can be in Xero
        own = (m.cfg.get("xero") or {}).get("client_posts_own_journal")
        try:
            xero = xero_for(m)
            tenant = resolve_tenant(m, xero.tenants())
        except Exception as exc:           # tenant not connected etc.
            findings.append({"kind": "unreachable", "client": m.client,
                             "period": "-", "note": str(exc)})
            continue
        tid = tenant["tenant_id"]
        for period in periods:
            start, end = month_window(period)
            try:
                existing = xero.find_manual_journals(tid, start, end,
                                                     include_deleted=True)
                # the pipeline's own journal may sit outside the window
                # (one-off re-date); read it back by id so it is never
                # reported as missing
                r0 = rows.get((slug, period))
                if r0 and r0.get("xero_journal_id") and \
                        r0["xero_journal_id"] not in {e["id"] for e in existing}:
                    full = xero.manual_journal(tid, r0["xero_journal_id"])
                    if full:
                        from .xero_client import _xero_date
                        existing = list(existing) + [{
                            "id": r0["xero_journal_id"],
                            "narration": full.get("Narration") or "",
                            "status": full.get("Status"),
                            "date": _xero_date(full.get("Date"))}]
            except Exception as exc:
                findings.append({"kind": "unreachable", "client": m.client,
                                 "period": period, "note": str(exc)})
                continue
            wages = [e for e in existing if WAGES_RE.search(e["narration"] or "")
                     and (slug, e["id"]) not in seen_journals]
            for e in wages:
                seen_journals.add((slug, e["id"]))
            live = [e for e in wages if e["status"] in ("POSTED", "DRAFT")]
            row = rows.get((slug, period))
            totals = {}
            for e in live:
                full = xero.manual_journal(tid, e["id"]) or {}
                totals[e["id"]] = _total_debits(full)
                if abs(_balance(full)) > 0.005:
                    findings.append({"kind": "unbalanced", "client": m.client,
                                     "period": period, "journal": e["id"],
                                     "note": f"'{e['narration']}' nets to "
                                             f"{_balance(full)}"})
            # duplicates: same date + same total
            seen = {}
            for e in live:
                key = (e["date"], totals[e["id"]])
                if key in seen:
                    findings.append({"kind": "duplicate", "client": m.client,
                                     "period": period, "journal": e["id"],
                                     "note": f"'{e['narration']}' ({e['status']}) "
                                             f"duplicates {seen[key]} on {e['date']} "
                                             f"total {totals[e['id']]:.2f}"})
                else:
                    seen[key] = e["id"]
            if row and row.get("xero_journal_id"):
                jid = row["xero_journal_id"]
                match = next((e for e in existing if e["id"] == jid), None)
                if not match or match["status"] not in ("POSTED", "DRAFT"):
                    findings.append({"kind": "missing", "client": m.client,
                                     "period": period, "journal": jid,
                                     "note": f"ledger says {row['status']} as {jid} "
                                             f"but Xero shows "
                                             f"{match['status'] if match else 'nothing'}"})
                else:
                    want = float(row.get("total_debits") or 0)
                    got = totals.get(jid)
                    if got is None:
                        got = _total_debits(xero.manual_journal(tid, jid) or {})
                    if abs(got - want) > 0.02:
                        findings.append({"kind": "mismatch", "client": m.client,
                                         "period": period, "journal": jid,
                                         "note": f"ledger total {want:.2f} vs Xero "
                                                 f"{got:.2f}"})
                    if match["status"] == "DRAFT":
                        try:
                            created = datetime.date.fromisoformat(
                                row["created_at"][:10])
                        except (ValueError, TypeError):
                            created = today
                        if (today - created).days > draft_age_days:
                            findings.append({"kind": "draft-aging",
                                             "client": m.client,
                                             "period": period, "journal": jid,
                                             "note": f"DRAFT created {created} still "
                                                     "not approved in Xero"})
            if m.mode in ("draft", "post") and m.active_for(period) and not own \
                    and not (row and (row.get("xero_journal_id")
                                      or row.get("status") == "skipped")) \
                    and not live and (today - _period_end(period)).days >= 7:
                findings.append({"kind": "no-journal", "client": m.client,
                                 "period": period, "journal": "",
                                 "note": "active client in " + m.mode + " mode "
                                         "with no journal in Xero and nothing "
                                         "in the ledger - payroll not run, "
                                         "report not filed, or held?"})
            for e in live:
                if own:
                    break                  # their journal is expected
                if not row or e["id"] != row.get("xero_journal_id"):
                    findings.append({"kind": "orphan", "client": m.client,
                                     "period": period, "journal": e["id"],
                                     "note": f"'{e['narration']}' ({e['status']}, "
                                             f"{e['date']}, total "
                                             f"{totals[e['id']]:.2f}) is not the "
                                             "pipeline's journal"})
    return findings


def render(findings):
    if not findings:
        return "recon: no findings"
    lines = [f"recon: {len(findings)} finding(s)"]
    for f in findings:
        lines.append(f"  {f['kind']:<12} {f['client']:<40} {f['period']:<9} "
                     f"{f.get('journal') or '':<38} {f['note']}")
    return "\n".join(lines)
