"""One run = discover filed reports -> build -> reconcile -> post -> report.

Deterministic and idempotent: running it ten times in a row does ten
discoveries and at most one Xero write per client-period. Every client is
finished (posted / draft / shadow / held / skipped / pending) before the
next is started; a failure in one client never stops the others.

Outcome buckets in the run summary:

    posted   created POSTED in Xero this run
    draft    created DRAFT in Xero this run (or adopted after a crash)
    shadow   built + reconciled, nothing sent (mode shadow)
    skipped  already in Xero / already recorded / nil payroll
    held     needs a human - exact note in HOLDS.md and the report
    pending  a source file is still being written by Dropbox; next run
    failed   Xero refused the write (also held)
"""
import datetime
import json
import os
import traceback

from . import journal_builder, mapping as mapping_mod, notify, p30 as p30_mod
from . import poster, sources, state, summary_parser
from .errors import Hold, Skip
from .xero_client import AuthRequired, XeroError

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def new_run_id(clock=None):
    now = (clock or datetime.datetime.now)()
    return now.strftime("%Y-%m-%dT%H%M%S")


def tax_year_periods(tax_year):
    """'2026-27' -> ['Apr-2026', ..., 'Mar-2027']."""
    start = int(tax_year.split("-")[0])
    out = []
    for i in range(12):
        m = (3 + i) % 12          # Apr = index 3
        y = start if m >= 3 else start + 1
        out.append(f"{MONTHS[m]}-{y}")
    return out


def current_tax_year_start(today=None):
    today = today or datetime.date.today()
    start = today.year if (today.month, today.day) >= (4, 6) else today.year - 1
    return f"Apr-{start}"


class RunContext:
    def __init__(self, cfg, *, ledger, mappings, xero_factory, log,
                 mode_override=None, dry_run=False, clock=None):
        self.cfg = cfg
        self.ledger = ledger
        self.mappings = mappings
        self.xero_factory = xero_factory
        self.log = log
        self.mode_override = mode_override
        self.dry_run = dry_run
        self.clock = clock or datetime.datetime.now
        self._xero = {}
        self.xero_failures = 0
        self.xero_down = False

    def xero_for(self, mapping):
        app = (mapping.cfg.get("xero") or {}).get("app") or "default"
        if app not in self._xero:
            self._xero[app] = self.xero_factory(app)
        return self._xero[app]


def _entry(client, period, **kw):
    d = {"client": client, "period": period}
    d.update(kw)
    return d


