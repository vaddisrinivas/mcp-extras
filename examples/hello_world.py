"""Hello World MCP server wrapped with approval proxy + webhook engine.

Run:
    # Terminal 1 — start the webhook
    uv run python examples/webhook_server.py

    # Terminal 2 — start this server
    uv run python examples/hello_world.py

    # Terminal 3 — connect with fastmcp client
    uv run python examples/test_client.py
"""

from __future__ import annotations

from fastmcp import FastMCP

from mcp_approval_proxy import ApprovalMiddleware
from mcp_approval_proxy.engines import WebhookEngine

# ── Hello World MCP server ──────────────────────────────────────────────────

mcp = FastMCP("hello-world")


@mcp.tool()
def greet(name: str) -> str:
    """Say hello — this is a read-like tool, won't be gated in destructive mode."""
    return f"Hello, {name}!"


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Fake write — this WILL be gated (write-like name)."""
    return f"[fake] Wrote {len(content)} bytes to {path}"


@mcp.tool()
def delete_record(record_id: str) -> str:
    """Fake delete — this WILL be gated (high risk)."""
    return f"[fake] Deleted record {record_id}"


@mcp.tool()
def list_records() -> str:
    """List things — read-only, won't be gated."""
    return "record-1, record-2, record-3"


# ── Wire up the approval proxy with webhook engine ──────────────────────────

engine = WebhookEngine(
    url="http://127.0.0.1:9999/approve",
    timeout=30.0,
)

middleware = ApprovalMiddleware(
    mode="destructive",
    server_name="hello-world",
    engine=engine,
    explain_decisions=True,
    dry_run=False,
)

mcp.add_middleware(middleware)

if __name__ == "__main__":
    print("Starting hello-world MCP server (stdio) with webhook approval...")
    print("  greet        -> pass-through (read-like)")
    print("  list_records -> pass-through (read-like)")
    print("  write_file   -> gated (medium risk)")
    print("  delete_record -> gated (high risk)")
    print()
    mcp.run(transport="stdio")
