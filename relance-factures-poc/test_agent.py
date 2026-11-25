"""Local checks for the relance-factures POC: tool unit tests, guardrail tests, scripted run (no API key needed)."""

import asyncio
import base64
import json
import os
from datetime import timedelta

from mistralai import agents

import main
from main import RelanceGuardrail, compute_encours_report, compute_relance_report, fetch_invoice_pdf_base64, fetch_pennylane_customer_invoices, fetch_pennylane_email_templates, fetch_relance_plan, fetch_top_debtors, send_pennylane_invoice_email, top_debtors_report

TODAY = "2026-11-22"


def _inv(invoice_id, number, customer, email, date, deadline, amount, paid="0", currency="EUR", status="finalized"):
    return {
        "invoice_id": invoice_id,
        "number": number,
        "customer_id": 100 + invoice_id,
        "customer_name": customer,
        "customer_email": email,
        "date": date,
        "deadline": deadline,
        "currency": currency,
        "amount": amount,
        "amount_paid": paid,
        "status": status,
    }


def sample_invoices():
    return [
        _inv(1, "FA-001", "Acme", "acme@example.com", "2026-09-20", "2026-11-02", "1200.00"),
        _inv(2, "FA-002", "Beta", "beta@example.com", "2026-09-20", "2026-10-18", "800.00"),
        _inv(3, "FA-003", "Gamma", "gamma@example.com", "2026-09-20", "2026-10-03", "2100.00"),
        _inv(4, "FA-004", "Delta", "delta@example.com", "2026-09-20", "2026-09-13", "430.00"),
        _inv(5, "FA-005", "Epsilon", "eps@example.com", "2026-08-10", "2026-08-14", "950.00"),
        _inv(6, "FA-006", "Zeta", "zeta@example.com", "2026-11-02", "2026-11-02", "500.00"),
        _inv(7, "FA-007", "Eta", "eta@example.com", "2026-10-01", "2026-10-15", "300.00", paid="300.00"),
        _inv(8, "FA-008", "Theta", "theta@example.com", "2026-11-01", "2026-11-27", "150.00"),
        _inv(9, "FA-009", "Iota", "iota@example.com", "2026-11-10", "2026-11-12", "75.00"),
        _inv(10, "FA-010", "Kappa", "kappa@example.com", "2026-10-01", "2026-10-01", "600.00", paid="200.00"),
    ]


def test_compute_report() -> None:
    report = compute_relance_report(json.dumps(sample_invoices()), today=TODAY)
    items = {e["number"]: e for e in report["items"]}
    assert items["FA-001"]["stage"]["level"] == 1 and items["FA-001"]["days_late"] == 20
    assert items["FA-002"]["stage"]["level"] == 2 and items["FA-002"]["days_late"] == 35
    assert items["FA-003"]["stage"]["level"] == 3 and items["FA-003"]["days_late"] == 50
    assert items["FA-004"]["stage"]["level"] == 4 and items["FA-004"]["days_late"] == 70
    assert items["FA-005"]["stage"]["level"] == 5 and items["FA-005"]["days_late"] == 100
    assert "FA-006" not in items and report["excluded_first_relance"] == [6]
    assert "FA-007" not in items and report["paid_ignored"] == 1
    assert "FA-008" not in items and "FA-009" not in items and report["not_due"] == 2
    assert "FA-010" not in items and report["excluded_partial_payment"] == [10]
    assert report["partial_payment_count"] == 1 and abs(report["partial_payment_due"] - 400.0) < 1e-6
    assert report["overdue_count"] == 5
    assert abs(report["total_due"] - (1200.00 + 800.00 + 2100.00 + 430.00 + 950.00)) < 1e-6
    assert report["by_stage"] == {"5": 1, "4": 1, "3": 1, "2": 1, "1": 1}
    md = report["report_markdown"]
    assert "Rapport a blanc" in md and "FA-005" in md and "Mise en demeure" in md
    assert "exclusion" in md.lower() and "prelevement" in md and "FA-010" in md
    print("compute_relance_report tests: OK")


def test_compute_validation() -> None:
    try:
        compute_relance_report('{"not": "a list"}', today=TODAY)
        raise AssertionError("should have raised")
    except agents.ToolError as e:
        assert "liste" in str(e)
    try:
        compute_relance_report("[]", today="not-a-date")
        raise AssertionError("should have raised")
    except agents.ToolError as e:
        assert "YYYY-MM-DD" in str(e)
    empty = compute_relance_report("[]", today=TODAY)
    assert empty["overdue_count"] == 0 and empty["report_markdown"]
    print("compute_relance_report validation: OK")


