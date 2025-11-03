from fastmcp import Client
import asyncio
import os

async def test():
    here = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(here, "mcp_server.py")
    async with Client(server_path) as c:
        result = await c.call_tool("get_tasks", {})
        print(result)

if __name__ == "__main__":
    asyncio.run(test())
