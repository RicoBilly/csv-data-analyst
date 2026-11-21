"""Local checks for the CSV analyst agent: tool unit tests + a scripted model run (no API key needed)."""

import asyncio
import base64
import json

from mistralai import agents

import main
from main import (
    ReadOnlyGuardrail,
    dataset_info,
    describe_columns,
    filter_rows,
    group_aggregate,
    load_csv,
)

SAMPLE_CSV = """region;client;date;montant;quantite
Nord;Acme;2025-01-05;1200,50;3
Sud;Beta;2025-01-07;800;2
Nord;Acme;2025-02-11;950,25;1
Sud;Gamma;2025-02-14;2100;5
Nord;Beta;2025-03-02;430;1
"""


def test_tools() -> None:
    loaded = load_csv(source_name="ventes.csv", csv_text=SAMPLE_CSV)
    assert loaded["rows"] == 5, loaded
    assert loaded["columns"] == ["region", "client", "date", "montant", "quantite"], loaded["columns"]
    assert loaded["column_types"]["montant"] == "number", loaded["column_types"]
    assert loaded["column_types"]["date"] == "date", loaded["column_types"]
    assert loaded["column_types"]["region"] == "text", loaded["column_types"]

    info = dataset_info()
    assert info["source"] == "ventes.csv" and info["rows"] == 5

    desc = describe_columns()
    by_col = {d["column"]: d for d in desc["described"]}
    assert by_col["montant"]["min"] == 430.0 and by_col["montant"]["max"] == 2100.0
    assert abs(by_col["montant"]["mean"] - (1200.5 + 800 + 950.25 + 2100 + 430) / 5) < 1e-6
    assert by_col["region"]["distinct"] == 2
    assert by_col["region"]["top_values"][0] == {"value": "Nord", "count": 3}

    f = filter_rows(column="region", operator="eq", value="Nord")
    assert f["matched"] == 3, f
    f = filter_rows(column="montant", operator="gt", value="1000")
    assert f["matched"] == 2, f
    f = filter_rows(column="client", operator="contains", value="ac")
    assert f["matched"] == 2, f
    f = filter_rows(column="region", operator="in", value=["Sud"])
    assert f["matched"] == 2, f

    g = group_aggregate(
        group_by=["region"],
        aggregations=[
            {"column": "montant", "function": "sum"},
            {"column": "montant", "function": "avg"},
            {"column": "client", "function": "count_distinct"},
            {"column": "montant", "function": "count"},
        ],
    )
    by_region = {r["region"]: r for r in g["results"]}
    assert by_region["Nord"]["sum(montant)"] == 1200.5 + 950.25 + 430
    assert by_region["Sud"]["count_distinct(client)"] == 2
    assert by_region["Nord"]["count"] == 3
    assert g["groups"] == 2

    g2 = group_aggregate(group_by=["region", "client"], aggregations=[{"column": "quantite", "function": "max"}])
    assert g2["groups"] == 4

    b64 = base64.b64encode(SAMPLE_CSV.encode()).decode()
    loaded2 = load_csv(source_name="ventes_b64.csv", base64_content=b64)
    assert loaded2["rows"] == 5 and loaded2["replaced_previous"] == "ventes.csv"

    try:
        filter_rows(column="nope", operator="eq", value="x")
        raise AssertionError("should have raised")
    except agents.ToolError as e:
        assert "inconnue" in str(e)
    print("tool unit tests: OK")


async def test_guardrail() -> None:
    guard = ReadOnlyGuardrail()
    allowed = agents.hook.PreToolCall(turn_id="t", tool_name="download_file_content", tool_id="connector_google_drive_mcp.download_file_content", tool_call_id="c1", arguments={})
    assert await guard.pre_tool_call(None, allowed) is None
    mine = agents.hook.PreToolCall(turn_id="t", tool_name="filter_rows", tool_id="client.filter_rows", tool_call_id="c2", arguments={})
    assert await guard.pre_tool_call(None, mine) is None
    denied = agents.hook.PreToolCall(turn_id="t", tool_name="create_file", tool_id="connector_google_drive_mcp.create_file", tool_call_id="c3", arguments={})
    decision = await guard.pre_tool_call(None, denied)
    assert decision is not None and decision.kind == "skip"
    print("guardrail tests: OK")


async def test_scripted_agent() -> None:
    scripted = agents.testing.ScriptedModel(
        [
            agents.testing.call("load_csv", source_name="ventes.csv", csv_text=SAMPLE_CSV),
            agents.testing.call("group_aggregate", group_by=["region"], aggregations=[{"column": "montant", "function": "sum"}]),
            agents.testing.reply("Nord total: 2580.75 ; Sud total: 2900."),
        ]
    )
    # Same agent as main.agent but without the Drive connector (which needs a MISTRAL_API_KEY locally).
    agent = agents.Agent(
        model="mistral-large-4",
        name="test-agent",
        instructions=main.INSTRUCTIONS,
        tools=[load_csv, dataset_info, describe_columns, filter_rows, group_aggregate],
        builtins=agents.Builtins(skills=False, subagents=False),
        guardrails=[ReadOnlyGuardrail()],
    ).override(model=scripted)
    async with agents.local_runtime():
        state = await agent.run("Charge le CSV et donne la somme des montants par region.")
    assert isinstance(state, agents.Completed), state
    assert "2580.75" in state.text
    calls = [c.name for c in state.tool_calls]
    assert calls == ["client.load_csv", "client.group_aggregate"], calls
    print("scripted agent run: OK ->", json.dumps([c.name for c in state.tool_calls]))


async def main_async() -> None:
    await test_guardrail()
    await test_scripted_agent()


if __name__ == "__main__":
    test_tools()
    asyncio.run(main_async())
    print("ALL LOCAL CHECKS PASSED")
