# mcp_server.py
import os
import json
import datetime
import requests
from fastmcp import FastMCP

# ===========================
# MCP server
# ===========================
mcp = FastMCP("bank-assistant")

# ===========================
# n8n CONFIG
# ===========================
# You can override these via environment variables if you prefer.
N8N_PIPELINE_URL = os.environ.get(
    "N8N_PIPELINE_URL",
    "http://localhost:5678/webhook/pipeline_summary/"
)
N8N_CREATE_TASK_URL = os.environ.get(
    "N8N_CREATE_TASK_URL",
    "http://localhost:5678/webhook/create_work_item/"
)

# ===========================
# POSTGRES CONFIG (direct container IP)
# ===========================
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "dbname":   "agent_memory",
    "user":     "agent_user",
    "password": "agent_pass",
    "hostaddr": "127.0.0.1",   # direct IP of container
    "host":     "",
    "port":     5433,
}

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)




# ===========================
# TOOL: get_pipeline_summary
# ===========================
@mcp.tool
def get_pipeline_summary() -> str:
    """
    Return current underwriting / credit pipeline snapshot.
    Tries n8n first; if unreachable, returns a small mock.
    """
    if not N8N_PIPELINE_URL:
        mock = {
            "pipeline_date": "2025-11-01",
            "deals": [
                {
                    "borrower": "ACME Industrial LLC",
                    "stage": "Underwriting",
                    "officer": "Smith",
                    "exposure": 15000000,
                    "notes": "Awaiting updated rent roll, DSCR tight",
                },
                {
                    "borrower": "Greenfield Storage Partners",
                    "stage": "Spreading",
                    "officer": "Lopez",
                    "exposure": 4200000,
                    "notes": "Need YE2024 financials, leverage high",
                },
            ],
        }
        return json.dumps(mock, indent=2)

    try:
        r = requests.get(N8N_PIPELINE_URL, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"n8n error {r.status_code}: {r.text}")
        return r.text
    except Exception as e:
        fallback = {
            "error": "Failed to reach n8n pipeline endpoint",
            "details": f"{type(e).__name__}: {e}",
        }
        return json.dumps(fallback, indent=2)


# ===========================
# TOOL: read_file
# ===========================
@mcp.tool
def read_file(path: str) -> str:
    """
    Read contents of a local text file (UTF-8).
    If it fails, return an error string instead of throwing.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"[read_file error] {type(e).__name__}: {e}"


# ===========================
# TOOL: debt_yield
# ===========================
@mcp.tool
def debt_yield(noi: float, loan_amount: float) -> str:
    """
    Calculate debt yield = NOI / Loan Amount * 100.
    Return a step-by-step explanation.
    """
    try:
        noi_val = float(noi)
        loan_val = float(loan_amount)
        dy = (noi_val / loan_val) * 100.0
    except Exception as e:
        return f"[debt_yield error] {type(e).__name__}: {e}"

    explanation = (
        "Debt Yield Calculation:\n"
        f"NOI = {noi_val}\n"
        f"Loan Amount = {loan_val}\n"
        "Debt Yield = NOI / Loan Amount * 100\n"
        f"           = {noi_val} / {loan_val} * 100\n"
        f"           = {dy:.2f}%\n"
    )
    return explanation


# ===========================
# TOOL: create_work_item (via n8n)
# ===========================
@mcp.tool
def create_work_item(borrower: str, officer: str, note: str) -> str:
    """
    Create / assign a task in n8n. Requires prior user confirmation in the agent.
    Returns n8n's JSON payload as text.
    """
    if not N8N_CREATE_TASK_URL:
        return "[create_work_item error] N8N_CREATE_TASK_URL not configured"

    payload = {"borrower": borrower, "officer": officer, "note": note}

    try:
        r = requests.post(
            N8N_CREATE_TASK_URL,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            return f"[create_work_item error] n8n {r.status_code}: {r.text}"
        return r.text
    except Exception as e:
        return f"[create_work_item error] {type(e).__name__}: {e}"


# ===========================
# PERSISTENT MEMORY TOOLS (Postgres)
# ===========================

def _now_utc():
    return datetime.datetime.utcnow()

@mcp.tool
def record_task(borrower: str, officer: str, note: str, status: str = "open") -> str:
    """
    Persist a task into Postgres 'tasks' table.
    - Generates a millisecond-precision task_id.
    - Stores borrower_name, officer_name, description, status, timestamps.
    Returns JSON with the stored fields (or an error string).
    """
    # Simple, stable task_id (milliseconds since epoch)
    task_id = str(int(datetime.datetime.now().timestamp() * 1000))
    now = _now_utc()

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks
                (task_id, borrower_name, officer_name, description, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (task_id, borrower, officer, note, status, now, now),
            )
        conn.commit()
        conn.close()

        return json.dumps(
            {
                "task_id": task_id,
                "borrower": borrower,
                "officer": officer,
                "note": note,
                "status": status,
                "stored": True,
            },
            indent=2,
            default=str,
        )

    except Exception as e:
        return f"[record_task error] {type(e).__name__}: {e}"


@mcp.tool
def get_tasks(borrower: str = None, officer: str = None, status: str = None) -> str:
    """
    Query tasks from Postgres with optional filters.
    - borrower: partial match (ILIKE)
    - officer:  partial match (ILIKE)
    - status:   exact match
    Returns JSON array of rows (or 'No matching tasks found.').
    """
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sql = "SELECT task_id, borrower_name, officer_name, description, status, created_at, updated_at FROM tasks WHERE 1=1"
            params = []

            if borrower:
                sql += " AND borrower_name ILIKE %s"
                params.append(f"%{borrower}%")
            if officer:
                sql += " AND officer_name ILIKE %s"
                params.append(f"%{officer}%")
            if status:
                sql += " AND status = %s"
                params.append(status)

            sql += " ORDER BY created_at DESC LIMIT 100"
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.close()

        if not rows:
            return "No matching tasks found."

        return json.dumps(rows, indent=2, default=str)

    except Exception as e:
        return f"[get_tasks error] {type(e).__name__}: {e}"


# Optional: quick DB reachability check (handy for debugging)
@mcp.tool
def db_health() -> str:
    """
    Simple connectivity check to Postgres and presence of 'tasks' table.
    """
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            _ = cur.fetchone()
            cur.execute("""
                SELECT to_regclass('public.tasks') IS NOT NULL AS has_tasks;
            """)
            has = cur.fetchone()
        conn.close()
        return json.dumps({"ok": True, "has_tasks_table": bool(has[0])}, indent=2)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, indent=2)


# ===========================
# ENTRYPOINT
# ===========================
if __name__ == "__main__":
    # Runs and blocks; your agent will also spawn this via fastmcp.Client(...)
    mcp.run()
