import pytest

from shared import msx_proxy as p

ORGS = {"T-OK": {"status": "connected"}, "T-GONE": {"status": "disconnected"}}
lookup = ORGS.get
MJ = "3285c4db-b287-44d5-b27d-e546dae236c8"


def journal(**over):
    j = {"Narration": "Payroll - September 2026 (M6)", "Date": "2026-09-30",
         "Status": "DRAFT",
         "JournalLines": [{"AccountCode": "477", "LineAmount": 1000.00},
                          {"AccountCode": "814", "LineAmount": -1000.00}]}
    j.update(over)
    return {"ManualJournals": [j]}


def refused(method, path, **kw):
    with pytest.raises(p.Refusal) as e:
        p.decide(method, path, kw.get("params"), kw.get("tenant", "T-OK"),
                 kw.get("body"), lookup)
    return e.value


# ── connections and reads ────────────────────────────────────────────────────

def test_connections_needs_no_tenant():
    assert p.decide("GET", "connections", {}, None, None, lookup) == ("connections", {})


def test_connections_is_read_only():
    assert refused("POST", "connections").status == 403


def test_reads_pass_for_a_connected_tenant():
    for path in ("Organisation", "Accounts", "TaxRates"):
        assert p.decide("GET", path, {"where": "x"}, "T-OK", None, lookup) == (path, {})


def test_tenant_required_and_must_be_connected():
    assert refused("GET", "Accounts", tenant="").status == 400
    assert refused("GET", "Accounts", tenant="T-GONE").status == 403
    assert refused("GET", "Accounts", tenant="T-UNKNOWN").status == 403


def test_manual_journal_list_keeps_only_query_keys_the_pipeline_uses():
    path, params = p.decide("GET", "ManualJournals",
                            {"where": 'Narration=="x"', "page": "2",
                             "order": "Date", "Statuses": "DRAFT",
                             "unitdp": "4"}, "T-OK", None, lookup)
    assert path == "ManualJournals"
    assert params == {"where": 'Narration=="x"', "page": "2", "order": "Date"}


def test_single_journal_read():
    assert p.decide("GET", f"ManualJournals/{MJ}", None, "T-OK", None, lookup) \
        == (f"ManualJournals/{MJ}", {})


def test_everything_else_is_refused():
    for method, path in (("GET", "Invoices"), ("PUT", "Accounts"),
                         ("DELETE", "ManualJournals"), ("GET", "ManualJournals/abc"),
                         ("GET", "Reports/BalanceSheet"), ("POST", "ManualJournals"),
                         ("PUT", f"ManualJournals/{MJ}"), ("GET", "../connections")):
        assert refused(method, path).status == 403, (method, path)


# ── create ───────────────────────────────────────────────────────────────────

def test_create_one_balanced_journal_passes():
    path, params = p.decide("PUT", "ManualJournals", {"summarizeErrors": "true"},
                            "T-OK", journal(), lookup)
    assert (path, params) == ("ManualJournals", {"summarizeErrors": "false"})


def test_create_posted_passes():
    p.decide("PUT", "ManualJournals", None, "T-OK", journal(Status="POSTED"), lookup)


@pytest.mark.parametrize("body, text", [
    ({"ManualJournals": []}, "exactly one"),
    ({"ManualJournals": [journal()["ManualJournals"][0]] * 2}, "exactly one"),
    (journal(ManualJournalID=MJ), "never names an existing"),
    (journal(Status="VOIDED"), "DRAFT or POSTED"),
    (journal(Narration="  "), "Narration"),
    (journal(Date=""), "Date"),
    (journal(JournalLines=[{"AccountCode": "477", "LineAmount": 5}]), "two lines"),
    (journal(JournalLines=[{"AccountCode": "477", "LineAmount": 5},
                           {"AccountCode": "814", "LineAmount": -4.99}]), "balance"),
    (journal(JournalLines=[{"AccountCode": "477", "LineAmount": 5},
                           {"AccountID": "x", "LineAmount": -5}]), "AccountCode"),
    (journal(JournalLines=[{"AccountCode": "477", "LineAmount": 0},
                           {"AccountCode": "814", "LineAmount": 0}]), "zero"),
    (journal(JournalLines=[{"AccountCode": "477", "LineAmount": "ten"},
                           {"AccountCode": "814", "LineAmount": -10}]), "not a number"),
    ("[]", "JSON object"),
])
def test_create_refusals(body, text):
    e = refused("PUT", "ManualJournals", body=body)
    assert e.status == 422 and text in e.reason


def test_create_balances_in_decimal_not_float():
    lines = [{"AccountCode": "477", "LineAmount": 0.1}, {"AccountCode": "478", "LineAmount": 0.2},
             {"AccountCode": "814", "LineAmount": -0.3}]
    p.decide("PUT", "ManualJournals", None, "T-OK", journal(JournalLines=lines), lookup)


# ── approve ──────────────────────────────────────────────────────────────────

def test_approve_passes_only_the_exact_status_change():
    body = {"ManualJournals": [{"ManualJournalID": MJ, "Status": "POSTED"}]}
    assert p.decide("POST", f"ManualJournals/{MJ}", None, "T-OK", body, lookup) \
        == (f"ManualJournals/{MJ}", {"summarizeErrors": "false"})


@pytest.mark.parametrize("body, status", [
    ({"ManualJournals": [{"ManualJournalID": MJ, "Status": "VOIDED"}]}, 403),
    ({"ManualJournals": [{"ManualJournalID": MJ, "Status": "DRAFT"}]}, 403),
    ({"ManualJournals": [{"ManualJournalID": MJ, "Status": "POSTED", "Narration": "x"}]}, 403),
    ({"ManualJournals": [{"ManualJournalID": "3285c4db-b287-44d5-b27d-000000000000",
                          "Status": "POSTED"}]}, 403),
    ({"ManualJournals": []}, 422),
    (None, 422),
])
def test_approve_refusals(body, status):
    assert refused("POST", f"ManualJournals/{MJ}", body=body).status == status
