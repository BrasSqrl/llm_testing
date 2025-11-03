import json
import subprocess
from typing import List, Dict, Any, Optional

# =========================================================
# CONFIG
# =========================================================

MODEL_NAME = "gpt-oss:20b"

SYSTEM_PROMPT = (
    "You are a commercial credit analyst / credit operations assistant.\n"
    "\n"
    "You operate in two modes every time you respond:\n"
    "\n"
    "MODE: TOOL_REQUEST\n"
    "- Use this if you STILL NEED more info OR you are about to TAKE AN ACTION the user explicitly approved.\n"
    "- In TOOL_REQUEST mode you MUST respond with STRICT JSON ONLY:\n"
    "  {\n"
    "    \"tool\": \"TOOL_NAME\",\n"
    "    \"arguments\": { ... }\n"
    "  }\n"
    "\n"
    "Allowed TOOL_NAME values:\n"
    "  • \"get_pipeline_summary\"   -> fetch live pipeline / deal status / owners / blockers (legacy)\n"
    "  • \"read_file\"              -> read a local text file such as memo.txt\n"
    "  • \"debt_yield\"             -> calculate debt yield from NOI and loan amount\n"
    "  • \"create_work_item\"       -> create/assign a task to an officer via n8n\n"
    "  • \"record_task\"            -> record a persistent task into Postgres\n"
    "  • \"get_tasks\"              -> retrieve persistent tasks\n"
    "\n"
    "Argument rules:\n"
    "- get_pipeline_summary: { }\n"
    "- read_file: { \"path\": \"exact_filename.txt\" }\n"
    "- debt_yield: { \"noi\": <number>, \"loan_amount\": <number> }\n"
    "- create_work_item: { \"borrower\": \"...\", \"officer\": \"...\", \"note\": \"...\" }\n"
    "- record_task: { \"borrower\": \"...\", \"officer\": \"...\", \"note\": \"...\" }\n"
    "- get_tasks: { \"borrower\"?: \"...\", \"officer\"?: \"...\", \"status\"?: \"...\" }\n"
    "\n"
    "3. Pipeline questions (REDEFINED):\n"
    "- When the user asks about the pipeline / queue (e.g., 'what’s in the pipeline', 'current pipeline', 'pipeline right now'), "
    "  treat this as a task-memory query and prefer get_tasks.\n"
    "\n"
    "10. Task-memory questions:\n"
    "- If the user asks about tasks (e.g., 'show open tasks', 'Lopez tasks', 'my tasks', 'what’s assigned', 'blocked tasks', 'done tasks'), you MUST request:\n"
    "{ \"tool\": \"get_tasks\", \"arguments\": { \"borrower\"?: \"...\", \"officer\"?: \"...\", \"status\"?: \"open|in_progress|done|blocked\" } }\n"
    "- Do NOT use get_pipeline_summary for task queries.\n"
    "\n"
    "4. File questions:\n"
    "- If the user references a file by name (like memo.txt) and asks for its contents or summary, you MUST request:\n"
    "  { \"tool\": \"read_file\", \"arguments\": { \"path\": \"memo.txt\" } }\n"
    "  Never guess a filename. If you do not know the filename, ask them.\n"
    "\n"
    "5. Task creation / assigning work (CRITICAL SAFETY RULE):\n"
    "- If the user asks you to assign work, create a task, chase someone for documents, or otherwise take an operational action, you MUST first ask for confirmation.\n"
    "- After user confirms, call create_work_item with strict JSON.\n"
    "\n"
    "6. When you are in TOOL_REQUEST mode: Output ONLY the JSON object. No extra words. No markdown fences.\n"
    "7. When you are in FINAL_ANSWER mode: Output ONLY plain English. No JSON. Summarize tool results clearly.\n"
    "8. NEVER claim to have looked at anything you were not explicitly given via a tool.\n"
    "9. NEVER silently create tasks or assign work without explicit human confirmation.\n"
)

# =========================================================
# CONVERSATION MEMORY
# =========================================================

conversation_history: List[Dict[str, str]] = []

# =========================================================
# HELPER: BUILD PROMPT FOR LLM
# =========================================================

def _build_prompt(messages: List[Dict[str, str]]) -> str:
    parts = []
    for m in messages:
        parts.append(f"{m['role'].upper()}:\n{m['content']}\n")
    parts.append("ASSISTANT:\n")
    return "\n".join(parts)