def test_compute_encours() -> None:
    accounts = [
        {"number": "411000010", "label": "BETA SARL", "total_balance": "1200.00"},
        {"number": "411000002", "label": "2D AMENAGEMENT", "total_balance": "285.6"},
        {"number": "411", "label": "ARCHIVE - Clients", "total_balance": "0.0"},
        {"number": "411000099", "label": "CLIENT INCONNU DU CATALOGUE", "total_balance": "50.25"},
    ]
    customers = [
        {"id": 1, "name": "2D AMENAGEMENT", "emails": ["contact@2d-amenagement.fr"]},
        {"id": 2, "name": "Beta sarl", "emails": ["beta@example.com", "compta@beta.example.com"]},
    ]
    payments = [
        {"invoice_id": 11, "amount": "100", "date": "2026-11-01", "customer_name": "2D AMENAGEMENT", "currency": "EUR", "status": "paid"},
        {"invoice_id": 12, "amount": "200", "date": "2026-08-15", "customer_name": "BETA SARL", "currency": "EUR", "status": "paid"},
    ]
    report = compute_encours_report(json.dumps(accounts), json.dumps(customers), json.dumps(payments), today=TODAY)
    assert report["debtor_count"] == 3
    assert abs(report["total_due"] - (1200.00 + 285.6 + 50.25)) < 1e-6
    assert report["payments_count"] == 2 and abs(report["payments_total"] - 300.0) < 1e-6
    debtors = report["debtors"]
    assert [d["customer_name"] for d in debtors] == ["Beta sarl", "2D AMENAGEMENT", "CLIENT INCONNU DU CATALOGUE"]
    assert debtors[0]["customer_email"] == "beta@example.com"
    assert debtors[1]["customer_email"] == "contact@2d-amenagement.fr"
    assert debtors[2]["customer_email"] is None and debtors[2]["account_label"] == "CLIENT INCONNU DU CATALOGUE"
    md = report["report_markdown"]
    assert "Rapport a blanc des encours clients" in md and "2D AMENAGEMENT" in md and "BETA SARL" not in md
    assert "411" in md and "J+15" in md
    try:
        compute_encours_report('{"not": "a list"}', "[]", "[]", today=TODAY)
        raise AssertionError("should have raised")
    except agents.ToolError as e:
        assert "listes" in str(e)
    print("compute_encours_report tests: OK")


def test_fetch_api_v2_tool() -> None:
    os.environ[main.API_TOKEN_ENV] = "dummy-token-never-printed"
    try:
        pages = {
            ("/customers", None): {"items": [
                {"id": 7, "name": "ACME", "emails": ["a@acme.fr"]},
                {"id": 8, "name": "BETA", "email": "b@beta.fr"},
            ], "has_more": False},
            ("/customer_invoices", None): {"items": [
                {"id": 1, "invoice_number": "FA-1", "date": "2026-09-01", "deadline": "2026-10-01",
                 "currency": "EUR", "currency_amount": "1000.00", "remaining_amount_with_tax": "400.00",
                 "paid": False, "status": "late", "draft": False, "customer": {"id": 7}},
                {"id": 2, "invoice_number": "FA-2", "date": "2026-09-01", "deadline": "2026-09-01",
                 "currency": "EUR", "currency_amount": "250.00", "remaining_amount_with_tax": None,
                 "paid": True, "status": "paid", "draft": False, "customer": {"id": 8}},
                {"id": 3, "invoice_number": "ANN-1", "date": "2026-09-01", "deadline": "2026-10-01",
                 "currency": "EUR", "currency_amount": "50.00", "status": "cancelled", "customer": {"id": 7}},
                {"id": 4, "invoice_number": "BROUILLON", "status": "draft", "draft": True, "customer": {"id": 7}},
            ], "has_more": True, "next_cursor": "c2"},
            ("/customer_invoices", "c2"): {"items": [
                {"id": 5, "invoice_number": "FA-5", "date": "2026-10-20", "deadline": "2026-11-06",
                 "currency": "EUR", "currency_amount": "80.00", "remaining_amount_with_tax": "80.00",
                 "paid": False, "status": "upcoming", "draft": False, "customer": {"id": 8}},
                {"id": 6, "invoice_number": "FA-6", "date": "2026-09-01", "deadline": "2026-09-20",
                 "currency": "EUR", "currency_amount": "500.00", "remaining_amount_with_tax": "500.00",
                 "paid": False, "status": "late", "draft": False, "customer": {"id": 7}},
            ], "has_more": False},
        }
        calls = []

        def fake_get(path, params, company_id):
            calls.append((path, params.get("cursor"), company_id, dict(params)))
            key = (path, params.get("cursor"))
            if key not in pages:
                raise AssertionError(f"unexpected call {key}")
            return pages[key]

        orig_get = main._api_v2_get
        main._api_v2_get = fake_get
        try:
            out = fetch_pennylane_customer_invoices(21994007)
        finally:
            main._api_v2_get = orig_get
        assert out["source"] == "pennylane_api_v2" and out["pages_fetched"] == 2
        assert out["customers_count"] == 2 and out["invoices_count"] == 4
        invs = {i["number"]: i for i in json.loads(out["invoices_json"])}
        assert set(invs) == {"FA-1", "FA-2", "FA-5", "FA-6"}
        assert invs["FA-1"]["amount_paid"] == 600.0 and invs["FA-1"]["customer_email"] == "a@acme.fr"
        assert invs["FA-2"]["amount_paid"] == 250.0 and invs["FA-2"]["customer_email"] == "b@beta.fr"
        assert invs["FA-5"]["amount_paid"] == 0.0 and invs["FA-5"]["deadline"] == "2026-11-06"
        assert all(c[2] == 21994007 for c in calls)
        filter_arg = [c[3]["filter"] for c in calls if c[0] == "/customer_invoices"][0]
        assert "draft" in filter_arg and "credit_note" in filter_arg
        report = compute_relance_report(out["invoices_json"], today="2026-11-22")
        nums = {e["number"]: e for e in report["items"]}
        assert "FA-1" not in nums and report["excluded_partial_payment"] == [1]
        assert report["partial_payment_count"] == 1 and abs(report["partial_payment_due"] - 400.0) < 1e-6
        assert nums["FA-6"]["stage"]["level"] == 4 and nums["FA-6"]["days_late"] == 63 and nums["FA-6"]["amount_due"] == 500.0
        assert "FA-2" not in nums
        assert nums["FA-5"]["stage"]["level"] == 1 and nums["FA-5"]["days_late"] == 16

        try:
            fetch_pennylane_customer_invoices(0)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "entier positif" in str(e)
    finally:
        del os.environ[main.API_TOKEN_ENV]
    try:
        fetch_pennylane_customer_invoices(21994007)
        raise AssertionError("should have raised")
    except agents.ToolError as e:
        assert main.API_TOKEN_ENV in str(e) and "dummy" not in str(e)
    print("fetch_pennylane_customer_invoices tests: OK")