def process_group(ctx, client, period, files, summary):
    """One client-period, end to end. Appends to the summary buckets."""
    log = ctx.log
    slug = None
    try:
        m = mapping_mod.find_for_report(ctx.mappings, client)
        slug = m.slug
        m.ensure_valid()
        paths = [f["path"] for f in files]
        for p in paths:
            if not sources.stable(p, settle_seconds=ctx.cfg.settle_seconds,
                                  sample_wait=ctx.cfg.sample_wait):
                summary["pending"].append(_entry(client, period,
                                                 note=f"{os.path.basename(p)} "
                                                 "not stable yet"))
                return
            if p.lower().endswith(".pdf") and not sources.is_pdf(p):
                raise Hold(f"{os.path.basename(p)} is not a readable PDF "
                           "(online-only Dropbox placeholder? re-download it)",
                           stage="discover")
        stats = {p: (os.stat(p).st_size, os.stat(p).st_mtime) for p in paths}
        row = ctx.ledger.get(slug, period)
        payroll = summary_parser.parse_files(paths)
        if not m.matches_employer(payroll.get("client") or client):
            raise Hold(f"report header says '{payroll.get('client')}' but the "
                       f"file is filed under '{client}' - wrong folder?",
                       stage="parse")
        notes = summary_parser.completeness_notes(payroll)
        journal = journal_builder.build(payroll, m.cfg)
        if notes:
            summary["warnings"].extend(f"{client} {period}: {n}" for n in notes)

        # P30 cross-check (independent report)
        p30_status, p30_msg = "missing", "no P30 found"
        p30_path = sources.find_p30(files[0]["folder"], client, period)
        if p30_path and sources.stable(p30_path,
                                       settle_seconds=ctx.cfg.settle_seconds,
                                       sample_wait=ctx.cfg.sample_wait):
            p30_status, p30_msg = p30_mod.cross_check(
                p30_mod.parse_p30_file(p30_path), journal)
        if p30_status == "mismatch":
            raise Hold(f"P30 cross-check failed: {p30_msg}", stage="reconcile")

        # write the build artefacts (CSV import file + JSON) every time
        out_dir = os.path.join(ctx.cfg.out_dir, slug, period)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "journal.csv"), "w", encoding="utf-8",
                  newline="") as fh:
            fh.write(journal.to_csv(m.cfg.get("tax_rate", "No VAT")))
        with open(os.path.join(out_dir, "journal.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"journal": journal.as_dict(), "payroll": payroll,
                       "p30": {"status": p30_status, "message": p30_msg},
                       "mapping_sha": m.fingerprint,
                       "source_sha": sources.source_fingerprint(paths)},
                      fh, indent=1, default=str)

        # figures changed after we posted? (payroll re-run) -> human
        mode = ctx.mode_override or m.mode
        payload_sha = poster.payload_fingerprint(journal.to_api_payload(
            status="POSTED" if mode == "post" else "DRAFT"))
        if row and row.get("xero_journal_id") and row.get("payload_sha") \
                and row["payload_sha"] != payload_sha \
                and row.get("mode") == mode:
            raise Hold(f"the Employer's Summary now builds a different journal "
                       f"(Dr {journal.total_debits}) from the one already in "
                       f"Xero as {row['xero_journal_id']} (Dr "
                       f"{row.get('total_debits')}) - the payroll was re-run "
                       "after posting. Post a correcting journal by hand, or "
                       "void the Xero journal and clear the ledger row "
                       f"(msx ledger clear {slug} {period})", stage="rerun")

        # year-to-date Employment Allowance sanity (a mid-year claim can
        # exceed one month's ER NIC, but never the annual maximum)
        ytd_periods = tax_year_periods(journal.meta["tax_year"])
        ea_ytd, er_ytd = ctx.ledger.year_to_date(slug, ytd_periods,
                                                 exclude_period=period)
        ea_max = float(ctx.cfg.data.get("ea_annual_max", 10500))
        if float(journal.meta["ea"]) + ea_ytd > ea_max + 0.005:
            raise Hold(f"employment allowance {journal.meta['ea']} this month "
                       f"plus {ea_ytd:.2f} already recorded this tax year "
                       f"exceeds the annual maximum {ea_max:.2f} - check the "
                       "EPS claim / Analysis > Employer's NIC Allowance in "
                       "Moneysoft", stage="reconcile")
        for w in journal.meta.get("warnings", []):
            summary["warnings"].append(f"{client} {period}: {w}")
        base = dict(client=client, period=period,
                    total_debits=f"{journal.total_debits:.2f}",
                    paye_due=f"{journal.meta['paye_due']:.2f}",
                    p30=p30_status)
        ctx.ledger.upsert(slug, period, ea=f"{journal.meta['ea']:.2f}",
                          er_nic=f"{journal.meta['er_nic']:.2f}")
        if mode != "shadow" and ctx.xero_down:
            summary["pending"].append(_entry(client, period,
                                             note="Xero unreachable earlier in "
                                             "this run - deferred"))
            return
        # the source must not have changed between build and post
        if mode != "shadow":
            for pth, st0 in stats.items():
                try:
                    st1 = os.stat(pth)
                except FileNotFoundError:
                    st1 = None
                if not st1 or (st1.st_size, st1.st_mtime) != st0:
                    summary["pending"].append(_entry(client, period,
                                                     note=f"{os.path.basename(pth)} "
                                                     "changed during the run"))
                    return
        xero = ctx.xero_for(m) if mode != "shadow" else None
        try:
            res = poster.post_journal(journal, m, ctx.ledger, xero, mode=mode,
                                      dry_run=ctx.dry_run)
            ctx.xero_failures = 0
        except Skip as exc:
            if row and row.get("status") in ("posted", "draft") \
                    and exc.reason == "ledger":
                return                         # quietly done already
            ctx.ledger.mark_skipped(slug, period, str(exc), mode=mode)
            summary["skipped"].append(_entry(client, period, note=str(exc)))
            return
        bucket = {"shadow": "shadow", "draft": "draft", "posted": "posted",
                  "dry-run": "skipped"}[res.outcome]
        if res.outcome == "shadow" and row and row.get("status") == "shadow" \
                and row.get("payload_sha") == payload_sha:
            summary["already_shadow"] += 1
            return
        summary[bucket].append(_entry(**base, outcome=res.outcome,
                                      xero_journal_id=res.xero_journal_id,
                                      note=res.message))
        log(f"{client} {period}: {res.outcome} - {res.message}")
    except Skip as exc:
        if slug:
            ctx.ledger.mark_skipped(slug, period, str(exc))
        summary["skipped"].append(_entry(client, period, note=str(exc)))
        log(f"{client} {period}: skipped - {exc}")
    except Hold as exc:
        note = str(exc)
        if exc.stage == "post-retry":
            ctx.xero_failures += 1
            if ctx.xero_failures >= 3:
                ctx.xero_down = True
                summary["warnings"].append("Xero unreachable three times in a "
                                           "row - remaining posts deferred to "
                                           "the next run")
            summary["pending"].append(_entry(client, period, note=note))
            log(f"{client} {period}: deferred - {note.splitlines()[0]}")
            return
        if slug:
            prev = ctx.ledger.get(slug, period)
            if prev and prev.get("status") in ("posted", "draft") \
                    and exc.stage != "rerun":
                pass                           # keep the posted state
            else:
                ctx.ledger.mark_held(slug, period, note)
        summary["held"].append(_entry(client, period, stage=exc.stage,
                                      note=note))
        log(f"{client} {period}: HELD ({exc.stage}) - {note.splitlines()[0]}")
    except (XeroError, AuthRequired) as exc:
        # transport / auth trouble outside the poster's own handling
        ctx.xero_failures += 1
        if ctx.xero_failures >= 3:
            ctx.xero_down = True
        note = f"Xero error: {exc}"
        if isinstance(exc, AuthRequired):
            summary["warnings"].append(f"XERO AUTH REQUIRED: {exc}")
            ctx.xero_down = True
        summary["pending"].append(_entry(client, period, note=note))
        log(f"{client} {period}: deferred - {note}")
    except Exception as exc:  # a bug, never silent
        note = f"unexpected error {type(exc).__name__}: {exc}\n" \
               + traceback.format_exc(limit=3)
        if slug:
            ctx.ledger.mark_held(slug, period, note)
        summary["held"].append(_entry(client, period, stage="bug", note=note))
        log(f"{client} {period}: ERROR - {exc}")


