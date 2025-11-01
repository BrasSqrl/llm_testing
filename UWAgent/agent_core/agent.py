import json
import subprocess
from typing import List, Dict, Any

# -------------------------
# CONFIG
# -------------------------

MODEL_NAME = "gpt-oss:20b"

SYSTEM_PROMPT = (
    "You are a commercial credit analyst / credit operations assistant.\n"
    "\n"
    "You have ONLY two legal response modes:\n"
    "\n"
    "MODE 1: DIRECT ANSWER\n"
    "- You may ONLY use this mode if you can fully answer using ONLY what is "
    "  already in the conversation so far.\n"
    "- You MUST NOT assume you have looked at any external file or pipeline "
    "  data unless a tool was actually called and its results were provided.\n"
    "- You MUST NOT invent borrower data, pipeline status, or memo contents.\n"
    "- When doing math, show each step.\n"
    "\n"
    "MODE 2: TOOL REQUEST\n"
    "- If the user references ANY filename such as memo.txt or asks you to "
    "  'read' / 'summarize' a file, you MUST request the read_file tool.\n"
    "- If the user asks about current pipeline, open deals, underwriting queue, "
    "  who is working on what, deal stage, blockers, or 'what's waiting on X', "
    "  you MUST request the get_pipeline_summary tool.\n"
    "- If the user asks to calculate debt yield or similar, you MAY request the "
    "  debt_yield tool with {\"noi\":..., \"loan_amount\":...}.\n"
    "- In MODE 2 you MUST NOT answer the question yet.\n"
    "- Instead, you MUST return ONLY valid JSON in this exact shape:\n"
    "{\n"
    "  \"tool\": \"get_pipeline_summary\",\n"
    "  \"arguments\": {}\n"
    "}\n"
    "\n"
    "RULES:\n"
    "- If the user mentions a filename like memo.txt, you MUST use MODE 2 with read_file.\n"
    "- If the user asks about pipeline/queue/status/owners/blockers, you MUST use MODE 2 with get_pipeline_summary.\n"
    "- In MODE 2, do NOT add commentary beyond that JSON.\n"
    "- If you do not know the filename/path, ask the user to clarify the exact filename instead of guessing.\n"
    "- In MODE 1, NEVER claim you've read a file or pipeline data unless tool results were actually provided.\n"
    "- When doing math, show each step.\n"
)

# -------------------------
# CONVERSATION MEMORY
# -------------------------

# This persists while the server is running.
conversation_history: List[Dict[str, str]] = []


# -------------------------
# LLM CALL UTILS
# -------------------------

def _build_prompt(messages: List[Dict[str, str]]) -> str:
    """
    Convert role/content messages into a single plain-text prompt
    suitable for an instruct-style local model via `ollama run`.
    """
    parts = []
    for m in messages:
        parts.append(f"{m['role'].upper()}:\n{m['content']}\n")
    parts.append("ASSISTANT:\n")
    return "\n".join(parts)


def _call_llm(messages: List[Dict[str, str]]) -> str:
    """
    Call the local model synchronously via `ollama run MODEL_NAME`.
    This is blocking, but that's fine because FastAPI will await us
    at a higher level and we're not spawning nested loops here.
    """
    prompt = _build_prompt(messages)

    completed = subprocess.run(
        ["ollama", "run", MODEL_NAME],
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    reply = completed.stdout.decode("utf-8").strip()
    return reply


# -------------------------
# MCP TOOL CALLER (ASYNC)
# -------------------------

async def _call_mcp_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """
    Talk to the MCP server (mcp_server.py) via fastmcp Client.
    This is async, so callers of this function must be async.
    """
    from fastmcp import Client
    async with Client("agent_core/mcp_server.py") as client:
        result = await client.call_tool(tool_name, tool_args)
        chunks = []
        for part in result.content:
            if hasattr(part, "text"):
                chunks.append(part.text)
        return "\n".join(chunks)


# -------------------------
# CORE TURN LOGIC (ASYNC)
# -------------------------

async def run_agent_turn_async(user_text: str) -> str:
    """
    Handle ONE user turn end-to-end, with session memory.
    This is now async-safe for FastAPI.

    Steps:
    1. Append user_text to conversation_history.
    2. Ask the model for either:
       - a direct answer (MODE 1), OR
       - a tool request in JSON (MODE 2).
    3. If it's a tool request, we await _call_mcp_tool(...),
       then ask the model again with that tool output.
    4. Append the final assistant answer to conversation_history and return it.
    """

    # 1. Add user's message to memory
    conversation_history.append({"role": "user", "content": user_text})

    def _messages_for_first_attempt(extra_instruction: str):
        msgs: List[Dict[str, str]] = []
        msgs.append({"role": "system", "content": SYSTEM_PROMPT})
        msgs.extend(conversation_history)
        msgs.append({
            "role": "user",
            "content": (
                extra_instruction +
                "\n\nYour job now: Either answer directly (MODE 1), "
                "OR respond with a JSON tool request (MODE 2). "
                "Do NOT stay silent."
            )
        })
        return msgs

    # 2. First attempt from the model
    first_reply = _call_llm(
        _messages_for_first_attempt(
            "You must either answer in plain English OR request a tool in strict JSON."
        )
    )

    # Retry if the model returned empty/whitespace
    if not first_reply.strip():
        first_reply = _call_llm(
            _messages_for_first_attempt(
                "If the user mentioned a filename (like memo.txt), "
                "respond ONLY with JSON requesting read_file. "
                "If the user asked about pipeline/queue/status, "
                "respond ONLY with JSON requesting get_pipeline_summary. "
                "Otherwise answer plainly. Do not be empty."
            )
        )

    # 3. Detect tool request
    tool_requested = False
    tool_name = None
    tool_args: Dict[str, Any] = {}

    try:
        parsed = json.loads(first_reply)
        tool_name = parsed["tool"]
        tool_args = parsed["arguments"]
        tool_requested = True
    except json.JSONDecodeError:
        pass

    # 4. If no tool requested, treat first_reply as final answer
    if not tool_requested:
        conversation_history.append({"role": "assistant", "content": first_reply})
        return first_reply

    # 5. Tool WAS requested -> call MCP async
    tool_output = await _call_mcp_tool(tool_name, tool_args)

    # 6. Build follow-up messages with tool output included
    followup_messages: List[Dict[str, str]] = []
    followup_messages.append({"role": "system", "content": SYSTEM_PROMPT})
    followup_messages.extend(conversation_history)

    # Simulate the assistant "asking" for that tool
    followup_messages.append({
        "role": "assistant",
        "content": first_reply  # JSON that asked for the tool
    })

    # Provide tool result as system context
    followup_messages.append({
        "role": "system",
        "content": (
            f"The tool '{tool_name}' returned this data:\n\n"
            f"{tool_output}\n\n"
            "Now answer the user in plain English using ONLY this data "
            "and prior conversation context. "
            "If math is relevant, show steps. "
            "Do not claim to have read anything you were not given."
        )
    })

    final_reply = _call_llm(followup_messages)

    # 7. Remember assistant's final answer
    conversation_history.append({"role": "assistant", "content": final_reply})

    return final_reply
