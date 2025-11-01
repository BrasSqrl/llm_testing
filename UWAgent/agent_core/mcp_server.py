from pathlib import Path
from fastmcp import FastMCP
import requests
import json

# -----------------------------------------------------------------------------
# MCP SERVER SETUP
# -----------------------------------------------------------------------------
# This creates the MCP server. The name "local_bank_tools" is how clients
# (like your agent) will identify this capability bundle.
mcp = FastMCP("local_bank_tools")

# -----------------------------------------------------------------------------
# CONFIG / CONSTANTS
# -----------------------------------------------------------------------------
# We only allow reading files from ./shared. This prevents the model from
# wandering your entire filesystem.
ALLOWED_ROOT = Path("./shared").resolve()

# n8n endpoint configuration:
# - If N8N_PIPELINE_URL is None, we are in MOCK MODE.
# - If you set N8N_PIPELINE_URL to a real URL (like an n8n webhook),
#   we'll call that instead and return whatever n8n says.
#
# Switch to NONE below when N8N is turned off
N8N_PIPELINE_URL = 	"http://localhost:5678/webhook/pipeline_summary"


# -----------------------------------------------------------------------------
# TOOL: read_file
# -----------------------------------------------------------------------------
@mcp.tool
def read_file(path: str) -> str:
    """
    Read a UTF-8 text file under ./shared and return its full contents.

    Intended use:
    - The agent calls this when you ask things like
      "Read memo.txt and summarize risks."
    - The agent then feeds the contents back into the model so it can reason.

    Security:
    - Only files inside ./shared are allowed.
    - We block any attempt to escape that directory.
    """
    candidate = (ALLOWED_ROOT / path).resolve()

    # Block attempts to climb outside ./shared
    if not str(candidate).startswith(str(ALLOWED_ROOT)):
        raise ValueError("Access denied: outside allowed directory.")

    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(f"{candidate} not found.")

    return candidate.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# TOOL: debt_yield
# -----------------------------------------------------------------------------
@mcp.tool
def debt_yield(noi: float, loan_amount: float) -> float:
    """
    Return NOI / loan_amount as a decimal (e.g. 0.083 = 8.3%).

    Why we care:
    - Debt yield is NOI / Loan Amount.
    - Lenders like it because it's a simple "cash return on loan balance"
      style metric independent of cap rate assumptions.

    Safety:
    - We guard against divide-by-zero.
    """
    if loan_amount == 0:
        raise ValueError("loan_amount cannot be 0.")
    return noi / loan_amount


# -----------------------------------------------------------------------------
# INTERNAL HELPER: mock_pipeline_payload
# -----------------------------------------------------------------------------
def mock_pipeline_payload() -> dict:
    """
    This returns a stand-in pipeline snapshot for when you don't have
    a live n8n URL configured yet.

    You can freely edit this mock to match how you think about the pipeline:
    stage names, officer names, exposure amounts, etc.

    The agent will summarize this for you like it's real data.
    """
    return {
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
            },
            {
                "borrower": "Harbor Logistics REIT",
                "stage": "Credit Committee",
                "officer": "Nguyen",
                "exposure": 28000000,
                "notes": "Committee Monday, collateral appraisal pending"
            }
        ]
    }


# -----------------------------------------------------------------------------
# TOOL: get_pipeline_summary
# -----------------------------------------------------------------------------
@mcp.tool
def get_pipeline_summary() -> str:
    """
    Return the current underwriting / credit pipeline snapshot.

    This is where we bridge MCP -> (optionally) n8n -> your workflow data.

    Behavior:
    - If N8N_PIPELINE_URL is None:
        We are in MOCK MODE.
        We return a JSON string of mock data that looks like pipeline status.

    - If N8N_PIPELINE_URL is set to an HTTP(S) URL:
        We call that URL (GET request).
        We expect n8n to return JSON or text about deals in pipeline.
        We forward that text directly back to the agent.

    The agent will feed this string to the LLM, and the LLM will turn it
    into a human-readable summary for you (e.g., "ACME is stuck waiting
    on rent roll, Greenfield is still spreading with Lopez," etc.).

    Why this is good for control / audit:
    - n8n becomes your gatekeeper to live systems.
    - The model never directly hits SQL/SharePoint/etc., only this tool.
    - You can log in n8n who pulled what and when.
    """
    # MOCK MODE
    if N8N_PIPELINE_URL is None:
        data = mock_pipeline_payload()
        # Return a pretty JSON string so the model can read it easily.
        return json.dumps(data, indent=2)

    # LIVE MODE
    try:
        r = requests.get(N8N_PIPELINE_URL, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(
                f"n8n error {r.status_code}: {r.text}"
            )
        # We don't parse/clean here; we just return whatever n8n said.
        # The agent will hand this to the model, and the LLM will summarize.
        return r.text
    except Exception as e:
        # Graceful fallback: we don't want your whole agent to crash
        # just because n8n is offline.
        fallback = {
            "error": "Failed to reach n8n pipeline endpoint",
            "details": str(e),
            "fallback_mock_data": mock_pipeline_payload()
        }
        return json.dumps(fallback, indent=2)


# -----------------------------------------------------------------------------
# SERVER ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # We run this MCP server over stdio so that the agent can spawn it
    # and talk to it like a subprocess.
    #
    # Your agent code (pillar2_agent.py) uses:
    #   async with Client("mcp_server.py") as client:
    #       ...
    #
    # That Client(...) call launches THIS file as a subprocess and then
    # communicates with it via stdin/stdout using MCP protocol.
    #
    mcp.run(transport="stdio")
