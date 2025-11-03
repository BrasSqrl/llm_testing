| Layer          | Role                                        | Data Direction    | Example                          |
| -------------- | ------------------------------------------- | ----------------- | -------------------------------- |
| **LLM Agent**  | Natural language interface + decision logic | Both              | “Show me what’s in the pipeline” |
| **MCP Server** | Bridge between agent and external systems   | Both              | Routes calls to n8n webhooks     |
| **n8n**        | Workflow orchestrator + Postgres interface  | Both              | Executes SQL or API calls        |
| **Postgres**   | Source of truth for tasks                   | Mostly read/write | Stores all active pipeline data  |










Docker container start to get to postgres:

(terminal)
docker ps
docker exec -it agent-postgres psql -U agent_user -d agent_memory





if launching from root:  uvicorn server:app --reload --port 8000 --app-dir ".\web"




What it can do (features)

Answer credit ops questions using an LLM (Ollama model gpt-oss:20b) with guardrails.

Fetch “pipeline” from Postgres tasks (we remapped “pipeline/queue” to use get_tasks, defaulting to status="open").

Query persistent memory (Postgres) by officer/borrower/status:

e.g., “show open tasks for Lopez”, “tasks for Patel”, “blocked tasks”, etc.

Create operational tasks in n8n (with explicit user confirmation), and

Auto-persist the same task to Postgres (record_task) for audit/history.

Do simple calculations like debt_yield(noi, loan_amount).

Read local files via read_file (for quick memos/notes you drop next to the MCP server).

Stay robust on first call with:

a retry if the model returns empty output,

a second pass that forces a plain-English summary when the model tries to return JSON only.

How it works (architecture)

Frontend (index.html) → sends your prompt to FastAPI (server.py) at http://127.0.0.1:8000.

FastAPI calls agent.py, which:

Builds an LLM prompt with strict tool-use rules.

If you said “pipeline” or “queue”, it short-circuits and calls the Postgres tool (get_tasks) immediately, then asks the model to summarize.

Otherwise, it asks the LLM “what next?”

If the model needs data or to act, it responds in TOOL_REQUEST (JSON) → agent.py calls the tool via MCP.

After the tool returns, agent.py re-asks the model and forces a natural-language summary if needed.

MCP layer (mcp_server.py) exposes tools:

get_tasks / record_task → Postgres (Docker on localhost:5433)

create_work_item / get_pipeline_summary → n8n webhooks (still supported, now “legacy” for pipeline)

debt_yield, read_file, db_health (utility/diagnostics)

Postgres stores your tasks (persistent memory).
We run it in Docker as agent-postgres-5433 (to avoid the local Windows Postgres on 5432).

The key tools (quick reference)

get_tasks: {"borrower"?: str, "officer"?: str, "status"?: "open|in_progress|done|blocked"}
Reads from tasks table; agent summarizes.

record_task: {"borrower": str, "officer": str, "note": str, "status"?: str}
Inserts into Postgres; generates a task_id.

create_work_item (n8n): {"borrower": str, "officer": str, "note": str}
Requires explicit user confirmation first.
After it runs, the agent auto-calls record_task.

get_pipeline_summary (n8n): legacy pipeline fetch (kept for parity).

read_file: {"path": "filename.txt"} (UTF-8)

debt_yield: {"noi": number, "loan_amount": number}

What we changed to make it smooth

“Pipeline” override: Any prompt containing “pipeline” or “queue” calls get_tasks directly (optionally filtered to status="open"), then forces a summary. This guarantees pipeline = Postgres memory, per your request.

Final-answer enforcement: After any tool call, if the model returns JSON or blanks, we re-prompt it to “summarize in plain English.”

Cold-start resilience: Added a one-time retry when the very first LLM call returns empty.

Transport reliability: MCP client now uses the absolute path to mcp_server.py, avoiding “infer transport” issues.

Port/host fixes: Postgres bound to 5433 (Docker) so Windows’ native Postgres on 5432 doesn’t conflict; mcp_server.py uses hostaddr="127.0.0.1".

Typical things to try (one at a time)

“What’s in the pipeline right now?” → Summarizes Postgres tasks (open by default).

“Show open tasks for Lopez.” → Direct Postgres read.

“Create a task for Lopez to obtain updated insurance certificates for BlueRiver Medical Partners.”
→ Agent asks for confirmation → n8n creates it → agent auto-persists to Postgres → verify with “show open tasks for Lopez”.

“Calculate the debt yield if NOI is 850000 and loan amount is 9600000.”

Where everything runs

FastAPI: uvicorn server:app --reload --port 8000 --app-dir ".\web"

Postgres (Docker): agent-postgres-5433 mapped to localhost:5433

n8n webhooks (optional/legacy for pipeline): http://localhost:5678/webhook/...

MCP server: spawned per request by fastmcp.Client (STDIO)

Quick troubleshooting (fast checks)

“(no answer)”: we already added fallbacks; if it reappears, check the FastAPI logs and try the same prompt again (cold start).

“Could not infer transport”: means a bad path; we now use an absolute path in _call_mcp_tool.

Auth errors to Postgres: ensure you’re hitting 5433, not 5432 (Windows Postgres).
DB_CONFIG in mcp_server.py: hostaddr="127.0.0.1", port=5433.

Verify tools quickly:

python test_db_health.py


python test_get_tasks.py / python test_record_task.py
