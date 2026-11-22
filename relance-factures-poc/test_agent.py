"""Local checks for the relance-factures POC: tool unit tests, guardrail tests, scripted run (no API key needed)."""

import asyncio
import json

from mistralai import agents

import main
from main import ReadOnlyPennylaneGuardrail, compute_relance_report

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


async def test_guardrail() -> None:
    guard = ReadOnlyPennylaneGuardrail()

    def call(tool_name, tool_id, arguments=None):
        return agents.hook.PreToolCall(
            turn_id="t", tool_name=tool_name, tool_id=tool_id, tool_call_id="c", arguments=arguments or {}
        )

    assert await guard.pre_tool_call(None, call("get_context", "connector_pennylane_e376.get_context")) is None
    assert await guard.pre_tool_call(None, call("search_operations", "connector_pennylane_e376.search_operations")) is None
    assert await guard.pre_tool_call(
        None, call("run_operations", "connector_pennylane_e376.run_operations", {"operation_id": "getCustomerInvoices"})
    ) is None
    assert await guard.pre_tool_call(None, call("compute_relance_report", "client.compute_relance_report")) is None

    for name, args in [
        ("run_operations", {"operation_id": "sendByEmailCustomerInvoice"}),
        ("run_operations", {"operation_id": "postCustomerInvoiceReminder"}),
        ("run_operations", {"operation_id": "markAsPaidCustomerInvoice"}),
    ]:
        decision = await guard.pre_tool_call(None, call(name, "connector_pennylane_e376.run_operations", args))
        assert decision is not None and decision.kind == "skip", args

    for tool_name in ("create_invoice", "delete_customer", "unknown_tool"):
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
    asyncio.run(main_async())
    print("ALL LOCAL CHECKS PASSED")
