"""The run ledger - what has been posted, held, skipped, and why.

SQLite (stdlib), WAL mode, on the LOCAL disk of the machine that posts
(never on Dropbox: sync + SQLite = corruption). One row per client-period
in ``journals``; every state change appended to ``events``.

Write-ahead discipline for posting::

    intent   -> row status 'posting' with the payload fingerprint BEFORE the
                Xero call
    posted   -> row status 'posted' with the ManualJournalID AFTER the call
    crash in between -> next run finds 'posting', and the poster checks Xero
                for a journal with the same narration/date before doing
                anything (the Xero duplicate guard is the true source of
                truth; the ledger is the memory that makes it fast).

A human-readable ``ledger.csv`` export can be written anywhere (Dropbox is
fine for that - it is a copy, not the store).
"""
import csv
import datetime
import json
import os
import socket
import sqlite3

STATUSES = ("shadow", "held", "skipped", "posting", "draft", "posted",
            "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS journals (
    slug TEXT NOT NULL,
    period TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT,
    narration TEXT,
    journal_date TEXT,
    total_debits TEXT,
    paye_due TEXT,
    xero_tenant_id TEXT,
    xero_journal_id TEXT,
    mapping_sha TEXT,
    source_sha TEXT,
    payload_sha TEXT,
    ea TEXT,
    er_nic TEXT,
    idempotency_key TEXT,
    machine TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (slug, period)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    machine TEXT,
    run_id TEXT,
    slug TEXT,
    period TEXT,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    machine TEXT,
    summary TEXT
);
"""


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


class Ledger:
    def __init__(self, path, machine=None):
        self.path = os.path.expanduser(path)
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.machine = machine or socket.gethostname()
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.run_id = None

    def _migrate(self):
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(journals)")}
        for col in ("ea", "er_nic", "idempotency_key"):
            if col not in have:
                self.db.execute(f"ALTER TABLE journals ADD COLUMN {col} TEXT")

    def year_to_date(self, slug, tax_year_periods, exclude_period=None):
        """Sum of EA and ER NIC recorded for the given periods of one client
        (any status except failed/held), for the cumulative EA check."""
        ea = er = 0.0
        for r in self.all_rows():
            if r["slug"] != slug or r["period"] not in tax_year_periods \
                    or r["period"] == exclude_period \
                    or r["status"] in ("failed", "held"):
                continue
            ea += float(r.get("ea") or 0)
            er += float(r.get("er_nic") or 0)
        return round(ea, 2), round(er, 2)

    def close(self):
        self.db.close()

    # ------------------------------------------------------------- runs

    def start_run(self, run_id):
        self.run_id = run_id
        self.db.execute("INSERT OR REPLACE INTO runs (run_id, started_at, "
                        "machine) VALUES (?,?,?)",
                        (run_id, now_iso(), self.machine))
        self.event(kind="run_start")
        return run_id

    def finish_run(self, summary):
        self.db.execute("UPDATE runs SET finished_at=?, summary=? WHERE "
                        "run_id=?", (now_iso(), json.dumps(summary),
                                     self.run_id))
        self.event(kind="run_end", detail=summary)

    def last_run(self):
        row = self.db.execute("SELECT * FROM runs ORDER BY started_at DESC "
                              "LIMIT 1").fetchone()
        return dict(row) if row else None

    # ----------------------------------------------------------- events

    def event(self, *, kind, slug=None, period=None, detail=None):
        self.db.execute(
            "INSERT INTO events (at, machine, run_id, slug, period, kind, "
            "detail) VALUES (?,?,?,?,?,?,?)",
            (now_iso(), self.machine, self.run_id, slug, period, kind,
             json.dumps(detail) if isinstance(detail, (dict, list))
             else detail))

    def events_for(self, slug, period):
        rows = self.db.execute("SELECT * FROM events WHERE slug=? AND "
                               "period=? ORDER BY id", (slug, period))
        return [dict(r) for r in rows]

    # --------------------------------------------------------- journals

    def get(self, slug, period):
        row = self.db.execute("SELECT * FROM journals WHERE slug=? AND "
                              "period=?", (slug, period)).fetchone()
        return dict(row) if row else None

    def upsert(self, slug, period, **fields):
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']}")
        existing = self.get(slug, period)
        ts = now_iso()
        if existing:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE journals SET {cols}, updated_at=? WHERE "
                            "slug=? AND period=?",
                            (*fields.values(), ts, slug, period))
        else:
            fields.setdefault("status", "shadow")
            fields.setdefault("machine", self.machine)
            keys = ["slug", "period", *fields.keys(), "created_at",
                    "updated_at"]
            self.db.execute(
                f"INSERT INTO journals ({', '.join(keys)}) VALUES "
                f"({', '.join('?' for _ in keys)})",
                (slug, period, *fields.values(), ts, ts))
        self.event(kind="journal_" + fields.get("status", "update"),
                   slug=slug, period=period, detail=fields)
        return self.get(slug, period)

    def mark_intent(self, slug, period, **fields):
        """Write-ahead record BEFORE calling Xero."""
        return self.upsert(slug, period, status="posting",
                           machine=self.machine, **fields)

    def mark_posted(self, slug, period, *, xero_journal_id, status="posted",
                    **fields):
        return self.upsert(slug, period, status=status,
                           xero_journal_id=xero_journal_id, **fields)

    def mark_held(self, slug, period, note, **fields):
        return self.upsert(slug, period, status="held", note=note[:2000],
                           **fields)

    def mark_skipped(self, slug, period, note, **fields):
        return self.upsert(slug, period, status="skipped", note=note[:2000],
                           **fields)

    def mark_failed(self, slug, period, note, **fields):
        return self.upsert(slug, period, status="failed", note=note[:2000],
                           **fields)

    def stale_intents(self, older_than_minutes=30):
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(minutes=older_than_minutes)
                  ).replace(microsecond=0).isoformat()
        rows = self.db.execute("SELECT * FROM journals WHERE status='posting' "
                               "AND updated_at < ?", (cutoff,))
        return [dict(r) for r in rows]

    def all_rows(self, status=None):
        if status:
            rows = self.db.execute("SELECT * FROM journals WHERE status=? "
                                   "ORDER BY slug, period", (status,))
        else:
            rows = self.db.execute("SELECT * FROM journals ORDER BY slug, "
                                   "period")
        return [dict(r) for r in rows]

    def export_csv(self, path):
        rows = self.all_rows()
        cols = ["slug", "period", "status", "mode", "narration",
                "journal_date", "total_debits", "paye_due", "ea", "er_nic",
                "xero_tenant_id", "xero_journal_id", "mapping_sha",
                "source_sha", "machine", "note", "created_at", "updated_at"]
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])
        os.replace(tmp, path)
        return len(rows)