def test_top_debtors() -> None:
    invoices = [
        {"number": "F1", "customer_name": "ACME", "customer_email": "a@acme.fr", "date": "2026-10-01", "deadline": "2026-11-01", "amount": "1000", "amount_paid": "0", "currency": "EUR"},
        {"number": "F2", "customer_name": "ACME", "customer_email": "a@acme.fr", "date": "2026-10-01", "deadline": "2026-11-10", "amount": "500", "amount_paid": "100", "currency": "EUR"},
        {"number": "F3", "customer_name": "BETA", "customer_email": "b@beta.fr", "date": "2026-09-01", "deadline": "2026-10-01", "amount": "300", "amount_paid": "0", "currency": "EUR"},
        {"number": "F4", "customer_name": "BETA", "customer_email": "b@beta.fr", "date": "2026-11-01", "deadline": "2026-12-01", "amount": "999", "amount_paid": "0", "currency": "EUR"},
        {"number": "F5", "customer_name": "GAMMA", "customer_email": "g@g.fr", "date": "2026-09-01", "deadline": "2026-09-15", "amount": "50", "amount_paid": "50", "currency": "EUR"},
    ]
    accounts = [
        {"number": "411000001", "label": "ACME", "total_balance": "1700.00"},
        {"number": "411000002", "label": "Beta", "total_balance": "250.00"},
        {"number": "411", "label": "ARCHIVE - Clients", "total_balance": "0.0"},
    ]
    out = top_debtors_report(json.dumps(invoices), json.dumps(accounts), today="2026-11-23", top_n=20)
    assert out["debtor_count"] == 2 and abs(out["total_due"] - 1700.0) < 1e-6
    top = out["top"]
    assert top[0]["customer_name"] == "ACME" and top[0]["invoices_overdue"] == 2 and top[0]["total_due"] == 1400.0
    assert top[0]["partial_invoices"] == 1 and abs(top[0]["partial_due"] - 400.0) < 1e-6
    assert out["partial_payment_excluded_from_relance"] == {"invoices": 1, "due": 400.0}
    assert top[0]["balance_411"] == 1700.0 and abs(top[0]["gap_411_minus_invoices"] - 300.0) < 1e-6
    assert top[1]["customer_name"] == "BETA" and top[1]["invoices_overdue"] == 1 and top[1]["total_due"] == 300.0
    assert top[1]["balance_411"] == 250.0 and abs(top[1]["gap_411_minus_invoices"] - (-50.0)) < 1e-6
    md = out["report_markdown"]
    assert "Top 2" in md and "ACME" in md and "BETA" in md and "Ecart" in md and "GAMMA" not in md
    assert "prelevement" in md
    out2 = top_debtors_report(json.dumps(invoices), "[]", today="2026-11-23", top_n=1)
    assert len(out2["top"]) == 1 and out2["top"][0]["balance_411"] is None
    try:
        top_debtors_report('{"x": 1}', "[]", today="2026-11-23")
        raise AssertionError("should have raised")
    except agents.ToolError as e:
        assert "listes" in str(e)
    print("top_debtors_report tests: OK")


