"""Parse a Moneysoft Payroll Manager Employer's Summary into a payroll dict.

Proven lineage: this is the parser from Digivolve's browns-payroll-journal
skill (verified against live Moneysoft output for Browns Garage Apr-2026 and
Jul-2026), converted from a script into an importable module that raises
``Hold`` instead of exiting.

Inputs handled
--------------
* **Fixed-width** text, as produced by ``pdftotext -layout`` (or pypdf's
  layout mode) from the Employer's Summary PDF. Column boundaries are taken
  from the report's own Total row, every value is matched to a column by its
  centre, and each column is re-summed against the Total row - a misread
  column cannot pass silently.
* **Tab-delimited** text, as produced by copying the report out of the PDF
  viewer into chat.

Pass all the layouts you have (Medium, Additions, Deductions) - in one file or
several - and rows are merged by employee name. Layouts that disagree about
the same figure are a hard Hold. A column title the parser does not know is
a hard Hold: a silently dropped column could hide a deduction.

Output shape (see docs/architecture/payroll-json.md)::

    {"client": "...", "period": "Apr-2026", "tax_year": "2026-27",
     "employees": [{"name": ..., "total_payments": ..., ...}],
     "report_totals": {...}, "employer_totals": {...},
     "layouts_seen": ["medium", "additions", "deductions"]}
"""
import os
import re
import subprocess
import tempfile

from .errors import Hold

# canonical key <- normalised column title
COLUMNS = {
    "employee": "name",
    "tax code": "tax_code",
    "total payments": "total_payments",
    "basic pay": "basic",
    "total hourly pay": "hourly",
    "total hours": "hours",
    "total additions": "additions",
    "additions": "additions",
    "holiday pay": "holiday",
    "sick pay": "sick",
    "parenting pay": "parenting",
    "rounding addition": "rounding_addition",
    "dividend": "dividend",
    "total deductions": "total_deductions",
    "deductions": "deductions_breakdown_total",
    "over payment": "overpayment",
    "overpayment": "overpayment",
    "total employee pension": "ee_pension",
    "pension employee contribution": "ee_pension",
    "student loan repayment": "student_loan",
    "postgrad loan repayment": "postgrad_loan",
    "employee nic": "ee_nic",
    "tax deducted": "tax",
    "attachments": "attachments",
    "payroll giving": "payroll_giving",
    "childcare from employee": "childcare",
    "loans repayment": "loans_repayment",
    "rounding deduction": "rounding_deduction",
    "dividend tax": "dividend_tax",
    "net pay": "net",
    "employer nic": "er_nic",
    "total employer pension": "er_pension",
}

# keys that are informational only (never posted, never summed)
IGNORE = {"tax_code", "hours"}

EMPLOYER_TOTALS = {
    "paye_tax": r"PAY ?E Tax",
    "total_tax_due": r"Total Tax Due",
    "ee_nic": r"Employee NIC",
    "er_nic": r"Employer NIC",
    "employment_allowance": r"NIC Employment Allowance",
    "total_nic_due": r"Total NIC Due",
    "total_tax_nic_due": r"Total Tax & NIC Due",
    "ee_pension": r"Employee Pension Contributions",
    "er_pension": r"Employer Pension Contributions",
    "total_other_payments": r"Total Other Payments",
    "total_net_pay": r"Total Net Pay",
    "total_net_outlay": r"TOTAL NET OUTLAY",
    "hmrc_due_for_period": r"Tax & NIC due for \S+",
    "hmrc_payment_for_period": r"Payment for \S+",
    "hmrc_balance_cf": r"Balance carried forward to \S+",
}

MONEY = re.compile(r"-?\d[\d,]*\.\d{2}")
FOOTER = re.compile(r"Page \d+ of \d+")
# lines inside a table that are page furniture, not employees
SKIP_ROW = re.compile(r"^(All Employees|[A-Z][a-z]{2}-\d{4}\s*$)")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

LAYOUT_RE = re.compile(r"Layout:\s*([A-Za-z]+)")