def keepalive(cfg, ledger, xero_factory, summary, log, every_days=7):
    """Touch each Xero app's token at least weekly so the rotating refresh
    token never reaches Xero's 60-day unused expiry. A failure here is a
    warning, never a stop, and never disables anything."""
    apps = sorted(set(["default"] + list((cfg.xero.get("apps") or {}).keys())))
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=every_days)).isoformat()
    for app in apps:
        if app.startswith("_"):
            continue
        try:
            cfg.xero_app(app)
        except Hold:
            continue                        # app not configured
        last = ledger.db.execute(
            "SELECT at FROM events WHERE kind='xero_keepalive' AND detail=? "
            "ORDER BY id DESC LIMIT 1", (app,)).fetchone()
        if last and last["at"] > cutoff:
            continue
        try:
            n = len(xero_factory(app).tenants())
            ledger.event(kind="xero_keepalive", detail=app)
            log(f"xero app {app}: token refreshed, {n} org(s) connected")
        except Exception as exc:
            summary["warnings"].append(f"xero app {app}: keep-alive failed - "
                                       f"{type(exc).__name__}: {exc}")


def standby_guard(cfg, mode_override, take_over, summary, clock):
    """A standby machine never posts unless told to take over, or unless the
    primary's heartbeat (a copy synced via Dropbox) is older than 24 h."""
    role = (cfg.data.get("role") or "primary").lower()
    if role != "standby":
        return mode_override
    if take_over:
        summary["warnings"].append("STANDBY machine posting under --take-over")
        return mode_override
    hb = cfg.path_of("primary_heartbeat_file")
    age_h = None
    if hb and os.path.exists(hb):
        now = (clock or datetime.datetime.now)().timestamp()
        age_h = (now - os.path.getmtime(hb)) / 3600
    if age_h is not None and age_h > 24:
        summary["warnings"].append(f"primary heartbeat is {age_h:.0f}h old - "
                                   "standby is posting")
        return mode_override
    summary["warnings"].append("standby machine: shadow only (primary alive "
                               "or unknown); use --take-over to post")
    return "shadow"