def test_fetch_top_debtors() -> None:
    os.environ[main.API_TOKEN_ENV] = "dummy-token-never-printed"
    inv_page1 = {"items": [
        {"id": 1, "invoice_number": "F1", "date": "2026-10-01", "deadline": "2026-11-01", "currency": "EUR",
         "currency_amount": "1000.00", "remaining_amount_with_tax": "1000.00", "paid": False, "status": "late", "customer": {"id": 7}},
        {"id": 2, "invoice_number": "F2", "date": "2026-10-01", "deadline": "2026-11-10", "currency": "EUR",
         "currency_amount": "500.00", "remaining_amount_with_tax": "100.00", "paid": False, "status": "late", "customer": {"id": 7}},
        {"id": 3, "invoice_number": "F3", "date": "2026-09-01", "deadline": "2026-10-01", "currency": "EUR",
         "currency_amount": "300.00", "remaining_amount_with_tax": "300.00", "paid": False, "status": "late", "customer": {"id": 8}},
        {"id": 4, "invoice_number": "F4", "date": "2026-11-01", "deadline": "2026-12-01", "currency": "EUR",
         "currency_amount": "999.00", "remaining_amount_with_tax": "999.00", "paid": False, "status": "upcoming", "customer": {"id": 8}},
        {"id": 5, "invoice_number": "F5", "date": "2026-09-01", "deadline": "2026-09-15", "currency": "EUR",
         "currency_amount": "50.00", "paid": True, "status": "paid", "customer": {"id": 8}},
    ], "has_more": True, "next_cursor": "c2"}
    inv_page2 = {"items": [], "has_more": False}

    def make_fake(with_accounts=True):
        def fake_get(path, params, company_id):
            assert company_id == 21994007
            if path == "/customers":
                return {"items": [
                    {"id": 7, "name": "ACME", "emails": ["a@acme.fr"]},
                    {"id": 8, "name": "BETA", "email": "b@beta.fr"},
                ], "has_more": False}
            if path == "/ledger_accounts":
                if with_accounts is False:
                    raise agents.ToolError("API Pennylane v2 /ledger_accounts -> HTTP 403: scope missing")
                if with_accounts == "fallback" and params.get("filter"):
                    raise agents.ToolError("API Pennylane v2 /ledger_accounts -> HTTP 400: filtre start_with non supporte")
                if with_accounts is True:
                    assert '"start_with"' in (params.get("filter") or ""), "filtre serveur 411 attendu"
                return {"items": [
                    {"id": 101, "number": "411000001", "label": "ACME", "enabled": True, "type": "customers", "letterable": True},
                    {"id": 102, "number": "411000002", "label": "Beta", "enabled": True, "type": "customers", "letterable": True},
                    {"id": 103, "number": "512000001", "label": "Banque", "enabled": True, "type": "bank"},
                ], "has_more": False}
            if path == "/customer_invoices":
                return inv_page1 if params.get("cursor") is None else inv_page2
            raise AssertionError(f"unexpected path {path}")
        return fake_get

    try:
        orig_get = main._api_v2_get
        main._api_v2_get = make_fake(with_accounts=True)
        try:
            out = fetch_top_debtors(21994007, today="2026-11-23", top_n=20)
        finally:
            main._api_v2_get = orig_get
        assert out["source"] == "pennylane_api_v2" and out["accounts_411"] is True
        assert out["invoices_seen"] == 5 and out["pages_fetched"] == 2
        assert out["debtor_count"] == 2 and abs(out["total_due"] - 1400.0) < 1e-6
        top = out["top"]
        assert top[0]["customer_name"] == "ACME" and top[0]["invoices_overdue"] == 2 and top[0]["total_due"] == 1100.0
        assert top[0]["partial_invoices"] == 1 and abs(top[0]["partial_due"] - 100.0) < 1e-6
        assert out["partial_payment_excluded_from_relance"] == {"invoices": 1, "due": 100.0}
        assert top[0]["balance_411"] is None and top[0]["gap_411_minus_invoices"] is None
        assert top[0]["accounts_411"] == ["411000001"]
        assert top[1]["customer_name"] == "BETA" and top[1]["total_due"] == 300.0 and top[1]["accounts_411"] == ["411000002"]
        md = out["report_markdown"]
        assert "Top 2" in md and "ACME" in md and "BETA" in md and "Banque" not in md and "F4" not in md
        assert "Comptes 411" in md and "411000001" in md and "total_balance" in md and "prelevement" in md

        main._api_v2_get = make_fake(with_accounts="fallback")
        try:
            out_fb = fetch_top_debtors(21994007, today="2026-11-23", top_n=20)
        finally:
            main._api_v2_get = orig_get
        assert out_fb["accounts_411"] is True and out_fb["top"][0]["accounts_411"] == ["411000001"]

        main._api_v2_get = make_fake(with_accounts=False)
        try:
            out2 = fetch_top_debtors(21994007, today="2026-11-23", top_n=1)
        finally:
            main._api_v2_get = orig_get
        assert out2["accounts_411"] is False and len(out2["top"]) == 1
        assert out2["top"][0]["balance_411"] is None and out2["top"][0]["accounts_411"] == []
        assert "ledger_accounts" in out2["report_markdown"]
    finally:
        del os.environ[main.API_TOKEN_ENV]
    print("fetch_top_debtors tests: OK")


