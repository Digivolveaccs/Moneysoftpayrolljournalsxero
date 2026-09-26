"""msx command line.

    python3 -m msx.cli run [--mode shadow|draft|post] [--client SLUG]
                            [--period Mon-YYYY] [--dry-run] [--no-notify]
    python3 -m msx.cli build REPORT... --client SLUG [--out DIR]
    python3 -m msx.cli approve SLUG Mon-YYYY
    python3 -m msx.cli status [--period Mon-YYYY] [--held]
    python3 -m msx.cli ledger clear SLUG Mon-YYYY
    python3 -m msx.cli auth login|tenants|check [--app NAME]
    python3 -m msx.cli onboard REPORT... [--org NAME]   # propose a mapping
    python3 -m msx.cli recon [--period Mon-YYYY]        # ledger vs Xero
    python3 -m msx.cli chart SLUG            # dump the org's chart of accounts
    python3 -m msx.cli doctor                # config, folders, mappings, tools

Exit codes: 0 ok / nothing to do, 2 something held, 3 configuration error.
"""
import argparse
import json
import os
import shutil
import sys

from . import config as config_mod
from . import journal_builder, mapping as mapping_mod, poster, runner, state
from . import summary_parser
from . import xero_client
from .errors import Hold, Skip


def make_xero_factory(cfg):
    def factory(app_name):
        app = cfg.xero_app(app_name)
        if app.get("token_store") == "keychain":
            store = xero_client.KeychainTokenStore(
                service=f"digivolve-msx-xero-{app_name}")
        else:
            store = xero_client.FileTokenStore(app["token_file"])
        scopes = app.get("scopes") or cfg.xero.get("scopes") \
            or xero_client.DEFAULT_SCOPES
        return xero_client.XeroClient(client_id=app["client_id"],
                                      client_secret=app.get("client_secret"),
                                      token_store=store, scopes=scopes,
                                      redirect_uri=app.get("redirect_uri"),
                                      log=lambda m: print("  xero:", m))
    return factory


def cmd_run(args):
    cfg = config_mod.load(args.config)
    summary = runner.run_once(
        cfg, xero_factory=make_xero_factory(cfg), mode_override=args.mode,
        dry_run=args.dry_run, only_clients=args.client_name,
        only_periods=[args.period] if args.period else None,
        min_period=args.since, notify_enabled=not args.no_notify,
        take_over=args.take_over)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                      for k, v in summary.items()
                      if k not in ("warnings",)}, indent=1, default=str))
    for w in summary["warnings"]:
        print("warning:", w)
    if summary["held"]:
        print(f"\n{len(summary['held'])} held - see "
              f"{os.path.join(cfg.out_dir, 'HOLDS.md')}")
        return 2
    return 0


