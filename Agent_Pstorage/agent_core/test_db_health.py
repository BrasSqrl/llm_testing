import os
import asyncio
from fastmcp import Client

async def test():
    here = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(here, "mcp_server.py")  # absolute path
    assert os.path.exists(server_path), f"Not found: {server_path}"

    # Client can infer how to run a .py when given an absolute path
    async with Client(server_path) as c:
        result = await c.call_tool("db_health", {})
        print(result)

if __name__ == "__main__":
    asyncio.run(test())
