"""Tests for the @approval_required decorator and register_from_server()."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from mcp_approval_proxy.audit import AuditLogger
from mcp_approval_proxy.decorators import APPROVAL_META_ATTR, approval_required
from mcp_approval_proxy.middleware import ApprovalMiddleware

# ─────────────────────────────────────────────────────────────────────────────
# Decorator metadata
# ─────────────────────────────────────────────────────────────────────────────


class TestApprovalRequiredDecorator:
    def test_sets_meta_attribute(self):
        @approval_required(force=True)
        def my_tool():
            pass

        meta = getattr(my_tool, APPROVAL_META_ATTR)
        assert meta["force"] is True

    def test_always_allow_meta(self):
        @approval_required(always_allow=True)
        def safe_tool():
            pass

        meta = getattr(safe_tool, APPROVAL_META_ATTR)
        assert meta["always_allow"] is True
        assert meta["force"] is False
        assert meta["always_deny"] is False

    def test_always_deny_meta(self):
        @approval_required(always_deny=True)
        def dangerous():
            pass

        meta = getattr(dangerous, APPROVAL_META_ATTR)
        assert meta["always_deny"] is True

    def test_risk_and_reason_meta(self):
        @approval_required(risk="high", reason="This deletes everything")
        def nuke():
            pass

        meta = getattr(nuke, APPROVAL_META_ATTR)
        assert meta["risk"] == "high"
        assert meta["reason"] == "This deletes everything"

    def test_annotations_meta(self):
        @approval_required(annotations={"destructiveHint": True})
        def my_tool():
            pass

        meta = getattr(my_tool, APPROVAL_META_ATTR)
        assert meta["annotations"] == {"destructiveHint": True}

    def test_no_args_is_valid(self):
        @approval_required()
        def my_tool():
            pass

        meta = getattr(my_tool, APPROVAL_META_ATTR)
        assert meta["force"] is False
        assert meta["always_allow"] is False
        assert meta["always_deny"] is False

    def test_mutual_exclusion_raises(self):
        with pytest.raises(ValueError, match="At most one"):
            approval_required(force=True, always_allow=True)

    def test_force_and_deny_raises(self):
        with pytest.raises(ValueError):
            approval_required(force=True, always_deny=True)

    def test_decorator_preserves_function(self):
        @approval_required(force=True)
        def my_tool(x: int) -> int:
            return x * 2

        # Function still callable
        assert my_tool(3) == 6

    def test_decorator_stackable_with_server_tool(self):
        """@approval_required works alongside @server.tool()."""
        server = FastMCP(name="test")

        @server.tool()
        @approval_required(force=True, risk="high")
        def do_something(path: str) -> str:
            return path

        # Decorator metadata is on the inner function
        meta = getattr(do_something, APPROVAL_META_ATTR)
        assert meta["force"] is True
        assert meta["risk"] == "high"


# ─────────────────────────────────────────────────────────────────────────────
# register_from_server
# ─────────────────────────────────────────────────────────────────────────────


def _make_server_with_decorated_tools() -> FastMCP:
    server = FastMCP(name="decorated-server")

    @server.tool(description="Write a file")
    @approval_required(force=True, risk="high", reason="Writes to disk")
    def write_file(path: str, content: str) -> str:
        return f"wrote {path}"

    @server.tool(description="List files")
    @approval_required(always_allow=True)
    def list_files(path: str) -> list[str]:
        return []

    @server.tool(description="Drop the database")
    @approval_required(always_deny=True, reason="Never allowed")
    def drop_database() -> str:
        return "dropped"

    @server.tool(description="Read a file")
    def read_file(path: str) -> str:
        return f"contents of {path}"

    return server


@pytest.mark.asyncio
class TestRegisterFromServer:
    async def test_tool_registry_populated(self):
        server = _make_server_with_decorated_tools()
        mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)

        assert "write_file" in mw.tool_registry
        assert "list_files" in mw.tool_registry
        assert "drop_database" in mw.tool_registry
        assert "read_file" in mw.tool_registry

    async def test_force_added_to_force_approve(self):
        server = _make_server_with_decorated_tools()
        mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)
        assert "write_file" in mw._force_approve

    async def test_always_allow_added(self):
        server = _make_server_with_decorated_tools()
        mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)
        assert "list_files" in mw.always_allow

    async def test_always_deny_added(self):
        server = _make_server_with_decorated_tools()
        mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)
        assert "drop_database" in mw.always_deny

    async def test_risk_override_stored(self):
        server = _make_server_with_decorated_tools()
        mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)
        assert mw._risk_overrides.get("write_file") == "high"

    async def test_reason_override_stored(self):
        server = _make_server_with_decorated_tools()
        mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)
        assert mw._reason_overrides.get("write_file") == "Writes to disk"

    async def test_undecorated_tool_not_in_overrides(self):
        server = _make_server_with_decorated_tools()
        mw = ApprovalMiddleware(mode="destructive", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)
        assert "read_file" not in mw._force_approve
        assert "read_file" not in mw.always_allow
        assert "read_file" not in mw.always_deny


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: decorator policy flows through middleware
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDecoratorPolicyIntegration:
    async def _build(self, server=None):
        from unittest.mock import AsyncMock

        from fastmcp.server.elicitation import AcceptedElicitation

        if server is None:
            server = _make_server_with_decorated_tools()

        mw = ApprovalMiddleware(mode="none", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)

        fctx = AsyncMock()
        fctx.client_supports_extension = AsyncMock(return_value=True)
        fctx.elicit = AsyncMock(return_value=AcceptedElicitation(data=True))
        return mw, fctx

    async def test_force_gates_even_in_mode_none(self):
        """force=True should require approval even when mode='none'."""
        from unittest.mock import AsyncMock

        import mcp.types as mt
        from fastmcp.server.middleware import MiddlewareContext

        mw, fctx = await self._build()
        msg = mt.CallToolRequestParams(name="write_file", arguments={"path": "/x", "content": "y"})
        ctx = MiddlewareContext(message=msg, fastmcp_context=fctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        # Elicitation should have been triggered despite mode="none"
        fctx.elicit.assert_awaited_once()

    async def test_always_allow_bypasses_in_mode_all(self):
        """always_allow=True should bypass even when mode='all'."""
        from unittest.mock import AsyncMock

        import mcp.types as mt
        from fastmcp.server.middleware import MiddlewareContext

        server = FastMCP(name="test")

        @server.tool()
        @approval_required(always_allow=True)
        def safe_op() -> str:
            return "ok"

        mw = ApprovalMiddleware(mode="all", server_name="test", audit=AuditLogger(None))
        await mw.register_from_server(server)

        fctx = AsyncMock()
        fctx.client_supports_extension = AsyncMock(return_value=True)
        fctx.elicit = AsyncMock()

        msg = mt.CallToolRequestParams(name="safe_op", arguments={})
        ctx = MiddlewareContext(message=msg, fastmcp_context=fctx)
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        fctx.elicit.assert_not_awaited()
        call_next.assert_awaited_once()

    async def test_always_deny_blocks_without_elicitation(self):
        """always_deny=True should hard-block without asking."""
        from unittest.mock import AsyncMock

        import mcp.types as mt
        from fastmcp.server.middleware import MiddlewareContext

        mw, fctx = await self._build()
        msg = mt.CallToolRequestParams(name="drop_database", arguments={})
        ctx = MiddlewareContext(message=msg, fastmcp_context=fctx)
        call_next = AsyncMock()

        result = await mw.on_call_tool(ctx, call_next)

        fctx.elicit.assert_not_awaited()
        assert result.isError is True