def test_fetch_email_templates() -> None:
    os.environ[main.API_TOKEN_ENV] = "dummy-token-never-printed"
    try:
        def fake_get(path, params, company_id):
            assert company_id == 21994007
            if path == "/email_templates":
                raise agents.ToolError("API Pennylane v2 /email_templates -> HTTP 403: scope missing")
            if path == "/billing_email_templates":
                return {"items": [
                    {"id": 21, "label": "Relance impayée j+15", "kind": "billing_email_invoice"},
                    {"id": 22, "label": "Relance impayée j+30", "kind": "billing_email_invoice"},
                    {"id": 23, "label": "Relance impayée j+45", "kind": "billing_email_invoice"},
                ], "has_more": False}
            if path == "/customer_invoice_templates":
                return {"items": [
                    {"id": 11, "name": "Facture standard"},
                ], "has_more": False}
            raise AssertionError(f"unexpected path {path}")

        orig_get = main._api_v2_get
        main._api_v2_get = fake_get
        try:
            out = fetch_pennylane_email_templates(21994007)
        finally:
            main._api_v2_get = orig_get
        assert out["source"] == "pennylane_api_v2"
        assert "403" in out["results"]["/email_templates"]["status"]
        assert out["results"]["/customer_invoice_templates"]["status"] == "ok"
        assert out["results"]["/customer_invoice_templates"]["count"] == 1
        assert out["results"]["/billing_email_templates"]["status"] == "ok"
        assert out["results"]["/billing_email_templates"]["count"] == 3
        found = {(f["id"], f["name"]) for f in out["relance_templates_found"]}
        assert found == {(21, "Relance impayée j+15"), (22, "Relance impayée j+30"), (23, "Relance impayée j+45")}
        md = out["report_markdown"]
        assert "Relance impayée j+45" in md and "403" in md and "3" in md
        assert main.PHASE2_CONFIG["gmail_account"] == "eric.doyen@id-interactive.fr"
        assert main.PHASE2_CONFIG["drive_folder_title"] == "Relance-factures"
        assert main.PHASE2_CONFIG["drive_folder_id"] == "1D3gWJnITWIpHHUKo13jgBClAmzWInDqP"
        assert main.PHASE2_CONFIG["relance_email_templates"][1] == "Relance impayée j+15"
        try:
            fetch_pennylane_email_templates(0)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "entier positif" in str(e)
    finally:
        del os.environ[main.API_TOKEN_ENV]
    print("fetch_pennylane_email_templates tests: OK")


async def test_guardrail() -> None:
    guard = RelanceGuardrail()

    def call(tool_name, tool_id, arguments=None):
        return agents.hook.PreToolCall(
            turn_id="t", tool_name=tool_name, tool_id=tool_id, tool_call_id="c", arguments=arguments or {}
        )

    for tool_name in (
        "get_context",
        "get_company",
        "search_operations",
        "list_customers",
        "get_customer",
        "list_customer_invoices",
        "get_customer_invoice",
        "list_customer_payments",
        "list_transactions",
        "list_quotes",  # outil curate list_* non liste explicitement : autorise par prefixe
    ):
        assert await guard.pre_tool_call(None, call(tool_name, f"connector_pennylane_e376.{tool_name}")) is None, tool_name

    # run_operations bloque en phase 2, meme en lecture (les outils client.* remplacent l'API v2 directe)
    for args in ({"operation_id": "getCustomerInvoices"}, {"operation_id": "sendByEmailCustomerInvoice"}):
        decision = await guard.pre_tool_call(None, call("run_operations", "connector_pennylane_e376.run_operations", args))
        assert decision is not None and decision.kind == "skip", args

    for tool_name in ("create_invoice", "delete_customer", "send_reminder", "unknown_tool"):
        decision = await guard.pre_tool_call(None, call(tool_name, f"connector_pennylane_e376.{tool_name}"))
        assert decision is not None and decision.kind == "skip", tool_name

    # outils client.* (y compris les 3 outils serveur phase 2) : autorises
    for tool_name in (
        "compute_relance_report",
        "fetch_pennylane_customer_invoices",
        "top_debtors_report",
        "fetch_top_debtors",
        "fetch_relance_plan",
        "send_pennylane_invoice_email",
        "fetch_invoice_pdf_base64",
    ):
        assert await guard.pre_tool_call(None, call(tool_name, f"client.{tool_name}", {"company_id": 21994007})) is None, tool_name

    # Gmail : brouillon + lectures autorises, envois/suppressions bloques
    for tool_name, tool_id in (
        ("gmail_create_draft", "connector_gmail.gmail_create_draft"),
        ("create_draft", "connector_gmail.create_draft"),
        ("gmail_search_threads", "connector_gmail.gmail_search_threads"),
        ("gmail_get_thread", "connector_gmail.gmail_get_thread"),
    ):
        assert await guard.pre_tool_call(None, call(tool_name, tool_id)) is None, tool_name
    for tool_name in ("gmail_send_email", "gmail_send_draft", "gmail_delete_message", "gmail_delete_label"):
        decision = await guard.pre_tool_call(None, call(tool_name, f"connector_gmail.{tool_name}"))
        assert decision is not None and decision.kind == "skip", tool_name

    # Drive : lectures + create/update/copy autorises, suppressions/permissions bloques
    for tool_name, tool_id in (
        ("google_drive_mcp_search_files", "connector_google_drive_mcp.google_drive_mcp_search_files"),
        ("search_files", "connector_google_drive_mcp.search_files"),
        ("google_drive_mcp_read_file_content", "connector_google_drive_mcp.google_drive_mcp_read_file_content"),
        ("google_drive_mcp_create_file", "connector_google_drive_mcp.google_drive_mcp_create_file"),
        ("google_drive_mcp_update_file", "connector_google_drive_mcp.google_drive_mcp_update_file"),
        ("google_drive_mcp_copy_file", "connector_google_drive_mcp.google_drive_mcp_copy_file"),
    ):
        assert await guard.pre_tool_call(None, call(tool_name, tool_id)) is None, tool_name
    for tool_name in ("google_drive_mcp_delete_file", "delete_file", "google_drive_mcp_get_file_permissions", "google_drive_mcp_update_file_permissions", "google_drive_mcp_share_file"):
        decision = await guard.pre_tool_call(None, call(tool_name, f"connector_google_drive_mcp.{tool_name}"))
        assert decision is not None and decision.kind == "skip", tool_name

    decision = await guard.pre_tool_call(None, call("run_typescript", "run_typescript"))
    assert decision is not None and decision.kind == "skip"
    decision = await guard.pre_tool_call(None, call("send_sms", "connector_allo_c229.allo_send_sms"))
    assert decision is not None and decision.kind == "skip"
    print("guardrail v2 tests: OK")


