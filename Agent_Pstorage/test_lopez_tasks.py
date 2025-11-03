import os
import asyncio
from fastmcp import Client

async def main():
    # Resolve the MCP server path correctly from the project root
    here = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(here, "agent_core", "mcp_server.py")
    assert os.path.exists(server_path), f"Not found: {server_path}"

    async with Client(server_path) as c:
        result = await c.call_tool("get_tasks", {"officer": "Lopez", "status": "open"})
        print(result)

if __name__ == "__main__":
    asyncio.run(main())
