"""Run reports, holds file and heartbeat - the observability layer.

* ``write_holds_md``: a human/Claude-readable list of everything held, with
  the exact next action, written to ``<out_dir>/HOLDS.md`` after every run.
* ``render_report_html``: the run report (one screenful, phone-readable).
* ``send_missive_report``: emails the report through the Missive API (the
  practice's existing channel; stdlib only). Failure to notify never fails
  the run - it is logged and the report is still on disk.
* ``heartbeat``: touches a file and/or pings a dead-man's-switch URL so
  "the run did not happen" is itself an alert.
"""
import datetime
import html
import json
import os
import urllib.error
import urllib.request

MISSIVE_BASE = "https://public.missiveapp.com/v1"


def write_holds_md(out_dir, summary):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "HOLDS.md")
    lines = [f"# Holds - {summary['run_id']}", "",
             f"Machine: {summary.get('machine')}  Mode default: "
             f"{summary.get('mode')}", ""]
    if not summary["held"]:
        lines.append("Nothing held.")
    for h in summary["held"]:
        lines.append(f"## {h['client']} - {h['period']}")
        lines.append("")
        lines.append(f"Stage: {h.get('stage') or '?'}")
        lines.append("")
        lines.append(h["note"].strip())
        lines.append("")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def render_report_html(summary):
    def esc(x):
        return html.escape(str(x))

    def table(rows, cols):
        if not rows:
            return "<p>none</p>"
        head = "".join(f"<th align=left>{esc(c)}</th>" for c in cols)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(r.get(c, ''))}</td>" for c in cols)
            + "</tr>" for r in rows)
        return (f"<table border=1 cellpadding=3 cellspacing=0><tr>{head}</tr>"
                f"{body}</table>")

    parts = [f"<p><b>Payroll journals run {esc(summary['run_id'])}</b> on "
             f"{esc(summary.get('machine'))} - "
             f"{len(summary['posted'])} posted, {len(summary['draft'])} drafts, "
             f"{len(summary['held'])} held, {len(summary['skipped'])} skipped, "
             f"{len(summary['shadow'])} shadow, {len(summary['pending'])} "
             f"pending</p>"]
    if summary["held"]:
        parts.append("<p><b>Held - needs a human</b></p>")
        parts.append(table(summary["held"],
                           ["client", "period", "stage", "note"]))
    if summary["posted"] or summary["draft"]:
        parts.append("<p><b>Sent to Xero</b></p>")
        parts.append(table(summary["posted"] + summary["draft"],
                           ["client", "period", "outcome", "total_debits",
                            "paye_due", "p30", "xero_journal_id"]))
    if summary["shadow"]:
        parts.append("<p><b>Shadow (built and reconciled, not sent)</b></p>")
        parts.append(table(summary["shadow"],
                           ["client", "period", "total_debits", "paye_due",
                            "p30"]))
    if summary["skipped"]:
        parts.append("<p><b>Skipped</b></p>")
        parts.append(table(summary["skipped"], ["client", "period", "note"]))
    if summary["pending"]:
        parts.append("<p><b>Pending (file still syncing)</b></p>")
        parts.append(table(summary["pending"], ["client", "period", "note"]))
    if summary.get("warnings"):
        parts.append("<p><b>Warnings</b></p><ul>"
                     + "".join(f"<li>{esc(w)}</li>" for w in summary["warnings"])
                     + "</ul>")
    return "<div>" + "".join(parts) + "</div>"


def report_subject(summary):
    d = summary["run_id"][:10]
    return (f"Payroll journals - {d} - {len(summary['posted']) + len(summary['draft'])}"
            f" to Xero, {len(summary['held'])} held")


def send_missive_report(notify_cfg, subject, body_html, *, transport=None):
    token_file = notify_cfg.get("missive_token_file")
    to = notify_cfg.get("report_to")
    if not token_file or not to:
        return {"ok": False, "skipped": "notify.missive_token_file / "
                                        "report_to not configured"}
    try:
        with open(os.path.expanduser(token_file), encoding="utf-8") as fh:
            token = fh.read().strip()
    except FileNotFoundError:
        return {"ok": False, "error": f"missing {token_file}"}
    draft = {"subject": subject, "body": body_html,
             "from_field": {"address": notify_cfg.get("from_address"),
                            "name": notify_cfg.get("from_name", "Digivolve Payroll")},
             "to_fields": [{"address": to}], "send": True}
    if notify_cfg.get("organization"):
        draft["organization"] = notify_cfg["organization"]
    data = json.dumps({"drafts": draft}).encode("utf-8")
    req = urllib.request.Request(
        MISSIVE_BASE + "/drafts", data=data, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    opener = transport or urllib.request.urlopen
    try:
        with opener(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        return {"ok": True, "draft_id": (payload.get("drafts") or {}).get("id")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}: "
                                      f"{exc.read()[:200].decode(errors='replace')}"}
    except Exception as exc:  # network
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def heartbeat(notify_cfg, summary, *, opener=None):
    out = {}
    path = notify_cfg.get("heartbeat_file")
    if path:
        p = os.path.expanduser(path)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"run_id": summary["run_id"],
                       "at": datetime.datetime.now().isoformat(timespec="seconds"),
                       "held": len(summary["held"]),
                       "posted": len(summary["posted"]) + len(summary["draft"])},
                      fh)
        out["file"] = p
    url = notify_cfg.get("heartbeat_url")
    if url:
        if summary["held"] or summary.get("failed"):
            url = url.rstrip("/") + "/fail"       # healthchecks.io convention
        try:
            with (opener or urllib.request.urlopen)(url, timeout=15) as resp:
                out["url_status"] = resp.status
        except Exception as exc:
            out["url_error"] = f"{type(exc).__name__}: {exc}"
    return out
