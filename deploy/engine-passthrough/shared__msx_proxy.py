"""Scoped Xero pass-through for the payroll journal pipeline (`msx`).

The Moneysoft-to-Xero pipeline (repo `Moneysoftpayrolljournalsxero`) runs on
the payroll Mac and needs a handful of Xero Accounting API calls per client:
list connections, read the organisation's lock dates, read the chart, list and
read manual journals, create one manual journal, and approve a draft. It has
no Xero token of its own - this app holds the one token that covers every
connected client - so it sends those calls here and this module decides, per
request, whether to forward them.

Everything about the decision is pure and tested. The Function App route is
glue: principal check, decision, forward, return Xero's answer verbatim
(status, body and the two headers the caller acts on), so the caller's own
error handling, retry and idempotency logic work exactly as against Xero.

What is allowed, and nothing else:

    GET  connections
    GET  Organisation | Accounts | TaxRates            (per tenant)
    GET  ManualJournals[?where=&page=&order=]           (per tenant)
    GET  ManualJournals/{guid}                          (per tenant)
    PUT  ManualJournals  - exactly ONE journal, DRAFT or POSTED, balanced
    POST ManualJournals/{guid} - body is only {ManualJournalID, Status: POSTED}

The tenant must be an org that is `connected` in this app's registry. A
request outside this list is refused with 403 and never reaches Xero.
"""
import re
from decimal import Decimal, InvalidOperation

_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_MJ_ONE = re.compile(rf"^ManualJournals/({_GUID})$")
_QUERY_KEYS = {"where", "page", "order", "summarizeErrors", "pageSize"}
PASS_HEADERS = ("Retry-After", "X-Rate-Limit-Problem", "X-DayLimit-Remaining",
                "X-MinLimit-Remaining")


class Refusal(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _lines_balance(journal):
    lines = journal.get("JournalLines")
    if not isinstance(lines, list) or len(lines) < 2:
        raise Refusal(422, "a manual journal needs at least two lines")
    total = Decimal("0")
    for i, line in enumerate(lines):
        if not isinstance(line, dict) or not line.get("AccountCode"):
            raise Refusal(422, f"line {i}: AccountCode is required")
        if "AccountID" in line:
            raise Refusal(422, f"line {i}: lines are addressed by AccountCode only")
        try:
            amt = Decimal(str(line.get("LineAmount")))
        except (InvalidOperation, TypeError, ValueError):
            raise Refusal(422, f"line {i}: LineAmount is not a number")
        if amt == 0:
            raise Refusal(422, f"line {i}: LineAmount is zero")
        total += amt
    if total != 0:
        raise Refusal(422, f"journal does not balance (net {total})")


def check_create(body):
    """A PUT ManualJournals body: one journal, DRAFT/POSTED, balanced."""
    if not isinstance(body, dict):
        raise Refusal(422, "body must be a JSON object")
    journals = body.get("ManualJournals")
    if not isinstance(journals, list) or len(journals) != 1:
        raise Refusal(422, "exactly one journal per request")
    j = journals[0]
    if not isinstance(j, dict):
        raise Refusal(422, "journal must be an object")
    if j.get("ManualJournalID"):
        raise Refusal(422, "PUT creates; it never names an existing journal")
    if j.get("Status", "DRAFT") not in ("DRAFT", "POSTED"):
        raise Refusal(422, f"status must be DRAFT or POSTED, not {j.get('Status')!r}")
    if not str(j.get("Narration") or "").strip():
        raise Refusal(422, "Narration is required")
    if not j.get("Date"):
        raise Refusal(422, "Date is required")
    _lines_balance(j)


def check_status_change(journal_id, body):
    """A POST ManualJournals/{id} body: only an approval of that journal."""
    if not isinstance(body, dict):
        raise Refusal(422, "body must be a JSON object")
    journals = body.get("ManualJournals")
    if not isinstance(journals, list) or len(journals) != 1 \
            or not isinstance(journals[0], dict):
        raise Refusal(422, "exactly one journal per request")
    j = journals[0]
    if set(j) != {"ManualJournalID", "Status"}:
        raise Refusal(403, "only a status change is allowed on this route")
    if j["ManualJournalID"].lower() != journal_id.lower():
        raise Refusal(403, "journal id in the body must match the URL")
    if j["Status"] != "POSTED":
        raise Refusal(403, "the only status change allowed is DRAFT -> POSTED")


def decide(method, path, params, tenant_id, body, org_lookup):
    """Return the forwardable (path, params) or raise Refusal.

    `org_lookup(tenant_id)` returns the registry row or None. Pure.
    """
    method = (method or "").upper()
    path = (path or "").strip("/")
    params = {k: v for k, v in (params or {}).items() if k in _QUERY_KEYS}

    if path == "connections":
        if method != "GET":
            raise Refusal(403, "connections is read-only")
        return path, {}

    if not tenant_id:
        raise Refusal(400, "Xero-Tenant-Id header is required")
    org = org_lookup(tenant_id)
    if org is None or org.get("status") != "connected":
        raise Refusal(403, f"tenant {tenant_id} is not a connected organisation")

    if path in ("Organisation", "Accounts", "TaxRates"):
        if method != "GET":
            raise Refusal(403, f"{path} is read-only")
        return path, {}
    if path == "ManualJournals":
        if method == "GET":
            return path, params
        if method == "PUT":
            check_create(body)
            return path, {"summarizeErrors": "false"}
        raise Refusal(403, "ManualJournals allows GET and PUT only")
    m = _MJ_ONE.match(path)
    if m:
        if method == "GET":
            return path, {}
        if method == "POST":
            check_status_change(m.group(1), body)
            return path, {"summarizeErrors": "false"}
        raise Refusal(403, "ManualJournals/{id} allows GET and POST only")
    raise Refusal(403, f"{method} {path} is not on the msx allow-list")