def run_once(cfg, *, xero_factory, log=print, mode_override=None,
             dry_run=False, only_clients=None, only_periods=None,
             min_period=None, notify_enabled=True, clock=None,
             take_over=False):
    ledger = state.Ledger(cfg.state_db, machine=cfg.machine_name)
    run_id = new_run_id(clock)
    ledger.start_run(run_id)
    if notify_enabled:
        notify.heartbeat_start(cfg.notify)
    summary = {"run_id": run_id, "machine": ledger.machine,
               "mode": mode_override or "per-client", "posted": [],
               "draft": [], "shadow": [], "skipped": [], "held": [],
               "pending": [], "failed": [], "warnings": [],
               "already_shadow": 0, "ignored_old": 0}
    try:
        mode_override = standby_guard(cfg, mode_override, take_over, summary,
                                      clock)
        mappings = mapping_mod.load_all(cfg.clients_dir)
        ctx = RunContext(cfg, ledger=ledger, mappings=mappings,
                         xero_factory=xero_factory, log=log,
                         mode_override=mode_override, dry_run=dry_run,
                         clock=clock)
        for m in mappings.values():
            if m.problems:
                summary["warnings"].append(f"mapping {m.path}: "
                                           + "; ".join(m.problems))
        rows = sources.find_summaries(cfg.pdf_root, periods=only_periods,
                                      clients=only_clients)
        for r in rows:
            if r["kind"] == "conflict":
                summary["held"].append(_entry(r["name"], "?", stage="discover",
                                              note=f"Dropbox conflicted/duplicate "
                                              f"copy: {r['path']} - a human must "
                                              "decide which file is real and "
                                              "remove the other"))
        floor = min_period or current_tax_year_start(
            (clock or datetime.datetime.now)().date())
        for (client, period), files in sources.group_summaries(rows).items():
            if sources.period_sort_key(period) < sources.period_sort_key(floor):
                summary["ignored_old"] += 1
                continue
            process_group(ctx, client, period, files, summary)
        keepalive(cfg, ledger, xero_factory, summary, log)
        for stale in ledger.stale_intents(older_than_minutes=30):
            summary["warnings"].append(
                f"{stale['slug']} {stale['period']}: a previous run died "
                "mid-post; it will be reconciled against Xero next time the "
                "report is processed")
    except Hold as exc:
        summary["held"].append(_entry("(run)", "-", stage=exc.stage,
                                      note=str(exc)))
        log(f"RUN HELD: {exc}")
    finally:
        counts = {k: len(v) for k, v in summary.items() if isinstance(v, list)}
        ledger.finish_run(counts)
        if cfg.ledger_csv:
            try:
                ledger.export_csv(cfg.ledger_csv)
            except OSError as exc:
                summary["warnings"].append(f"ledger CSV export failed: {exc}")
        ledger.close()
    os.makedirs(cfg.out_dir, exist_ok=True)
    notify.write_holds_md(cfg.out_dir, summary)
    html = notify.render_report_html(summary)
    with open(os.path.join(cfg.out_dir, "last_run_report.html"), "w",
              encoding="utf-8") as fh:
        fh.write(html)
    with open(os.path.join(cfg.out_dir, "last_run_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, default=str)
    if notify_enabled:
        actionable = summary["held"] or summary["posted"] or summary["draft"] \
            or summary["failed"] or summary["pending"]
        if actionable or cfg.notify.get("report_always"):
            res = notify.send_missive_report(cfg.notify,
                                             notify.report_subject(summary),
                                             html)
            summary["notify"] = res
            if not res.get("ok"):
                log(f"report not emailed: {res}")
        summary["heartbeat"] = notify.heartbeat(cfg.notify, summary)
    return summary
