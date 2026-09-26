"""Discover filed Moneysoft reports on disk and prove they are safe to read.

The payroll-agent files every client-period's reports (via split_reports.py)
under::

    <pdf_root>/<Client YYYY-YY>/<Client> - Employer's Summary for <Mon-YYYY>.pdf
    <pdf_root>/<Client YYYY-YY>/<Client> - P30 Employer's Payslip for <span>.pdf
    <pdf_root>/<Client YYYY-YY>/<Client> - Employee Payslip for <Mon-YYYY> for <Name>.pdf

That folder is Dropbox-synced, so a file can be half-written, online-only,
or a "conflicted copy". Nothing here is parsed until it passes ``stable()``.
"""
import hashlib
import os
import re
import time

from .errors import Hold

SUMMARY_RE = re.compile(
    r"^(?P<client>.+?) - Employer's Summary(?: \((?P<layout>[A-Za-z]+)\))? "
    r"for (?P<period>[A-Z][a-z]{2}-\d{4})\.pdf$")
P30_RE = re.compile(
    r"^(?P<client>.+?) - P30 Employer's Payslip for (?P<span>[A-Z][a-z]{2}-\d{4}"
    r"(?: to [A-Z][a-z]{2}-\d{4})?)\.pdf$")
CONFLICT_RE = re.compile(r"conflicted copy|\(\d+\)\.pdf$", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def stable(path, *, settle_seconds=30, sample_wait=1.0, clock=time.time,
           sleep=time.sleep):
    """True when the file is fully written: non-empty, size unchanged over
    ``sample_wait`` seconds, and last modified at least ``settle_seconds``
    ago (Dropbox writes in place and can take a while to finish)."""
    try:
        st1 = os.stat(path)
    except FileNotFoundError:
        return False
    if st1.st_size == 0:
        return False
    if clock() - st1.st_mtime < settle_seconds:
        return False
    sleep(sample_wait)
    try:
        st2 = os.stat(path)
    except FileNotFoundError:
        return False
    return st1.st_size == st2.st_size and st1.st_mtime == st2.st_mtime


def is_pdf(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def period_sort_key(period):
    mon, yr = period.split("-")
    return int(yr) * 12 + MONTHS[mon]


def find_summaries(pdf_root, *, periods=None, clients=None):
    """Every Employer's Summary PDF under pdf_root -> list of dicts, sorted
    by client then period. Conflicted copies and '(1)' duplicates are
    reported with kind='conflict' so the caller can hold them, never
    silently skipped."""
    out = []
    root = os.path.expanduser(pdf_root)
    if not os.path.isdir(root):
        raise Hold(f"PDF root folder not found: {root} - is Dropbox syncing "
                   "on this machine?", stage="discover")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and not d.lower().startswith("superseded")]
        for name in filenames:
            m = SUMMARY_RE.match(name)
            path = os.path.join(dirpath, name)
            if CONFLICT_RE.search(name) and "Employer's Summary" in name:
                out.append({"kind": "conflict", "path": path, "name": name})
                continue
            if not m:
                continue
            client, period = m.group("client"), m.group("period")
            if periods and period not in periods:
                continue
            if clients and client not in clients:
                continue
            layout = (m.group("layout") or "").lower() or None
            out.append({"kind": "summary", "client": client, "period": period,
                        "layout": layout, "path": path, "folder": dirpath,
                        "name": name})
    out.sort(key=lambda r: (r.get("client", ""), period_sort_key(r["period"])
                            if r.get("period") else 0, r.get("layout") or ""))
    return out


def group_summaries(rows):
    """{(client, period): [summary rows]} - several layouts may be filed as
    separate PDFs ('Employer's Summary (Additions) for ...')."""
    groups = {}
    for r in rows:
        if r["kind"] != "summary":
            continue
        groups.setdefault((r["client"], r["period"]), []).append(r)
    return groups


def find_p30(folder, client, period):
    """The P30 PDF covering ``period`` in ``folder`` (exact span preferred)."""
    exact = cover = None
    for name in sorted(os.listdir(folder)):
        m = P30_RE.match(name)
        if not m or m.group("client") != client:
            continue
        span = m.group("span")
        if span == period:
            exact = os.path.join(folder, name)
            break
        if covers(span, period) and cover is None:
            cover = os.path.join(folder, name)
    return exact or cover


def covers(span, period):
    if not span:
        return False
    if span == period:
        return True
    parts = span.split(" to ")
    if len(parts) != 2:
        return False
    try:
        return (period_sort_key(parts[0]) <= period_sort_key(period)
                <= period_sort_key(parts[1]))
    except (KeyError, ValueError):
        return False


def source_fingerprint(paths):
    """One SHA-256 over the sorted (name, sha) pairs of the source files."""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(os.path.basename(p).encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(p).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:16]
