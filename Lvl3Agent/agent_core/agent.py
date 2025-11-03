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
    "  • \"get_pipeline_summary\"   -> fetch live pipeline / deal status / owners / blockers\n"
    "  • \"read_file\"              -> read a local text file such as memo.txt\n"
    "  • \"debt_yield\"             -> calculate debt yield from NOI and loan amount\n"
    "  • \"create_work_item\"       -> create/assign a task to an officer via n8n\n"
    "\n"
    "Argument rules:\n"
    "- get_pipeline_summary: { }\n"
    "- read_file: { \"path\": \"exact_filename.txt\" }\n"
    "- debt_yield: { \"noi\": <number>, \"loan_amount\": <number> }\n"
    "- create_work_item: { \"borrower\": \"...\", \"officer\": \"...\", \"note\": \"...\" }\n"
    "\n"
    "IMPORTANT:\n"
    "1. You are allowed to chain multiple TOOL_REQUEST steps in a row. After each tool result is provided to you, "
    "   you will get another chance to either request ANOTHER tool (TOOL_REQUEST) or give a FINAL_ANSWER.\n"
    "\n"
    "2. FINAL_ANSWER mode:\n"
    "- Use this if you ALREADY HAVE enough info to answer the user's last request.\n"
    "- In FINAL_ANSWER, DO NOT output JSON. Just answer in plain English.\n"
    "- You may summarize tool results you were explicitly given. You MUST NOT invent data you haven't seen.\n"
    "- If you do math, show each step.\n"
    "\n"
    "3. Pipeline questions:\n"
    "- If the user asks about current pipeline / underwriting queue / deal status / who owns what / blockers, "
    "  and you do not ALREADY have up-to-date pipeline info in this conversation, you MUST request:\n"
    "  {\n"
    "    \"tool\": \"get_pipeline_summary\",\n"
    "    \"arguments\": {}\n"
    "  }\n"
    "\n"
    "4. File questions:\n"
    "- If the user references a file by name (like memo.txt) and asks for its contents or summary, you MUST request:\n"
    "  {\n"
    "    \"tool\": \"read_file\",\n"
    "    \"arguments\": { \"path\": \"memo.txt\" }\n"
    "  }\n"
    "  Never guess a filename. If you do not know the filename, ask them.\n"
    "\n"
    "5. Task creation / assigning work (CRITICAL SAFETY RULE):\n"
    "- If the user asks you to assign work, create a task, chase someone for documents, "
    "  or otherwise take an operational action, you MUST first ask for confirmation.\n"
    "- Example confirmation: \"Do you want me to create a task for Lopez about getting the rent roll for ACME Industrial LLC? Yes or No.\" \n"
    "- You MUST WAIT for the user to explicitly say yes / confirm before calling create_work_item.\n"
    "- ONLY AFTER the user says yes, you respond in TOOL_REQUEST mode with STRICT JSON:\n"
    "  {\n"
    "    \"tool\": \"create_work_item\",\n"
    "    \"arguments\": {\n"
    "      \"borrower\": \"ACME Industrial LLC\",\n"
    "      \"officer\": \"Lopez\",\n"
    "      \"note\": \"Get updated rent roll ASAP\"\n"
    "    }\n"
    "  }\n"
    "\n"
    "6. When you are in TOOL_REQUEST mode:\n"
    "- Output ONLY the JSON object. No extra words. No commentary. No markdown fences.\n"
    "\n"
    "7. When you are in FINAL_ANSWER mode:\n"
    "- Output ONLY plain English. No JSON.\n"
    "- Clearly summarize status, owners, blockers, etc.\n"
    "- For math (like debt_yield), show the steps.\n"
    "\n"
    "8. NEVER claim to have looked at anything you were not explicitly given via a tool.\n"
    "9. NEVER silently create tasks or assign work without explicit human confirmation.\n"
    



)

# =========================================================
# CONVERSATION MEMORY
# =========================================================

# Global session memory for conversation (lives as long as the server runs)
conversation_history: List[Dict[str, str]] = []


# =========================================================
# HELPER: BUILD PROMPT FOR LLM
# =========================================================