def fail(msg):
    raise Hold(msg, stage="parse")


def norm(title):
    t = title.replace(" ", " ").strip().lower()
    t = t.replace("-", " ").replace("&", "&")
    return re.sub(r"\s+", " ", t)


def money(cell, context):
    cell = (cell or "").replace(" ", " ").strip()
    if cell in ("", "-"):
        return 0.0
    cell = cell.replace(",", "").replace("£", "")
    neg = cell.startswith("(") and cell.endswith(")")
    if neg:
        cell = cell[1:-1]
    try:
        val = float(cell)
    except ValueError:
        fail(f"'{cell}' in {context} is not a number")
    return -val if neg else val


# --------------------------------------------------------------- tab-delimited

def sections(lines):
    """Yield (header_cells, data_rows) for every table found."""
    out = []
    i = 0
    while i < len(lines):
        cells = [c.strip() for c in lines[i].split("\t")]
        if cells and norm(cells[0]) == "employee" and len(cells) > 2:
            header = cells
            rows, i = [], i + 1
            while i < len(lines):
                raw = lines[i]
                cells = [c.strip() for c in raw.split("\t")]
                first = norm(cells[0]) if cells else ""
                if first in ("total", "totals"):
                    rows.append(("__total__", cells))
                    i += 1
                    break
                if not raw.strip() or "\t" not in raw:
                    break
                if first:
                    rows.append((cells[0].strip(), cells))
                i += 1
            out.append((header, rows))
            continue
        i += 1
    return out


def read_table(header, rows):
    keys = []
    for title in header:
        n = norm(title)
        if not n:
            keys.append(None)          # spacer column
            continue
        if n not in COLUMNS:
            fail(f"unrecognised report column '{title.strip()}' - add it to "
                 "COLUMNS in msx/summary_parser.py before posting")
        keys.append(COLUMNS[n])
    people, totals = {}, None
    for label, cells in rows:
        rec = {}
        for idx, key in enumerate(keys):
            if key is None or key in IGNORE:
                continue
            cell = cells[idx] if idx < len(cells) else ""
            if key == "name":
                continue
            rec[key] = money(cell, f"row '{label}' column '{header[idx]}'")
        if label == "__total__":
            totals = rec
        else:
            people[label] = rec
    return people, totals


# ------------------------------------------------------------------ PDF text

def pdf_to_text(path):
    """Layout-preserving text for one PDF.

    Prefers ``pdftotext -layout`` (poppler) because that is what the parser
    was verified against. Falls back to pypdf's layout extraction mode when
    poppler is not installed. Raises Hold when neither is available.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "t.txt")
        try:
            subprocess.run(["pdftotext", "-layout", path, out],
                           check=True, capture_output=True)
            with open(out, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except FileNotFoundError:
            pass
        except subprocess.CalledProcessError as exc:
            fail(f"pdftotext failed on {path}: "
                 f"{exc.stderr.decode(errors='replace').strip()}")
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        fail("neither pdftotext (brew install poppler) nor pypdf "
             "(pip3 install --user pypdf) is available to read the PDF")
    try:
        reader = PdfReader(path)
        return "\n\f".join(
            (p.extract_text(extraction_mode="layout") or "")
            for p in reader.pages)
    except TypeError:
        reader = PdfReader(path)
        return "\n\f".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # pragma: no cover - depends on the PDF
        fail(f"could not read {path}: {exc}")


def load_text(paths):
    """Concatenate the text of every input (PDFs are converted)."""
    chunks = []
    for path in paths:
        if path.lower().endswith(".pdf"):
            chunks.append(pdf_to_text(path))
        else:
            with open(path, encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
    return "\n".join(chunks)


# --------------------------------------------------------------- fixed-width

def money_runs(line):
    """Column right/left edges, taken from the report's own Total row."""
    spans = [(m.start(), m.end() - 1) for m in MONEY.finditer(line)]
    cols, cur = [], None
    for s, e in spans:
        if cur and s <= cur[1] + 1:
            cur = (cur[0], e)
            cols[-1] = cur
        else:
            cur = (s, e)
            cols.append(cur)
    return cols


