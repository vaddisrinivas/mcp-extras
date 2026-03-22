"""Test client that connects to the hello-world server and calls tools.

Run after starting webhook_server.py and hello_world.py:
    uv run python examples/test_client.py
"""

from __future__ import annotations

import asyncio

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport


async def main():
    transport = PythonStdioTransport("examples/hello_world.py")

    async with Client(transport) as client:
        # List available tools
        tools = await client.list_tools()
        print(f"Available tools: {[t.name for t in tools]}\n")

        # Call read-like tool (should pass through)
        print("--- Calling greet (read-like, no approval needed) ---")
        result = await client.call_tool("greet", {"name": "World"})
        print(f"Result: {result}\n")

        # Call read-like tool (should pass through)
        print("--- Calling list_records (read-like, no approval needed) ---")
        result = await client.call_tool("list_records", {})
        print(f"Result: {result}\n")

        # Call write tool (should trigger webhook approval)
        print("--- Calling write_file (write-like, approval required) ---")
        result = await client.call_tool(
            "write_file", {"path": "/tmp/test.txt", "content": "hello"}
        )
        print(f"Result: {result}\n")

        # Call delete tool (should trigger webhook approval, high risk)
        print("--- Calling delete_record (destructive, approval required) ---")
        result = await client.call_tool("delete_record", {"record_id": "abc-123"})
        print(f"Result: {result}\n")


if __name__ == "__main__":
    asyncio.run(main())