async def test_scripted_agent() -> None:
    scripted = agents.testing.ScriptedModel(
        [
            agents.testing.run_typescript(
                'return await tools.client.compute_relance_report({invoices_json: "[]", today: "2026-11-22"});'
            ),
            agents.testing.reply("Rapport a blanc genere. Aucun envoi effectue (POC lecture seule)."),
        ]
    )
    agent = agents.Agent(
        model="mistral-large-4",
        name="test-relance-poc",
        instructions=main.INSTRUCTIONS,
        tools=[compute_relance_report],
        builtins=agents.Builtins(skills=False, subagents=False),
        guardrails=[RelanceGuardrail()],
    ).override(model=scripted)
    async with agents.local_runtime():
        state = await agent.run("Produis le rapport a blanc des relances.")
    assert isinstance(state, agents.Completed), state
    assert "Aucun envoi" in state.text
    calls = [c.name for c in state.tool_calls]
    assert any("compute_relance_report" in c for c in calls), calls
    print("scripted agent run: OK ->", json.dumps(calls))


async def main_async() -> None:
    await test_guardrail()
    await test_scripted_agent()


def test_fetch_relance_plan() -> None:
    os.environ[main.API_TOKEN_ENV] = "dummy-token-never-printed"
    today = agents.now().date()

    def d(days: int) -> str:
        return (today - timedelta(days=days)).isoformat()

    def fake_get(path, params, company_id):
        assert company_id == 21994007
        if path == "/me":
            return {"scopes": ["customers:readonly", "customer_invoices:all"]}
        if path == "/customers":
            return {"items": [{"id": 7, "name": "ACME", "emails": ["a@acme.fr"]}], "has_more": False}
        if path == "/customer_invoices":
            return {"items": [
                {"id": 1, "invoice_number": "FA-1", "date": d(80), "deadline": d(51), "currency": "EUR", "currency_amount": "1000.00", "remaining_amount_with_tax": "1000.00", "paid": False, "status": "late", "customer": {"id": 7}},
                {"id": 2, "invoice_number": "FA-2", "date": d(60), "deadline": d(35), "currency": "EUR", "currency_amount": "500.00", "remaining_amount_with_tax": "500.00", "paid": False, "status": "late", "customer": {"id": 7}},
                {"id": 3, "invoice_number": "FA-3", "date": d(40), "deadline": d(17), "currency": "EUR", "currency_amount": "200.00", "remaining_amount_with_tax": "200.00", "paid": False, "status": "late", "customer": {"id": 7}},
                {"id": 4, "invoice_number": "FA-4", "date": d(120), "deadline": d(94), "currency": "EUR", "currency_amount": "300.00", "remaining_amount_with_tax": "300.00", "paid": False, "status": "late", "customer": {"id": 7}},
                {"id": 5, "invoice_number": "FA-5", "date": d(90), "deadline": d(63), "currency": "EUR", "currency_amount": "400.00", "remaining_amount_with_tax": "400.00", "paid": False, "status": "late", "customer": {"id": 7}},
            ], "has_more": False}
        raise AssertionError(f"unexpected path {path}")

    try:
        orig_get = main._api_v2_get
        main._api_v2_get = fake_get
        try:
            journal = [{"invoice_id": 2, "stage": 2, "channel": "pennylane_email", "at": f"{today.isoformat()}T08:00:00+01:00", "dry_run": False}]
            out = fetch_relance_plan(21994007, today=today.isoformat(), journal_json=json.dumps(journal))
        finally:
            main._api_v2_get = orig_get
        assert out["source"] == "pennylane_api_v2" and out["scopes_ok"] is True
        assert out["invoices_seen"] == 5 and out["pages_fetched"] == 1
        nums = {e["number"]: e for e in out["plan"]}
        assert set(nums) == {"FA-1", "FA-3", "FA-4", "FA-5"}
        assert nums["FA-1"]["stage"]["level"] == 3 and nums["FA-1"]["action"] == "pennylane_email"
        assert nums["FA-3"]["stage"]["level"] == 1 and nums["FA-3"]["action"] == "pennylane_email"
        assert nums["FA-5"]["stage"]["level"] == 4 and nums["FA-5"]["action"] == "gmail_draft"
        assert nums["FA-4"]["stage"]["level"] == 5 and nums["FA-4"]["action"] == "drive_letter"
        assert [x["invoice_id"] for x in out["excluded_by_journal"]] == [2]
        assert out["by_stage"] == {"1": 1, "3": 1, "4": 1, "5": 1}
        assert abs(out["total_due"] - 1900.0) < 1e-6
        assert "Plan de relance du" in out["report_markdown"] and "FA-2" in out["report_markdown"]

        # une entree dry_run dans le journal n'exclut PAS la facture du plan
        main._api_v2_get = fake_get
        try:
            journal_dry = [{"invoice_id": 2, "stage": 2, "channel": "pennylane_email", "at": f"{today.isoformat()}T08:00:00+01:00", "dry_run": True}]
            out2 = fetch_relance_plan(21994007, today=today.isoformat(), journal_json=json.dumps(journal_dry))
        finally:
            main._api_v2_get = orig_get
        assert {e["number"] for e in out2["plan"]} == {"FA-1", "FA-2", "FA-3", "FA-4", "FA-5"}

        for bad in (0, -3):
            try:
                fetch_relance_plan(bad)
                raise AssertionError("should have raised")
            except agents.ToolError as e:
                assert "entier positif" in str(e)
        try:
            fetch_relance_plan(21994007, today="not-a-date")
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "YYYY-MM-DD" in str(e)
        try:
            fetch_relance_plan(21994007, journal_json='{"x": 1}')
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "liste" in str(e)
    finally:
        del os.environ[main.API_TOKEN_ENV]
    print("fetch_relance_plan tests: OK")


