# csv-data-analyst

Conversational data analyst agent built with the [Mistral Agents SDK](https://docs.mistral.ai) (`mistralai-agents`), deployable on Mistral Cloud as a managed deployment with `agents deploy`.

The user gives a link to a CSV file in their Google Drive and asks natural-language questions in French. The agent fetches the file through the user's own Google Drive connection (Mistral connector `google_drive_mcp`, on-behalf-of), then answers with real numbers computed by fixed tools only:

- `load_csv` — load the CSV into the session memory (text or base64 from `download_file_content`)
- `dataset_info` — recall which file is loaded (source, rows, columns, types)
- `describe_columns` — column types, nulls, distinct counts, min/max/mean, top values
- `filter_rows` — row filtering (`eq`, `ne`, `gt`, `ge`, `lt`, `le`, `contains`, `startswith`, `in`, `is_null`, `is_not_null`)
- `group_aggregate` — per-group aggregates (`count`, `count_distinct`, `sum`, `avg`, `min`, `max`)

**No arbitrary code execution**: the agent runs with the default `NoSandbox` — no computer is provisioned, so no shell, no filesystem, no network and no Python for model-written code (the only `run_typescript` programs that can run are restricted tool-call chains with no IO, e.g. `download_file_content` → `load_csv` passing the result through untouched). On top of that, a guardrail (`ReadOnlyGuardrail`) skips any tool call other than the five CSV tools (`client.*`) and the six read-only Google Drive tools — write tools (`create_file`, `update_file`, `copy_file`) and anything else are refused before execution. The Drive connector only forces `programmatic_tool_calling` in the config encoding; it cannot turn into real code execution without an executable sandbox.

Sessions are durable on Mistral Cloud, so follow-up questions in the same conversation remember the loaded file and everything discussed.

## Run locally

```bash
uv run --env-file .env agents chat          # local chat (needs MISTRAL_API_KEY in .env)
uv run python test_agent.py                 # local checks, no API key needed
uv run --env-file .env python smoke_test.py # end-to-end run with the real model (inline CSV + follow-up question)
```

## Deploy (Mistral-managed)

```bash
git push                 # the deploy builds the pushed commit
uv run --env-file .env agents deploy        # hardened automatically (on-behalf-of Google Drive)
```

Requires: a GitHub repository holding this project with the GitHub connector connected in Mistral Studio, and a `MISTRAL_API_KEY` whose user has a workspace role with Workflows access (Developer or Workspace Contributor).

## Use the deployed agent

- In Vibe (Vibe Code Web): open the `chat.mistral.ai/managed-agents/csv-data-analyst?...` link printed by `agents deploy`.
- Programmatically: `agents.Agent.load_from(agent_name="csv-data-analyst")`, then `start_session()` / `session.run(...)` — sessions persist and can be resumed by id.
- From Vibe (this MCP session): the deployed agent is registered as a workflow; list it with `list_workflows` and run it with `execute_workflow`.
