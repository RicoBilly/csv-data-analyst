"""Local checks for the relance-factures POC: tool unit tests, guardrail tests, scripted run (no API key needed)."""

import asyncio
import json
import os

from mistralai import agents

import main
from main import ReadOnlyPennylaneGuardrail, compute_encours_report, compute_relance_report, fetch_pennylane_customer_invoices, fetch_top_debtors, top_debtors_report

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
    assert report["overdue_count"] == 5
    assert abs(report["total_due"] - (1200.00 + 800.00 + 2100.00 + 430.00 + 950.00)) < 1e-6
    assert report["by_stage"] == {"5": 1, "4": 1, "3": 1, "2": 1, "1": 1}
    md = report["report_markdown"]
    assert "Rapport a blanc" in md and "FA-005" in md and "Mise en demeure" in md
    assert "exclusion" in md.lower()
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
        assert out["customers_count"] == 2 and out["invoices_count"] == 3
        invs = {i["number"]: i for i in json.loads(out["invoices_json"])}
        assert set(invs) == {"FA-1", "FA-2", "FA-5"}
        assert invs["FA-1"]["amount_paid"] == 600.0 and invs["FA-1"]["customer_email"] == "a@acme.fr"
        assert invs["FA-2"]["amount_paid"] == 250.0 and invs["FA-2"]["customer_email"] == "b@beta.fr"
        assert invs["FA-5"]["amount_paid"] == 0.0 and invs["FA-5"]["deadline"] == "2026-11-06"
        assert all(c[2] == 21994007 for c in calls)
        filter_arg = [c[3]["filter"] for c in calls if c[0] == "/customer_invoices"][0]
        assert "draft" in filter_arg and "credit_note" in filter_arg
        report = compute_relance_report(out["invoices_json"], today="2026-11-22")
        nums = {e["number"]: e for e in report["items"]}
        assert nums["FA-1"]["stage"]["level"] == 3 and nums["FA-1"]["days_late"] == 52 and nums["FA-1"]["amount_due"] == 400.0
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
    assert top[0]["balance_411"] == 1700.0 and abs(top[0]["gap_411_minus_invoices"] - 300.0) < 1e-6
    assert top[1]["customer_name"] == "BETA" and top[1]["invoices_overdue"] == 1 and top[1]["total_due"] == 300.0
    assert top[1]["balance_411"] == 250.0 and abs(top[1]["gap_411_minus_invoices"] - (-50.0)) < 1e-6
    md = out["report_markdown"]
    assert "Top 2" in md and "ACME" in md and "BETA" in md and "Ecart" in md and "GAMMA" not in md
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
                if not with_accounts:
                    raise agents.ToolError("API Pennylane v2 /ledger_accounts -> HTTP 403: scope missing")
                return {"items": [
                    {"number": "411000001", "label": "ACME", "total_balance": "1400.00"},
                    {"number": "411000002", "label": "Beta", "total_balance": "250.00"},
                    {"number": "512000001", "label": "Banque", "total_balance": "99999.0"},
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
        assert top[0]["balance_411"] == 1400.0 and top[0]["gap_411_minus_invoices"] == 300.0
        assert top[1]["customer_name"] == "BETA" and top[1]["total_due"] == 300.0 and top[1]["balance_411"] == 250.0
        md = out["report_markdown"]
        assert "Top 2" in md and "ACME" in md and "BETA" in md and "Banque" not in md and "F4" not in md

        main._api_v2_get = make_fake(with_accounts=False)
        try:
            out2 = fetch_top_debtors(21994007, today="2026-11-23", top_n=1)
        finally:
            main._api_v2_get = orig_get
        assert out2["accounts_411"] is False and len(out2["top"]) == 1 and out2["top"][0]["balance_411"] is None
        assert "ledger_accounts" in out2["report_markdown"]
    finally:
        del os.environ[main.API_TOKEN_ENV]
    print("fetch_top_debtors tests: OK")


async def test_guardrail() -> None:
    guard = ReadOnlyPennylaneGuardrail()

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
    assert await guard.pre_tool_call(
        None, call("run_operations", "connector_pennylane_e376.run_operations", {"operation_id": "getCustomerInvoices"})
    ) is None
    assert await guard.pre_tool_call(None, call("compute_relance_report", "client.compute_relance_report")) is None
    assert await guard.pre_tool_call(None, call("fetch_pennylane_customer_invoices", "client.fetch_pennylane_customer_invoices", {"company_id": 21994007})) is None
    assert await guard.pre_tool_call(None, call("top_debtors_report", "client.top_debtors_report", {"invoices_json": "[]"})) is None
    assert await guard.pre_tool_call(None, call("fetch_top_debtors", "client.fetch_top_debtors", {"company_id": 21994007, "top_n": 20})) is None

    for name, args in [
        ("run_operations", {"operation_id": "sendByEmailCustomerInvoice"}),
        ("run_operations", {"operation_id": "postCustomerInvoiceReminder"}),
        ("run_operations", {"operation_id": "markAsPaidCustomerInvoice"}),
    ]:
        decision = await guard.pre_tool_call(None, call(name, "connector_pennylane_e376.run_operations", args))
        assert decision is not None and decision.kind == "skip", args

    for tool_name in ("create_invoice", "delete_customer", "send_reminder", "unknown_tool"):
        decision = await guard.pre_tool_call(None, call(tool_name, f"connector_pennylane_e376.{tool_name}"))
        assert decision is not None and decision.kind == "skip", tool_name

    decision = await guard.pre_tool_call(None, call("create_draft", "connector_gmail.create_draft"))
    assert decision is not None and decision.kind == "skip"
    decision = await guard.pre_tool_call(None, call("run_typescript", "run_typescript"))
    assert decision is not None and decision.kind == "skip"
    print("guardrail tests: OK")


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
        guardrails=[ReadOnlyPennylaneGuardrail()],
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


if __name__ == "__main__":
    test_compute_report()
    test_compute_validation()
    test_compute_encours()
    test_fetch_api_v2_tool()
    test_top_debtors()
    test_fetch_top_debtors()
    asyncio.run(main_async())
    print("ALL LOCAL CHECKS PASSED")
