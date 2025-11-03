import json
import requests
from fastmcp import FastMCP

# Instantiate the MCP server
mcp = FastMCP("bank-assistant")

# ===========================
# CONFIG: FILL THESE IN
# ===========================

# Must be the *working* production webhook URL for your pipeline_summary workflow in n8n.
# Example shape:
#   "http://localhost:5678/webhook/pipeline_summary/"
N8N_PIPELINE_URL = "http://localhost:5678/webhook/pipeline_summary/"

# Must be the *working* production webhook URL for your create_work_item workflow in n8n.
# Example shape:
#   "http://localhost:5678/webhook/create_work_item/"
N8N_CREATE_TASK_URL = "http://localhost:5678/webhook/create_work_item/"


# ===========================
# TOOL: get_pipeline_summary
# ===========================

@mcp.tool
def get_pipeline_summary() -> str:
    """
    Return current underwriting / credit pipeline snapshot.

    We try n8n first. If n8n isn't reachable or returns non-200,
    we fall back to a local mock snapshot.
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
                    "notes": "Awaiting updated rent roll, DSCR tight"
                },
                {
                    "borrower": "Greenfield Storage Partners",
                    "stage": "Spreading",
                    "officer": "Lopez",
                    "exposure": 4200000,
                    "notes": "Need YE2024 financials, leverage high"
                }
            ]
        }
        return json.dumps(mock, indent=2)

    try:
        r = requests.get(N8N_PIPELINE_URL, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"n8n error {r.status_code}: {r.text}")
        # return text directly (n8n already responds with JSON)
        return r.text
    except Exception as e:
        fallback = {
            "error": "Failed to reach n8n pipeline endpoint",
            "details": str(e),
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
        return f"[read_file error] {e}"


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
        return f"[debt_yield error] {e}"

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
# TOOL: create_work_item (LEVEL 3 ACTION)
# ===========================

@mcp.tool
def create_work_item(borrower: str, officer: str, note: str) -> str:
    """
    Create / assign a task in n8n. Requires confirmation from user
    BEFORE the agent is allowed to call this tool.

    Args:
        borrower: "ACME Industrial LLC"
        officer:  "Lopez"
        note:     "Get updated rent roll ASAP"

    Returns:
        Text (JSON string) from n8n, including task_id and status.
    """
    if not N8N_CREATE_TASK_URL:
        return "[create_work_item error] N8N_CREATE_TASK_URL not configured"

    payload = {
        "borrower": borrower,
        "officer": officer,
        "note": note
    }

    try:
        r = requests.post(
            N8N_CREATE_TASK_URL,
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"}
        )

        if r.status_code != 200:
            return f"[create_work_item error] n8n {r.status_code}: {r.text}"

        # n8n should respond with JSON, which .text will already be
        return r.text

    except Exception as e:
        return f"[create_work_item error] {e}"


# ===========================
# ENTRYPOINT
# ===========================

if __name__ == "__main__":
    # This blocks and runs the MCP server.
    # fastmcp.Client(...) in your agent code will launch this file as a subprocess.
    mcp.run()
