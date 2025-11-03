from fastmcp import Client
import asyncio

async def test():
    async with Client("mcp_server.py") as c:
        result = await c.call_tool("get_tasks", {})
        print(result)

asyncio.run(test())