def test_send_pennylane_invoice_email() -> None:
    os.environ[main.API_TOKEN_ENV] = "dummy-token-never-printed"
    os.environ.pop(main.RELANCE_DRY_RUN_ENV, None)
    today = agents.now().date()
    deadline = (today - timedelta(days=20)).isoformat()
    issue = (today - timedelta(days=80)).isoformat()
    inv = {"id": 5, "invoice_number": "FA-5", "date": issue, "deadline": deadline,
           "currency": "EUR", "currency_amount": "800.00", "remaining_amount_with_tax": "800.00",
           "paid": False, "status": "late", "customer": {"id": 7, "emails": ["a@acme.fr"]}}

    def fake_get(path, params, company_id):
        assert company_id == 21994007
        if path == "/customer_invoices/5":
            return inv
        if path == "/me":
            return {"scopes": ["customers:readonly", "customer_invoices:all"]}
        if path == "/billing_email_templates":
            return {"items": [
                {"id": 153549164544, "name": "Relance impayée j+15", "kind": "billing_email_invoice"},
                {"id": 22, "name": "Relance impayée j+30", "kind": "billing_email_invoice"},
                {"id": 23, "name": "Relance impayée j+45", "kind": "billing_email_invoice"},
            ], "has_more": False}
        raise AssertionError(f"unexpected path {path}")

    posts = []

    def fake_post(path, params, company_id, body=None):
        posts.append((path, company_id, body))
        return 204, None

    orig_get, orig_post = main._api_v2_get, main._api_v2_post
    main._api_v2_get, main._api_v2_post = fake_get, fake_post
    try:
        # dry-run par defaut (env non defini -> true) : aucun POST
        out = send_pennylane_invoice_email(21994007, 5, 1, journal_json="[]")
        assert out["dry_run"] is True and out["sent"] is False and not posts
        assert out["template_id"] == 153549164544 and out["customer_email"] == "a@acme.fr"
        entry = out["journal_entry"]
        assert entry["invoice_id"] == 5 and entry["stage"] == 1 and entry["dry_run"] is True
        assert len(json.loads(out["journal_json"])) == 1

        # dry_run=False : POST effectue avec le bon template
        out2 = send_pennylane_invoice_email(21994007, 5, 1, dry_run=False, journal_json="[]")
        assert out2["sent"] is True and out2["status_http"] == 204
        assert posts == [("/customer_invoices/5/send_by_email", 21994007, {"email_template_id": 153549164544})]
        assert json.loads(out2["journal_json"])[0]["dry_run"] is False

        # anti-doublon : meme facture/palier deja envoyee (non dry-run) -> refuse
        try:
            send_pennylane_invoice_email(21994007, 5, 1, dry_run=False, journal_json=out2["journal_json"])
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "deja relancee" in str(e)

        # une entree dry_run dans le journal ne bloque PAS un envoi reel
        out3 = send_pennylane_invoice_email(21994007, 5, 1, dry_run=False, journal_json=out["journal_json"])
        assert out3["sent"] is True

        # palier demande != palier calcule (20 j de retard -> palier 1, demande stage 2)
        try:
            send_pennylane_invoice_email(21994007, 5, 2, dry_run=True, journal_json="[]")
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "palier attendu 1" in str(e)

        # stage hors 1-3 (J+60/J+90 ne passent pas par cet outil)
        try:
            send_pennylane_invoice_email(21994007, 5, 4, dry_run=True)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "stage doit etre" in str(e)

        # facture soldee -> refuse
        main._api_v2_get = lambda path, params, company_id: (dict(inv, remaining_amount_with_tax="0.00", paid=True, status="paid") if path == "/customer_invoices/5" else fake_get(path, params, company_id))
        try:
            send_pennylane_invoice_email(21994007, 5, 1, dry_run=True)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "soldee" in str(e)

        # paiement partiel -> jamais relance (prelevement automatique)
        main._api_v2_get = lambda path, params, company_id: (dict(inv, remaining_amount_with_tax="300.00") if path == "/customer_invoices/5" else fake_get(path, params, company_id))
        try:
            send_pennylane_invoice_email(21994007, 5, 1, dry_run=True)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "paiement partiel" in str(e)
        main._api_v2_get = fake_get

        # scope customer_invoices:all absent -> envoi reel refuse, aucun POST
        def fake_get_noscope(path, params, company_id):
            if path == "/me":
                return {"scopes": ["customers:readonly"]}
            return fake_get(path, params, company_id)
        main._api_v2_get = fake_get_noscope
        posts.clear()
        try:
            send_pennylane_invoice_email(21994007, 5, 1, dry_run=False, journal_json="[]")
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "customer_invoices:all" in str(e)
        assert not posts
        main._api_v2_get = fake_get

        # HTTP 409 (PDF pas encore genere) -> ToolError avec indice de retry
        main._api_v2_post = lambda path, params, company_id, body=None: (409, {"message": "pdf not generated"})
        try:
            send_pennylane_invoice_email(21994007, 5, 1, dry_run=False, journal_json="[]")
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "409" in str(e)
        main._api_v2_post = fake_post

        # plafond quotidien atteint -> refuse
        journal_full = json.dumps([
            {"invoice_id": 1000 + i, "stage": 1, "channel": "pennylane_email", "at": f"{today.isoformat()}T08:00:00+01:00", "dry_run": False}
            for i in range(main.DEFAULT_DAILY_SEND_CAP)
        ])
        posts.clear()
        try:
            send_pennylane_invoice_email(21994007, 5, 1, dry_run=False, journal_json=journal_full)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "Plafond" in str(e)
        assert not posts

        # modele du palier absent : dry-run -> warning (pas d'exception), envoi reel -> ToolError
        def fake_get_no_tpl(path, params, company_id):
            if path == "/billing_email_templates":
                return {"items": [], "has_more": False}
            return fake_get(path, params, company_id)
        main._api_v2_get = fake_get_no_tpl
        outw = send_pennylane_invoice_email(21994007, 5, 1, dry_run=True, journal_json="[]")
        assert outw["dry_run"] is True and outw["template_id"] is None and "warning" in outw
        try:
            send_pennylane_invoice_email(21994007, 5, 1, dry_run=False, journal_json="[]")
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "Modele d'email" in str(e)
        main._api_v2_get = fake_get
    finally:
        main._api_v2_get, main._api_v2_post = orig_get, orig_post
        del os.environ[main.API_TOKEN_ENV]
    print("send_pennylane_invoice_email tests: OK")