def cmd_build(args):
    if args.config or os.path.exists(os.path.expanduser(config_mod.DEFAULT_PATH)):
        cfg = config_mod.load(args.config)
        mappings = mapping_mod.load_all(cfg.clients_dir)
    else:
        here = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        mappings = mapping_mod.load_all(os.path.join(here, "clients"))
    payroll = summary_parser.parse_files(args.reports)
    if args.client:
        m = mappings[args.client]
    else:
        m = mapping_mod.find_for_report(mappings, payroll["client"])
    m.ensure_valid()
    try:
        j = journal_builder.build(payroll, m.cfg, date_override=args.date,
                                  allow_placeholders=args.allow_placeholders)
    except Skip as exc:
        print("SKIP:", exc)
        return 0
    out = args.out or "."
    os.makedirs(out, exist_ok=True)
    csv_path = os.path.join(out, f"{m.slug} - {j.period} - Xero payroll journal.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(j.to_csv(m.cfg.get("tax_rate", "No VAT")))
    for n in summary_parser.completeness_notes(payroll):
        print("note:", n)
    s = j.summary()
    print(f"{s['client']} - {j.narration} dated {j.date}")
    print(f"lines {s['lines']}  Dr = Cr = {s['total_debits']}")
    print(f"gross {s['gross']}  dividends {s['dividends']}  ER NIC {s['er_nic']}")
    print(f"PAYE control {s['paye_pre_ea']} less EA {s['employment_allowance']} "
          f"= {s['paye_due']} due to HMRC")
    print(f"pensions {s['pensions']}  net wages {s['net_wages']}")
    print("wrote", csv_path)
    if j.meta["placeholders"]:
        print("WARNING placeholder codes present - do not import:",
              j.meta["placeholders"])
    return 0


def cmd_approve(args):
    cfg = config_mod.load(args.config)
    mappings = mapping_mod.load_all(cfg.clients_dir)
    m = mappings[args.slug]
    led = state.Ledger(cfg.state_db, machine=cfg.machine_name)
    xero = make_xero_factory(cfg)((m.cfg.get("xero") or {}).get("app", "default"))
    try:
        j = poster.approve(args.slug, args.period, led, xero)
        print("posted:", j.get("ManualJournalID"), j.get("Status"))
    except Skip as exc:
        print("skip:", exc)
    finally:
        led.close()
    return 0


def cmd_status(args):
    cfg = config_mod.load(args.config)
    led = state.Ledger(cfg.state_db, machine=cfg.machine_name)
    rows = led.all_rows(status="held" if args.held else None)
    if args.period:
        rows = [r for r in rows if r["period"] == args.period]
    for r in rows:
        print(f"{r['status']:<8} {r['slug']:<40} {r['period']:<9} "
              f"{r.get('total_debits') or '':>10} {r.get('xero_journal_id') or ''}"
              f"  {(r.get('note') or '').splitlines()[0][:80] if r.get('note') else ''}")
    last = led.last_run()
    if last:
        print(f"\nlast run {last['run_id']} on {last['machine']}: "
              f"{last.get('summary')}")
    led.close()
    return 0


def cmd_ledger(args):
    cfg = config_mod.load(args.config)
    led = state.Ledger(cfg.state_db, machine=cfg.machine_name)
    if args.action == "clear":
        row = led.get(args.slug, args.period)
        if not row:
            print("no such row")
            return 0
        led.event(kind="ledger_clear", slug=args.slug, period=args.period,
                  detail=row)
        led.db.execute("DELETE FROM journals WHERE slug=? AND period=?",
                       (args.slug, args.period))
        print("cleared", args.slug, args.period, "(was", row["status"], ")")
    elif args.action == "export":
        n = led.export_csv(args.path)
        print("exported", n, "rows to", args.path)
    led.close()
    return 0


def cmd_auth(args):
    cfg = config_mod.load(args.config)
    xero = make_xero_factory(cfg)(args.app)
    if args.action == "login":
        tenants = xero.login()
        print(f"connected {len(tenants)} organisation(s):")
        for t in tenants:
            print(f"  {t['tenant_id']}  {t['name']}")
        if len(tenants) >= 25:
            print("NOTE: this app is at Xero's 25-tenant limit for uncertified "
                  "apps; register another app for further clients "
                  "(xero.apps.<name>) or certify the app.")
    elif args.action == "tenants":
        for t in xero.tenants():
            print(f"  {t['tenant_id']}  {t['name']}")
    elif args.action == "check":
        st = xero.token_status()
        try:
            ts = xero.tenants()
            st = xero.token_status()
            print(f"ok - token valid, {len(ts)} organisation(s) connected; "
                  f"refresh token expires in {st.get('refresh_expires_in_days')} "
                  "days if unused")
        except xero_client.AuthRequired as exc:
            print("AUTH REQUIRED:", exc)
            return 3
    return 0


def cmd_onboard(args):
    """Propose clients/<slug>.json from the client's own Xero history."""
    from . import onboard
    cfg = config_mod.load(args.config)
    payroll = summary_parser.parse_files(args.reports)
    stub = onboard.stub_for(payroll, org_name=args.org)
    if args.slug:
        stub["slug"] = args.slug
    xero = make_xero_factory(cfg)(args.app)
    m, report = onboard.propose(payroll, xero, stub, months=args.months)
    m["xero"]["app"] = args.app
    print(f"org: {report['tenant']['name']} ({report['tenant']['tenant_id']})")
    print(f"wages journals seen in the last {args.months} months: "
          f"{report['journals_seen']}")
    for ev in report["evidence"][:40]:
        print("  ", ev)
    if report["unresolved_lines"]:
        print(f"{len(report['unresolved_lines'])} journal line(s) could not be "
              "classified (shown so a human can decide):")
        for c in report["unresolved_lines"][:20]:
            print(f"   {c['code']:>8} {c['amount']:>10.2f} {c['description'][:70]}")
    if report["placeholders"]:
        print("placeholders to fill before leaving shadow mode:",
              ", ".join(report["placeholders"]))
    try:
        path = onboard.write_mapping(m, cfg.clients_dir, overwrite=args.overwrite)
    except FileExistsError as exc:
        print(f"mapping already exists: {exc} (use --overwrite to replace)")
        return 2
    print("wrote", path, "(mode shadow)")
    return 0


def cmd_recon(args):
    from . import recon
    cfg = config_mod.load(args.config)
    mappings = mapping_mod.load_all(cfg.clients_dir)
    if args.client:
        mappings = {k: v for k, v in mappings.items() if k == args.client}
    led = state.Ledger(cfg.state_db, machine=cfg.machine_name)
    factory = make_xero_factory(cfg)
    cache = {}

    def xero_for(m):
        app = (m.cfg.get("xero") or {}).get("app") or "default"
        if app not in cache:
            cache[app] = factory(app)
        return cache[app]

    periods = args.period or [runner.current_tax_year_start()]
    if not args.period:
        # every month from the tax-year start to today (or the last N)
        import datetime as _dt
        start = _dt.datetime.strptime(periods[0], "%b-%Y")
        now = _dt.datetime.now()
        periods = []
        while (start.year, start.month) <= (now.year, now.month):
            periods.append(start.strftime("%b-%Y"))
            start = (start.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        if args.last:
            periods = periods[-args.last:]
    findings = recon.sweep(mappings, led, xero_for, periods=periods)
    led.event(kind="recon", detail={"periods": periods, "findings": len(findings)})
    led.close()
    print(recon.render(findings))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, indent=1)
    return 2 if findings else 0


def cmd_chart(args):
    cfg = config_mod.load(args.config)
    mappings = mapping_mod.load_all(cfg.clients_dir)
    m = mappings[args.slug]
    xero = make_xero_factory(cfg)((m.cfg.get("xero") or {}).get("app", "default"))
    tenant = poster.resolve_tenant(m, xero.tenants())
    org = xero.organisation(tenant["tenant_id"])
    print(f"{org['name']}  lock dates: period {org.get('period_lock_date')} "
          f"year-end {org.get('end_of_year_lock_date')}")
    for code, a in sorted(xero.accounts(tenant["tenant_id"]).items()):
        flag = "" if a.get("status") == "ACTIVE" else f" [{a.get('status')}]"
        sysa = f" (system {a['system_account']})" if a.get("system_account") else ""
        print(f"  {code:<10} {a.get('type'):<14} {a.get('name')}{flag}{sysa}")
    return 0


def cmd_sync_dropbox(args):
    """Cloud fallback: mirror new report files from a Dropbox app folder
    into the local pdf_root, then (optionally) run."""
    from . import dropbox_sync
    cfg = config_mod.load(args.config)
    dcfg = cfg.data.get("dropbox")
    if not dcfg:
        print("config has no 'dropbox' block (see msx/dropbox_sync.py)")
        return 3
    r = dropbox_sync.sync(dcfg, cfg.pdf_root, log=print)
    print(f"downloaded {r['downloaded']}, removed {r['deleted']}, cursor stored")
    if args.then_run:
        return cmd_run(args)
    return 0


def cmd_doctor(args):
    problems = []
    try:
        cfg = config_mod.load(args.config)
        print("config:", cfg.path)
    except Hold as exc:
        print("CONFIG:", exc)
        return 3
    role = (cfg.data.get("role") or "primary").lower()
    print(f"role: {role}" + ("" if "role" in cfg.data else
                             " (default - set \"role\" explicitly on every "
                             "machine: primary on ONE, standby on the rest)"))
    for label, p in (("pdf_root", cfg.pdf_root), ("clients_dir", cfg.clients_dir)):
        print(f"{label}: {p} -> {'ok' if os.path.isdir(p) else 'MISSING'}")
        if not os.path.isdir(p):
            problems.append(label)
    print("pdftotext:", shutil.which("pdftotext") or "not found (brew install poppler)")
    try:
        import pypdf  # noqa: F401
        print("pypdf: ok")
    except Exception:
        print("pypdf: not installed (fallback only; pip3 install --user pypdf)")
    try:
        mappings = mapping_mod.load_all(cfg.clients_dir)
        for m in mappings.values():
            ph = mapping_mod.placeholders(m.cfg)
            print(f"mapping {m.slug}: mode={m.mode} "
                  f"{'ok' if not m.problems else 'PROBLEMS: ' + '; '.join(m.problems)}"
                  f"{' placeholders: ' + ', '.join(ph) if ph else ''}")
            problems.extend(m.problems)
    except Hold as exc:
        print("MAPPINGS:", exc)
        problems.append("mappings")
    for app_name in sorted(set(["default"] + list((cfg.xero.get("apps") or {}).keys()))):
        try:
            xero = make_xero_factory(cfg)(app_name)
            ts = xero.tenants()
            st = xero.token_status()
            days = st.get("refresh_expires_in_days")
            flag = " - WARNING: re-authorise soon" if days is not None and days < 7 else ""
            print(f"xero app {app_name}: token ok, {len(ts)} org(s) connected, "
                  f"refresh token good for {days} days unused{flag}")
            if flag:
                problems.append("xero-token-expiring")
        except Hold as exc:
            print(f"xero app {app_name}: {exc}")
        except xero_client.AuthRequired as exc:
            print(f"xero app {app_name}: AUTH REQUIRED - {exc}")
            problems.append("xero-auth")
        except Exception as exc:
            print(f"xero app {app_name}: {type(exc).__name__}: {exc}")
    led = state.Ledger(cfg.state_db, machine=cfg.machine_name)
    last = led.last_run()
    print("ledger:", cfg.state_db, "- last run",
          last["run_id"] if last else "never", "on",
          last["machine"] if last else "-")
    led.close()
    print("DOCTOR:", "ok" if not problems else "problems: " + ", ".join(problems))
    return 0 if not problems else 3


def main(argv=None):
    ap = argparse.ArgumentParser(prog="msx", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config path (default ~/.config/msx/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="discover, build, reconcile, post")
    p.add_argument("--mode", choices=("shadow", "draft", "post"))
    p.add_argument("--client-name", action="append",
                   help="only this client (as filed on disk); repeatable")
    p.add_argument("--period", help="only this period, e.g. Aug-2026")
    p.add_argument("--since", help="ignore periods before this (Mon-YYYY); "
                   "default = start of the current tax year")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--take-over", action="store_true",
                   help="on a standby machine: post even though the primary "
                        "may be alive")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("build", help="parse report(s) and write the CSV only")
    p.add_argument("reports", nargs="+")
    p.add_argument("--client", help="mapping slug (default: match the report)")
    p.add_argument("--out")
    p.add_argument("--date", help="override journal date dd/mm/yyyy")
    p.add_argument("--allow-placeholders", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("approve", help="promote a DRAFT the pipeline created")
    p.add_argument("slug")
    p.add_argument("period")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("status")
    p.add_argument("--period")
    p.add_argument("--held", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("ledger")
    p.add_argument("action", choices=("clear", "export"))
    p.add_argument("slug", nargs="?")
    p.add_argument("period", nargs="?")
    p.add_argument("--path", default="ledger.csv")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("auth")
    p.add_argument("action", choices=("login", "tenants", "check"))
    p.add_argument("--app", default="default")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("onboard", help="propose a client mapping from the "
                       "client's own posted wages journals in Xero")
    p.add_argument("reports", nargs="+", help="the client's latest Employer's "
                   "Summary (PDF/txt), all layouts")
    p.add_argument("--org", help="Xero organisation name if it differs from "
                   "the report header")
    p.add_argument("--slug")
    p.add_argument("--app", default="default")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("recon", help="read-only sweep: ledger vs Xero "
                       "(duplicates, missing, mismatches, orphans, aging drafts)")
    p.add_argument("--period", action="append", help="Mon-YYYY; repeatable; "
                   "default = every month of the current tax year")
    p.add_argument("--client", help="one mapping slug")
    p.add_argument("--last", type=int, help="only the last N months of the "
                   "tax year (e.g. 2 for a daily sweep)")
    p.add_argument("--json", help="also write findings to this file")
    p.set_defaults(func=cmd_recon)

    p = sub.add_parser("chart", help="print a client org's chart of accounts")
    p.add_argument("slug")
    p.set_defaults(func=cmd_chart)

    p = sub.add_parser("sync-dropbox", help="cloud fallback: mirror the "
                       "Dropbox app folder into pdf_root via the API")
    p.add_argument("--then-run", action="store_true")
    p.add_argument("--mode", choices=("shadow", "draft", "post"))
    p.add_argument("--client-name", action="append")
    p.add_argument("--period")
    p.add_argument("--since")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--take-over", action="store_true")
    p.set_defaults(func=cmd_sync_dropbox)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except Hold as exc:
        print("HOLD:", exc, file=sys.stderr)
        return 2
    except xero_client.AuthRequired as exc:
        print("AUTH REQUIRED:", exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