def _call_llm(messages: List[Dict[str, str]]) -> str:
    prompt = _build_prompt(messages)
    completed = subprocess.run(
        ["ollama", "run", MODEL_NAME],
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    reply = completed.stdout.decode("utf-8").strip()
    return reply

# =========================================================
# MCP TOOL CALL SUPPORT
# =========================================================

async def _call_mcp_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    import os
    from fastmcp import Client

    here = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(here, "mcp_server.py")
    if not os.path.exists(server_path):
        raise FileNotFoundError(f"mcp_server.py not found at: {server_path}")

    async with Client(server_path) as client:
        result = await client.call_tool(tool_name, tool_args)
        chunks = []
        for part in result.content:
            if hasattr(part, "text"):
                chunks.append(part.text)
        return "\n".join(chunks)

# =========================================================
# NEW HELPER: persist create_work_item tasks
# =========================================================

import json as _json

async def _maybe_persist_task(tool_name: str, tool_args: Dict[str, Any], tool_output: str):
    if tool_name != "create_work_item":
        return

    borrower = tool_args.get("borrower")
    officer = tool_args.get("officer")
    note = tool_args.get("note")

    task_id = None
    try:
        data = _json.loads(tool_output)
        if isinstance(data, dict):
            task_id = data.get("task_id")
    except Exception:
        pass

    args = {"borrower": borrower, "officer": officer, "note": note, "status": "open"}
    if task_id:
        args["task_id"] = task_id

    try:
        await _call_mcp_tool("record_task", args)
    except Exception:
        pass

# =========================================================
# INTERNAL STEP 1: ask model for next action
# =========================================================

def _ask_model_for_next_action() -> str:
    control_instruction = (
        "You are deciding what to do NEXT:\n"
        "- If you ALREADY have enough info to answer the user's last request, provide FINAL_ANSWER in plain English.\n"
        "- If you STILL NEED more info or need to take an action, respond in TOOL_REQUEST mode with STRICT JSON ONLY.\n"
    )

    msgs: List[Dict[str, str]] = []
    msgs.append({"role": "system", "content": SYSTEM_PROMPT})
    msgs.extend(conversation_history)
    msgs.append({"role": "user", "content": control_instruction})

    return _call_llm(msgs)

# =========================================================
# INTERNAL STEP 2: after a tool call
# =========================================================

def _ask_model_after_tool(tool_name: str, tool_args: Dict[str, Any], tool_output: str) -> str:
    followup_instruction = (
        "The tool call just completed.\n"
        f"Tool name: {tool_name}\n"
        f"Tool arguments: {json.dumps(tool_args)}\n"
        "Tool returned the following data (verbatim):\n\n"
        f"{tool_output}\n\n"
        "Now decide what to do NEXT:\n"
        "- If you STILL need more info, respond in TOOL_REQUEST mode by returning STRICT JSON ONLY.\n"
        "- Otherwise, provide FINAL_ANSWER in plain English (no JSON). Summarize clearly.\n"
    )

    msgs: List[Dict[str, str]] = []
    msgs.append({"role": "system", "content": SYSTEM_PROMPT})
    msgs.extend(conversation_history)
    msgs.append({"role": "assistant", "content": json.dumps({"tool": tool_name, "arguments": tool_args})})
    msgs.append({"role": "system", "content": followup_instruction})

    return _call_llm(msgs)

# =========================================================
# PARSE MODEL OUTPUT
# =========================================================

def _try_parse_tool_request(maybe_json: str) -> Optional[Dict[str, Any]]:
    txt = maybe_json.strip()
    if not (txt.startswith("{") and txt.endswith("}")):
        return None
    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and "tool" in parsed and "arguments" in parsed and isinstance(parsed["arguments"], dict):
        return parsed
    return None

# =========================================================
# MAIN ENTRYPOINT
# =========================================================

async def run_agent_turn_async(user_text: str) -> str:
    conversation_history.append({"role": "user", "content": user_text})

    # --- PIPELINE OVERRIDE: treat "pipeline/queue" as a Postgres task query ---
    lt = user_text.lower()
    if "pipeline" in lt or "queue" in lt:
        tool_name = "get_tasks"
        tool_args = {"status": "open"}  # remove this line to include all statuses
        tool_output = await _call_mcp_tool(tool_name, tool_args)

        summary = _ask_model_after_tool(
            tool_name,
            tool_args,
            tool_output + "\n\nPlease summarize these tasks in plain English, grouped by officer if helpful."
        ).strip()
        if not summary:
            summary = _ask_model_after_tool(
                tool_name,
                tool_args,
                tool_output + "\n\nSummarize in plain English."
            ).strip()
        return summary or "No tasks found."
    # --------------------------------------------------------------------------

    max_steps = 5
    step_count = 0

    while step_count < max_steps:
        step_count += 1

        # First or subsequent LLM call
        if step_count == 1:
            model_reply = _ask_model_for_next_action()
        else:
            model_reply = _ask_model_for_next_action()

        # Retry once if the model returned nothing
        if not model_reply or not model_reply.strip():
            model_reply = _ask_model_for_next_action()

        parsed_tool_req = _try_parse_tool_request(model_reply)

        if parsed_tool_req is None:
            final_answer = model_reply.strip()
            conversation_history.append({"role": "assistant", "content": final_answer})
            return final_answer

        tool_name = parsed_tool_req["tool"]
        tool_args = parsed_tool_req["arguments"]

        tool_output = await _call_mcp_tool(tool_name, tool_args)

        # persist create_work_item tasks
        await _maybe_persist_task(tool_name, tool_args, tool_output)

        # ask model again and force plain-English summary if blank or JSON
        model_reply = _ask_model_after_tool(tool_name, tool_args, tool_output).strip()

        # handle empty model output after tool call
        if not model_reply:
            model_reply = _ask_model_after_tool(
                tool_name,
                tool_args,
                tool_output + "\n\nPlease summarize the above tool data in plain English for the user.",
            ).strip()

        # handle JSON-only replies
        if model_reply.startswith("{") and model_reply.endswith("}"):
            model_reply = _ask_model_after_tool(
                tool_name,
                tool_args,
                tool_output + "\n\nPlease summarize the above tool data in plain English for the user.",
            )

        parsed_tool_req_2 = _try_parse_tool_request(model_reply)
        if parsed_tool_req_2 is None:
            final_answer = model_reply.strip()
            conversation_history.append({"role": "assistant", "content": final_answer})
            return final_answer

        conversation_history.append({"role": "assistant", "content": json.dumps(parsed_tool_req_2)})
        tool_name_2 = parsed_tool_req_2["tool"]
        tool_args_2 = parsed_tool_req_2["arguments"]

        tool_output_2 = await _call_mcp_tool(tool_name_2, tool_args_2)
        conversation_history.append({
            "role": "system",
            "content": f"Tool '{tool_name_2}' returned:\n{tool_output_2}"
        })

    safety_msg = (
        "I tried too many tool steps without producing a final answer. "
        "Here's what I gathered so far, but you should clarify:"
    )
    conversation_history.append({"role": "assistant", "content": safety_msg})
    return safety_msg