def fixed_tables(lines):
    """-> [(layout_name, header_lines, data_lines, total_line)] per table."""
    out = []
    for i, line in enumerate(lines):
        if "Layout:" not in line:
            continue
        m = LAYOUT_RE.search(line)
        layout = (m.group(1).lower() if m else "unknown")
        head = None
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("Employee"):
                head = j
                break
        if head is None:
            fail(f"'{line.strip()}' has no 'Employee ...' header row under it")
        end = None
        for j in range(head + 1, len(lines)):
            if lines[j].startswith("Total") and MONEY.search(lines[j]):
                end = j
                break
        if end is None:
            fail(f"no Total row found for '{line.strip()}' - the report must "
                 "include it, it is what proves every column")
        out.append((layout, lines[i + 1:head + 1], lines[head + 1:end],
                    lines[end]))
    return out


def read_fixed_table(head, data, total, furniture):
    cols = money_runs(total)
    if not cols:
        fail("the Total row carries no figures")
    first = cols[0][0]
    centres = [(s + e) / 2 for s, e in cols]
    words = [[] for _ in cols]
    for li, line in enumerate(head):
        for m in re.finditer(r"\S+", line):
            centre = (m.start() + m.end() - 1) / 2
            if centre < first - 3:
                continue            # the Employee / Tax Code gutter
            k = min(range(len(cols)), key=lambda i: abs(centre - centres[i]))
            words[k].append((li, m.start(), m.group()))
    keys = []
    for (_, end), got in zip(cols, words):
        title = " ".join(w for _, _, w in sorted(got))
        n = norm(title)
        if not n:
            fail(f"no header text found for the column ending at position "
                 f"{end} - the report header may not have extracted cleanly")
        if n not in COLUMNS:
            fail(f"unrecognised report column '{title}' - add it to COLUMNS "
                 "in msx/summary_parser.py before posting")
        keys.append(COLUMNS[n])

    def read_row(line, label):
        rec = {k: 0.0 for k in keys if k not in IGNORE}
        used = {}
        for m in MONEY.finditer(line):
            centre = (m.start() + m.end() - 1) / 2
            k = min(range(len(cols)), key=lambda i: abs(centre - centres[i]))
            if k in used:
                fail(f"'{used[k]}' and '{m.group()}' both read as the same "
                     f"column on row '{label}' - the layout was not read "
                     "correctly, do not post this")
            used[k] = m.group()
            if keys[k] in IGNORE:
                continue
            rec[keys[k]] = money(m.group(), f"row '{label}'")
        return rec

    people = {}
    for line in data:
        if (not line.strip() or line[0] == " " or FOOTER.search(line)
                or line.strip() in furniture or SKIP_ROW.match(line)
                or line.split()[0] in ("Employee", "Total")):
            continue
        name = re.match(r"^(\S.*?)(?:\s{2,}|$)", line).group(1).strip()
        if name in people:
            fail(f"'{name}' appears twice in the same layout")
        people[name] = read_row(line, name)
    if not people:
        fail("no employee rows found in a table")

    totals = read_row(total, "Total")
    for key, want in totals.items():
        got = round(sum(p.get(key, 0.0) for p in people.values()), 2)
        if abs(got - round(want, 2)) > 0.005:
            fail(f"column '{key}' reads {got:.2f} across employees but the "
                 f"report's Total row says {want:.2f} - the column layout was "
                 "not read correctly, do not post this")
    return people, totals


def merge(dest, src, who):
    for key, val in src.items():
        if key in dest and abs(dest[key] - val) > 0.005:
            fail(f"{who}: layouts disagree on {key} "
                 f"({dest[key]:.2f} vs {val:.2f})")
        dest[key] = val


# ----------------------------------------------------------------- entrypoint

