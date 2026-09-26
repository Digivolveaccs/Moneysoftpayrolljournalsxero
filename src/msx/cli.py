"""msx command line.

    python3 -m msx.cli run [--mode shadow|draft|post] [--client SLUG]
                            [--period Mon-YYYY] [--dry-run] [--no-notify]
    python3 -m msx.cli build REPORT... --client SLUG [--out DIR]
    python3 -m msx.cli approve SLUG Mon-YYYY
    python3 -m msx.cli status [--period Mon-YYYY] [--held]
    python3 -m msx.cli ledger clear SLUG Mon-YYYY
    python3 -m msx.cli auth login|tenants|check [--app NAME]
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
        min_period=args.since, notify_enabled=not args.no_notify)
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
        try:
            ts = xero.tenants()
            print(f"ok - token valid, {len(ts)} organisation(s) connected")
        except xero_client.AuthRequired as exc:
            print("AUTH REQUIRED:", exc)
            return 3
    return 0


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


def cmd_doctor(args):
    problems = []
    try:
        cfg = config_mod.load(args.config)
        print("config:", cfg.path)
    except Hold as exc:
        print("CONFIG:", exc)
        return 3
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
            print(f"xero app {app_name}: token ok, {len(ts)} org(s) connected")
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

    p = sub.add_parser("chart", help="print a client org's chart of accounts")
    p.add_argument("slug")
    p.set_defaults(func=cmd_chart)

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