def test_fetch_invoice_pdf_base64() -> None:
    os.environ[main.API_TOKEN_ENV] = "dummy-token-never-printed"
    inv = {"id": 9, "invoice_number": "FA-9", "public_file_url": "https://app.pennylane.com/signed/abc"}
    orig_get, orig_dl = main._api_v2_get, main._download_bytes
    try:
        main._api_v2_get = lambda path, params, company_id: (inv if path == "/customer_invoices/9" else (_ for _ in ()).throw(AssertionError(f"unexpected path {path}")))
        main._download_bytes = lambda url, max_bytes: b"%PDF-1.4 fake"
        out = fetch_invoice_pdf_base64(21994007, 9)
        assert out["too_large"] is False and out["filename"] == "FA-9.pdf"
        assert out["mime_type"] == "application/pdf" and out["size"] == len(b"%PDF-1.4 fake")
        assert out["base64"] == base64.b64encode(b"%PDF-1.4 fake").decode("ascii")

        main._download_bytes = lambda url, max_bytes: b"x" * (max_bytes + 1)
        out2 = fetch_invoice_pdf_base64(21994007, 9)
        assert out2["too_large"] is True and out2["size"] == 3_000_001 and "drive_link_hint" in out2

        main._api_v2_get = lambda path, params, company_id: {"id": 9, "invoice_number": "FA-9"}
        try:
            fetch_invoice_pdf_base64(21994007, 9)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "public_file_url" in str(e)
        try:
            fetch_invoice_pdf_base64(0, 9)
            raise AssertionError("should have raised")
        except agents.ToolError as e:
            assert "entier positif" in str(e)
    finally:
        main._api_v2_get, main._download_bytes = orig_get, orig_dl
        del os.environ[main.API_TOKEN_ENV]
    print("fetch_invoice_pdf_base64 tests: OK")


if __name__ == "__main__":
    test_compute_report()
    test_compute_validation()
    test_compute_encours()
    test_fetch_api_v2_tool()
    test_top_debtors()
    test_fetch_top_debtors()
    test_fetch_email_templates()
    test_fetch_relance_plan()
    test_send_pennylane_invoice_email()
    test_fetch_invoice_pdf_base64()
    asyncio.run(main_async())
    print("ALL LOCAL CHECKS PASSED")
