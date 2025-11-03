from fastmcp import Client
import asyncio, os, json

async def main():
    here = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(here, "mcp_server.py")
    async with Client(server_path) as c:
        res = await c.call_tool("record_task", {
            "borrower": "Falcon Steel LLC",
            "officer":  "Nguyen",
            "note":     "Request updated balance sheet",
            "status":   "open"
        })
        print(res)

if __name__ == "__main__":
    asyncio.run(main())