def parse_text(text):
    """Parse report text (fixed-width or tab-delimited) -> payroll dict."""
    lines = text.splitlines()
    people, report_totals = {}, {}
    order, spellings = [], {}
    layouts_seen = []

    def take(got, totals, source):
        # match people across layouts ignoring spaces: pdftotext occasionally
        # splits a name ('Y lva Alexandersson'), and that must not become a
        # second employee. The spelling used by the most layouts wins.
        for name, rec in got.items():
            key = re.sub(r"\s+", "", name).lower()
            if key not in people:
                people[key] = {}
                order.append(key)
                spellings[key] = {}
            spellings[key][name] = spellings[key].get(name, 0) + 1
            merge(people[key], rec, name)
        if totals:
            merge(report_totals, totals, f"Total row ({source})")

    def display(key):
        return max(spellings[key].items(),
                   key=lambda kv: (kv[1], -len(kv[0])))[0]

    if "\t" in text:
        tables = sections(lines)
        if not tables:
            fail("no 'Employee ...' table found in the report text")
        for header, rows in tables:
            take(*read_table(header, rows), "paste")
        for line in lines:
            m = LAYOUT_RE.search(line)
            if m:
                layouts_seen.append(m.group(1).lower())
    else:
        furniture = set()
        for line in lines:
            if "Layout:" in line:
                break
            if line.strip():
                furniture.add(line.strip())
        tables = fixed_tables(lines)
        if not tables:
            fail("no 'All Employees, Layout: ...' table found - is this an "
                 "Employer's Summary?")
        for layout, head, data, total in tables:
            layouts_seen.append(layout)
            take(*read_fixed_table(head, data, total, furniture), "report")

    # report header: client name, then 'Employer's Summary', then the period
    client, period, tax_year = None, None, None
    for idx, line in enumerate(lines):
        if "Employer's Summary" in line or "Employers Summary" in line:
            for back in range(idx - 1, -1, -1):
                if lines[back].strip():
                    client = lines[back].strip()
                    break
            break
    for line in lines:
        m = re.match(r"^\s*(%s)-(\d{4})\s*$" % "|".join(MONTHS), line)
        if m:
            period = f"{m.group(1)}-{m.group(2)}"
            break
    if not period:
        fail("could not find the pay period (a line like 'Apr-2026')")
    if client:
        m = re.search(r"\s+(\d{4})[-/](\d{2})$", client)
        if m:
            tax_year = f"{m.group(1)}-{m.group(2)}"
        client = re.sub(r"\s+\d{4}[-/]\d{2}$", "", client).strip()

    employer = {}
    for key, label in EMPLOYER_TOTALS.items():
        m = re.search(label + r"\s*\t?\s*(-?[\d,]+\.\d{2})", text)
        if m:
            employer[key] = money(m.group(1), f"employer total {key}")

    return {
        "client": client,
        "period": period,
        "tax_year": tax_year,
        "employees": [dict(name=display(k), **people[k]) for k in order],
        "report_totals": report_totals,
        "employer_totals": employer,
        "layouts_seen": sorted(set(layouts_seen)),
    }


def parse_files(paths):
    """Parse one or more report files (PDF or text) -> payroll dict."""
    return parse_text(load_text(list(paths)))


def completeness_notes(payroll):
    """Return warnings about missing sections (never Holds by themselves)."""
    notes = []
    employer = payroll.get("employer_totals", {})
    for k in ("total_tax_nic_due", "total_net_pay"):
        if k not in employer:
            notes.append(f"employer totals block missing {k}")
    seen = set(payroll.get("layouts_seen", []))
    totals = payroll.get("report_totals", {})
    if "medium" in seen:
        if abs(totals.get("additions", 0.0)) > 0.005 and "additions" not in seen:
            notes.append("Total Additions is non-zero but the Additions "
                         "layout was not supplied")
        if (abs(totals.get("total_deductions", 0.0)) > 0.005
                and "deductions" not in seen):
            notes.append("Total Deductions is non-zero but the Deductions "
                         "layout was not supplied")
    return notes
