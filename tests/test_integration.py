"""Integration test: approval proxy wrapping a real FastMCP server.

Uses an in-process FastMCP server (no subprocess) to verify the full
middleware → elicitation → approve/deny flow end-to-end.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import mcp.types as mt
import pytest
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.server import create_proxy
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation

from mcp_approval_proxy.audit import AuditLogger
from mcp_approval_proxy.middleware import ApprovalMiddleware

# ─────────────────────────────────────────────────────────────────────────────
# Shared test server
# ─────────────────────────────────────────────────────────────────────────────


def make_test_server() -> FastMCP:
    """A minimal FastMCP server with read, write, and delete tools."""
    server = FastMCP(name="test-upstream")

    @server.tool(description="Read a file")
    def read_file(path: str) -> str:
        return f"contents of {path}"

    @server.tool(description="Write content to a file")
    def write_file(path: str, content: str) -> str:
        return f"wrote {len(content)} bytes to {path}"

    @server.tool(description="Delete a file permanently")
    def delete_file(path: str) -> str:
        return f"deleted {path}"

    return server


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_fastmcp_ctx(
    approved: bool = True,
    elicit_result=None,
    supports_elicitation: bool = True,
):
    if elicit_result is None:
        elicit_result = AcceptedElicitation(data=approved)
    ctx = AsyncMock()
    ctx.client_supports_extension = AsyncMock(return_value=supports_elicitation)
    ctx.elicit = AsyncMock(return_value=elicit_result)
    return ctx


def _make_ctx(tool_name: str, args: dict | None = None, fastmcp_ctx=None):
    from fastmcp.server.middleware import MiddlewareContext

    msg = mt.CallToolRequestParams(name=tool_name, arguments=args or {})
    return MiddlewareContext(message=msg, fastmcp_context=fastmcp_ctx)


async def _build_middleware(
    server: FastMCP | None = None,
    mode: str = "destructive",
    **kwargs,
) -> ApprovalMiddleware:
    """Populate tool_registry from a real FastMCP server."""
    if server is None:
        server = make_test_server()
    kwargs.setdefault("audit", AuditLogger(None))
    mw = ApprovalMiddleware(mode=mode, server_name="test-upstream", **kwargs)
    client = Client(server)
    async with client:
        tools = await client.list_tools()
    mw.tool_registry = {t.name: t for t in tools}
    return mw


# ─────────────────────────────────────────────────────────────────────────────
# Core approval flows
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestApprovalFlows:
    async def test_read_passes_without_elicitation(self):
        mw = await _build_middleware()
        ctx = _make_ctx("read_file", {"path": "/tmp/foo"}, _make_fastmcp_ctx())
        call_next = AsyncMock(
            return_value=mt.CallToolResult(content=[mt.TextContent(type="text", text="contents")])
        )

        result = await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0
        assert not result.isError

    async def test_write_approved_passes_through(self):
        mw = await _build_middleware()
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("write_file", {"path": "/tmp/x", "content": "hello"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await mw.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_awaited_once()
        call_next.assert_awaited_once()
        assert not result.isError

    async def test_write_denied_returns_error(self):
        mw = await _build_middleware()
        fastmcp_ctx = _make_fastmcp_ctx(approved=False)
        ctx = _make_ctx("write_file", {"path": "/etc/passwd", "content": "evil"}, fastmcp_ctx)

        result = await mw.on_call_tool(ctx, AsyncMock())

        fastmcp_ctx.elicit.assert_awaited_once()
        assert result.isError is True

    async def test_delete_requires_elicitation(self):
        mw = await _build_middleware()
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("delete_file", {"path": "/tmp/x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_awaited_once()

    async def test_declined_elicitation_blocks_call(self):
        mw = await _build_middleware()
        fastmcp_ctx = _make_fastmcp_ctx(elicit_result=DeclinedElicitation())
        ctx = _make_ctx("write_file", {"path": "/tmp/x", "content": "x"}, fastmcp_ctx)

        result = await mw.on_call_tool(ctx, AsyncMock())

        assert result.isError is True


# ─────────────────────────────────────────────────────────────────────────────
# Mode tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestModes:
    async def test_mode_all_gates_read_file(self):
        mw = await _build_middleware(mode="all")
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("read_file", {"path": "/tmp/foo"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_awaited_once()

    async def test_mode_none_passes_delete(self):
        mw = await _build_middleware(mode="none")
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("delete_file", {"path": "/tmp/x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_not_awaited()
        call_next.assert_awaited_once()

    async def test_always_deny_blocks_without_elicitation(self):
        mw = await _build_middleware()
        mw.always_deny = frozenset({"delete_file"})
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("delete_file", {"path": "/tmp/x"}, fastmcp_ctx)

        result = await mw.on_call_tool(ctx, AsyncMock())

        fastmcp_ctx.elicit.assert_not_awaited()
        assert result.isError is True

    async def test_always_allow_bypasses_elicitation(self):
        mw = await _build_middleware(mode="all")
        mw.always_allow = frozenset({"write_file"})
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("write_file", {"path": "/tmp/x", "content": "x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_not_awaited()
        call_next.assert_awaited_once()

    async def test_deny_pattern_blocks(self):
        mw = await _build_middleware(deny_patterns=["delete_*"])
        ctx = _make_ctx("delete_file", {"path": "/tmp/x"}, _make_fastmcp_ctx())

        result = await mw.on_call_tool(ctx, AsyncMock())

        assert result.isError is True

    async def test_allow_pattern_bypasses(self):
        mw = await _build_middleware(mode="all", allow_patterns=["read_*"])
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("read_file", {"path": "/tmp/x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        fastmcp_ctx.elicit.assert_not_awaited()
        call_next.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDryRunIntegration:
    async def test_dry_run_passes_blocked_tool(self):
        mw = await _build_middleware(dry_run=True, always_deny=["delete_file"])
        ctx = _make_ctx("delete_file", {"path": "/tmp/x"}, _make_fastmcp_ctx())
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()

    async def test_dry_run_passes_write_without_elicitation(self):
        mw = await _build_middleware(dry_run=True)
        ctx = _make_ctx("write_file", {"path": "/tmp/x", "content": "x"}, _make_fastmcp_ctx())
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        # Dry-run: never elicit
        assert ctx.fastmcp_context.elicit.await_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Custom annotations
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCustomAnnotations:
    async def test_custom_annotation_gates_unannotated_tool(self):
        # Tool "get_records" is read-like, but we override it as destructive
        mw = await _build_middleware(
            mode="annotated",
            custom_annotations={"read_file": {"destructiveHint": True}},
        )
        fastmcp_ctx = _make_fastmcp_ctx(approved=True)
        ctx = _make_ctx("read_file", {"path": "/tmp/x"}, fastmcp_ctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        # Annotated mode + custom destructiveHint → should elicit
        fastmcp_ctx.elicit.assert_awaited_once()

    async def test_custom_annotation_makes_write_safe(self):
        mw = await _build_middleware(
            mode="destructive",
            custom_annotations={"write_file": {"readOnlyHint": True}},
        )
        ctx = _make_ctx("write_file", {"path": "/tmp/x", "content": "x"}, _make_fastmcp_ctx())
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        # readOnlyHint overrides the write heuristic
        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Proxy tool passthrough
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_exposes_upstream_tools():
    """The proxy should expose the same tools as the upstream server."""
    server = make_test_server()
    client = Client(server)

    proxy = create_proxy(client)
    mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))

    async with client:
        tools = await client.list_tools()
    mw.tool_registry = {t.name: t for t in tools}
    proxy.add_middleware(mw)

    proxy_client = Client(proxy)
    async with proxy_client:
        proxy_tools = await proxy_client.list_tools()

    tool_names = {t.name for t in proxy_tools}
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "delete_file" in tool_names