def _build_prompt(messages: List[Dict[str, str]]) -> str:
    """
    Convert chat-style messages into a single prompt string for the local model
    via `ollama run`.

    We keep this simple: role headers followed by content.
    """
    parts = []
    for m in messages:
        parts.append(f"{m['role'].upper()}:\n{m['content']}\n")
    parts.append("ASSISTANT:\n")
    return "\n".join(parts)


def _call_llm(messages: List[Dict[str, str]]) -> str:
    """
    Synchronous call to local model. This blocks, which is fine.
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


# =========================================================
# MCP TOOL CALL SUPPORT
# =========================================================

async def _call_mcp_tool(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """
    Actually call the MCP server tool and return text output.
    """
    from fastmcp import Client
    async with Client("agent_core/mcp_server.py") as client:
        result = await client.call_tool(tool_name, tool_args)
        chunks = []
        for part in result.content:
            if hasattr(part, "text"):
                chunks.append(part.text)
        return "\n".join(chunks)


# =========================================================
# INTERNAL STEP 1:
# ASK MODEL WHAT TO DO NEXT
# =========================================================

def _ask_model_for_next_action() -> str:
    """
    Ask the model: either give FINAL_ANSWER or request a tool.
    We do NOT include tool results here (that happens in _ask_model_after_tool()).
    """
    # We'll tell the model explicitly what it's deciding right now.
    control_instruction = (
        "You are deciding what to do NEXT:\n"
        "- If you ALREADY have enough info to answer the user's last request, "
        "  respond in FINAL_ANSWER mode (plain English, no JSON).\n"
        "- If you STILL NEED more info, respond in TOOL_REQUEST mode by returning "
        "  STRICT JSON ONLY with {\"tool\": ..., \"arguments\": {...}}.\n"
        "- DO NOT include any explanation outside the JSON in TOOL_REQUEST mode.\n"
    )

    msgs: List[Dict[str, str]] = []
    msgs.append({"role": "system", "content": SYSTEM_PROMPT})
    msgs.extend(conversation_history)
    msgs.append({"role": "user", "content": control_instruction})

    return _call_llm(msgs)


# =========================================================
# INTERNAL STEP 2:
# ASK MODEL AGAIN AFTER WE RAN A TOOL
# =========================================================

def _ask_model_after_tool(
    tool_name: str,
    tool_args: Dict[str, Any],
    tool_output: str
) -> str:
    """
    We called a tool and got tool_output. Now we show that to the model and ask:
    'Do you need ANOTHER tool, or can you FINAL_ANSWER now?'
    """

    followup_instruction = (
        "The tool call just completed.\n"
        f"Tool name: {tool_name}\n"
        f"Tool arguments: {json.dumps(tool_args)}\n"
        "Tool returned the following data (verbatim):\n\n"
        f"{tool_output}\n\n"
        "Now decide what to do NEXT:\n"
        "- If you STILL need more info, respond in TOOL_REQUEST mode by returning "
        "  STRICT JSON ONLY with {\"tool\": ..., \"arguments\": {...}}.\n"
        "- Otherwise, provide FINAL_ANSWER in plain English (no JSON).\n"
        "- In FINAL_ANSWER you may reference the tool result above and prior "
        "  conversation, but you must not invent data you haven't seen.\n"
        "- If you do math, show steps.\n"
    )

    msgs: List[Dict[str, str]] = []
    msgs.append({"role": "system", "content": SYSTEM_PROMPT})
    msgs.extend(conversation_history)

    # We also append the "assistant" turn that represented the tool request
    # so the model remembers that it asked for this tool.
    tool_request_json = {
        "tool": tool_name,
        "arguments": tool_args
    }
    msgs.append({
        "role": "assistant",
        "content": json.dumps(tool_request_json)
    })

    # Then we append a "system" style turn injecting the tool output as ground truth.
    msgs.append({
        "role": "system",
        "content": followup_instruction
    })

    return _call_llm(msgs)


# =========================================================
# PARSE MODEL OUTPUT TO SEE IF IT'S A TOOL REQUEST
# =========================================================

def _try_parse_tool_request(maybe_json: str) -> Optional[Dict[str, Any]]:
    """
    If the model reply is strict JSON of the form:
    {
      "tool": "...",
      "arguments": { ... }
    }
    then return that dict.
    Otherwise return None.
    """
    txt = maybe_json.strip()

    # Quick fast-fail: if it doesn't start with { and end with }, probably not JSON
    if not (txt.startswith("{") and txt.endswith("}")):
        return None

    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError:
        return None

    if (
        isinstance(parsed, dict)
        and "tool" in parsed
        and "arguments" in parsed
        and isinstance(parsed["arguments"], dict)
    ):
        return parsed

    return None


# =========================================================
# PUBLIC ENTRYPOINT:
# run_agent_turn_async
# =========================================================

async def run_agent_turn_async(user_text: str) -> str:
    """
    This is called once per browser message by FastAPI.

    What it does:
    1. Append user's message to conversation_history.
    2. Loop:
       a. Ask model what to do next.
       b. If it's a tool request -> call MCP tool -> append tool result into context -> continue.
       c. If it's a FINAL_ANSWER -> append answer to memory and return it.

    We also enforce a max_steps so it can't infinite-loop tool calls.
    """

    # 1. Save the user turn in memory
    conversation_history.append({"role": "user", "content": user_text})

    max_steps = 5  # hard safety ceiling so it can't spin forever
    step_count = 0

    latest_reply_text: str = ""

    while step_count < max_steps:
        step_count += 1

        # Ask the model what to do next, given everything it knows so far
        if step_count == 1:
            model_reply = _ask_model_for_next_action()
        else:
            # After we feed it tool output, we call _ask_model_after_tool(),
            # so we shouldn't hit this branch. Just in case:
            model_reply = _ask_model_for_next_action()

        # Try to interpret that reply as a tool request
        parsed_tool_req = _try_parse_tool_request(model_reply)

        if parsed_tool_req is None:
            # Not a tool request => This is FINAL_ANSWER.
            final_answer = model_reply.strip()

            # Save assistant's final answer in memory
            conversation_history.append({"role": "assistant", "content": final_answer})
            return final_answer

        # It IS a tool request
        tool_name = parsed_tool_req["tool"]
        tool_args = parsed_tool_req["arguments"]

        # Call that tool through MCP
        tool_output = await _call_mcp_tool(tool_name, tool_args)

        # Now we ask the model AGAIN, giving it the tool output,
        # and ask if it needs more tools or can answer.
        model_reply = _ask_model_after_tool(tool_name, tool_args, tool_output)

        # Try to parse AGAIN as a tool request
        parsed_tool_req_2 = _try_parse_tool_request(model_reply)

        if parsed_tool_req_2 is None:
            # Model gave a final answer now
            final_answer = model_reply.strip()
            conversation_history.append({
                "role": "assistant",
                "content": final_answer
            })
            return final_answer

        # Otherwise, chain continues:
        # We treat this "second" tool request as the next loop iteration input.
        # BUT: we need to update conversation memory so the model "remembers"
        # it asked for that tool.
        conversation_history.append({
            "role": "assistant",
            "content": json.dumps(parsed_tool_req_2)
        })

        # Now manually execute that requested tool, update, and loop again.
        tool_name_2 = parsed_tool_req_2["tool"]
        tool_args_2 = parsed_tool_req_2["arguments"]

        tool_output_2 = await _call_mcp_tool(tool_name_2, tool_args_2)

        # After running second tool, we stuff that tool result back in history
        # so future loops know we have it.
        conversation_history.append({
            "role": "system",
            "content": (
                f"Tool '{tool_name_2}' returned:\n{tool_output_2}"
            )
        })

        # Loop continues. If the model still keeps asking for tools,
        # it'll just keep cycling until max_steps.

    # Safety exit: too many steps
    safety_msg = (
        "I tried too many tool steps without producing a final answer. "
        "Here's what I gathered so far, but you should clarify:"
    )
    conversation_history.append({"role": "assistant", "content": safety_msg})
    return safety_msg
