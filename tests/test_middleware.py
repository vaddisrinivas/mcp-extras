"""Tests for the ApprovalMiddleware decision logic and elicitation flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

import mcp.types as mt
import pytest
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from fastmcp.server.middleware import MiddlewareContext

from mcp_approval_proxy.audit import AuditLogger
from mcp_approval_proxy.engines import _build_elicitation_message
from mcp_approval_proxy.middleware import (
    ApprovalMiddleware,
    _deny,
    _is_write_heuristic,
    _needs_approval,
    _resolve_annotations,
    _risk_level,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _ann(read_only: bool = False, destructive: bool = False) -> mt.ToolAnnotations:
    return mt.ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive)


def _null_audit() -> AuditLogger:
    return AuditLogger(path=None)


def _make_context(
    tool_name: str,
    arguments: dict | None = None,
    elicit_result=None,
    supports_elicitation: bool = True,
) -> MiddlewareContext:
    fastmcp_ctx = AsyncMock()
    fastmcp_ctx.client_supports_extension = AsyncMock(return_value=supports_elicitation)
    if elicit_result is None:
        elicit_result = AcceptedElicitation(data=True)
    fastmcp_ctx.elicit = AsyncMock(return_value=elicit_result)
    msg = mt.CallToolRequestParams(name=tool_name, arguments=arguments or {})
    return MiddlewareContext(message=msg, fastmcp_context=fastmcp_ctx)


def _middleware(**kwargs) -> ApprovalMiddleware:
    kwargs.setdefault("audit", _null_audit())
    return ApprovalMiddleware(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# _is_write_heuristic
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteHeuristic:
    def test_snake_case_write(self):
        assert _is_write_heuristic("write_file") is True

    def test_snake_case_delete(self):
        assert _is_write_heuristic("delete_record") is True

    def test_camel_case_write(self):
        assert _is_write_heuristic("writeFile") is True

    def test_camel_case_delete(self):
        assert _is_write_heuristic("deleteUser") is True

    def test_pascal_case(self):
        assert _is_write_heuristic("CreateUser") is True

    def test_kebab_case(self):
        assert _is_write_heuristic("delete-item") is True

    def test_read_tool_no_match(self):
        assert _is_write_heuristic("read_file") is False

    def test_list_tool_no_match(self):
        assert _is_write_heuristic("list_files") is False

    def test_get_tool_no_match(self):
        assert _is_write_heuristic("get_record") is False

    def test_formatter_no_match(self):
        # "format" IS in _WRITE_WORDS, so formatter would match via "format" token
        # This test documents the behaviour
        assert _is_write_heuristic("formatter") is False  # split = ["formatter"], not "format"

    def test_all_write_names(self):
        names = [
            "write_file",
            "delete_record",
            "create_user",
            "update_config",
            "remove_entry",
            "exec_command",
            "insert_row",
            "deploy_app",
            "upload_file",
            "send_message",
            "publish_event",
        ]
        for name in names:
            assert _is_write_heuristic(name) is True, f"{name!r} should be write"


# ─────────────────────────────────────────────────────────────────────────────
# _risk_level
# ─────────────────────────────────────────────────────────────────────────────


class TestRiskLevel:
    def test_destructive_hint_is_high(self):
        assert _risk_level("any_tool", _ann(destructive=True), "destructive") == "high"

    def test_delete_word_is_high(self):
        assert _risk_level("delete_file", None, "destructive") == "high"

    def test_write_word_is_medium(self):
        assert _risk_level("write_file", None, "destructive") == "medium"

    def test_read_tool_mode_all_is_low(self):
        assert _risk_level("read_file", _ann(read_only=True), "all") == "low"

    def test_read_tool_mode_none_is_unknown(self):
        assert _risk_level("read_file", _ann(read_only=True), "none") == "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_annotations
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveAnnotations:
    def test_no_overrides_returns_tool_annotations(self):
        tool = mt.Tool(name="t", inputSchema={}, annotations=_ann(read_only=True))
        result = _resolve_annotations("t", tool, {})
        assert result is tool.annotations

    def test_override_destructive_hint(self):
        tool = mt.Tool(name="t", inputSchema={})
        result = _resolve_annotations("t", tool, {"t": {"destructiveHint": True}})
        assert result is not None
        assert result.destructiveHint is True

    def test_override_read_only_hint(self):
        tool = mt.Tool(name="t", inputSchema={}, annotations=_ann(destructive=True))
        result = _resolve_annotations("t", tool, {"t": {"readOnlyHint": True}})
        assert result is not None
        assert result.readOnlyHint is True

    def test_override_is_case_insensitive_via_middleware(self):
        # custom_annotations keys are pre-lowercased in ServerConfig
        tool = mt.Tool(name="MyTool", inputSchema={})
        result = _resolve_annotations("MyTool", tool, {"mytool": {"destructiveHint": True}})
        assert result is not None
        assert result.destructiveHint is True

    def test_no_tool_no_override_returns_none(self):
        assert _resolve_annotations("t", None, {}) is None

    def test_no_tool_with_override_returns_annotation(self):
        result = _resolve_annotations("t", None, {"t": {"readOnlyHint": True}})
        assert result is not None
        assert result.readOnlyHint is True


# ─────────────────────────────────────────────────────────────────────────────
# _needs_approval
# ─────────────────────────────────────────────────────────────────────────────


class TestNeedsApproval:
    def _call(self, name, ann=None, mode="destructive", allow=(), deny=(), ap=(), dp=(), fp=()):
        return _needs_approval(
            name,
            ann,
            mode,
            frozenset(allow),
            frozenset(deny),
            list(ap),
            list(dp),
            frozenset(fp),
        )

    def test_mode_none_always_passes(self):
        assert self._call("delete_file", mode="none") is False

    def test_mode_all_always_gates(self):
        assert self._call("list_dir", _ann(read_only=True), mode="all") is True

    def test_always_deny_returns_none(self):
        assert self._call("delete_file", deny={"delete_file"}) is None

    def test_always_allow_returns_false(self):
        assert self._call("delete_file", mode="all", allow={"delete_file"}) is False

    def test_deny_pattern_returns_none(self):
        assert self._call("delete_users", dp=["delete_*"]) is None

    def test_allow_pattern_returns_false(self):
        assert self._call("list_files", mode="all", ap=["list_*"]) is False

    def test_deny_pattern_before_allow_pattern(self):
        # deny_pattern checked before allow_pattern
        assert self._call("delete_file", deny={"delete_file"}, ap=["delete_*"]) is None

    def test_read_only_hint_skips_approval(self):
        assert self._call("read_file", _ann(read_only=True)) is False

    def test_destructive_hint_requires_approval(self):
        assert self._call("some_tool", _ann(destructive=True)) is True

    def test_annotated_mode_destructive_hint(self):
        assert self._call("write_file", _ann(destructive=True), mode="annotated") is True
        assert self._call("write_file", _ann(), mode="annotated") is False

    def test_write_pattern_heuristic(self):
        names = [
            "write_file",
            "delete_record",
            "create_user",
            "update_config",
            "remove_entry",
            "exec_command",
            "insert_row",
            "deploy_app",
        ]
        for name in names:
            assert self._call(name) is True, f"{name!r} should require approval"

    def test_read_pattern_heuristic_no_approval(self):
        names = ["list_files", "get_record", "fetch_data", "show_logs", "search_index"]
        for name in names:
            assert self._call(name, _ann()) is False, f"{name!r} should skip approval"

    def test_case_insensitive_deny(self):
        assert self._call("DELETE_FILE", deny={"delete_file"}) is None

    def test_case_insensitive_allow(self):
        assert self._call("READ_FILE", mode="all", allow={"read_file"}) is False

    def test_fnmatch_pattern_case_insensitive(self):
        # patterns are pre-lowercased; name is compared lowercased
        assert self._call("Delete_Users", dp=["delete_*"]) is None


# ─────────────────────────────────────────────────────────────────────────────
# _deny helper
# ─────────────────────────────────────────────────────────────────────────────


class TestDenyHelper:
    def test_returns_error_result(self):
        result = _deny("blocked")
        assert result.isError is True
        assert any("blocked" in c.text for c in result.content if hasattr(c, "text"))


# ─────────────────────────────────────────────────────────────────────────────
# _build_elicitation_message
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildElicitationMessage:
    def test_includes_server_and_tool(self):
        msg = _build_elicitation_message(
            server_name="filesystem",
            tool_name="write_file",
            tool_args={"path": "/tmp/test.txt"},
            description="Write content to a file",
            annotations=None,
        )
        assert "filesystem" in msg
        assert "write_file" in msg
        assert "/tmp/test.txt" in msg

    def test_includes_risk_level(self):
        msg = _build_elicitation_message("s", "t", {}, "", None, risk="high")
        assert "HIGH" in msg
        assert "🔴" in msg

    def test_destructive_hint_warning_shown(self):
        msg = _build_elicitation_message("s", "t", {}, "", _ann(destructive=True))
        assert "destructive" in msg.lower()

    def test_read_only_hint_shown(self):
        msg = _build_elicitation_message("s", "t", {}, "", _ann(read_only=True))
        assert "read-only" in msg.lower()

    def test_long_args_truncated(self):
        big_args = {"data": "x" * 1000}
        msg = _build_elicitation_message("s", "t", big_args, "", None)
        assert len(msg) < 1800

    def test_empty_args_no_code_block(self):
        msg = _build_elicitation_message("s", "t", {}, "", None)
        assert "```" not in msg


# ─────────────────────────────────────────────────────────────────────────────
# ApprovalMiddleware.on_call_tool
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMiddlewareOnCallTool:
    async def test_read_only_tool_passes_through(self):
        mw = _middleware(mode="destructive", server_name="test")
        mw.tool_registry["list_files"] = mt.Tool(
            name="list_files", inputSchema={}, annotations=_ann(read_only=True)
        )
        ctx = _make_context("list_files")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_write_tool_approved_passes_through(self):
        mw = _middleware(mode="destructive", server_name="test")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file", elicit_result=AcceptedElicitation(data=True))
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert not result.isError

    async def test_approval_cache_skips_second_prompt(self):
        engine = AsyncMock()
        engine.request_approval = AsyncMock(return_value=True)
        mw = _middleware(
            mode="destructive",
            server_name="test",
            engine=engine,
            approval_ttl_seconds=60,
        )
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        ctx1 = _make_context("write_file", {"path": "/tmp/a", "content": "x"})
        ctx2 = _make_context("write_file", {"path": "/tmp/a", "content": "x"})

        await mw.on_call_tool(ctx1, call_next)
        await mw.on_call_tool(ctx2, call_next)

        assert engine.request_approval.await_count == 1
        assert call_next.await_count == 2

    async def test_high_risk_double_confirmation_requests_twice(self):
        engine = AsyncMock()
        engine.request_approval = AsyncMock(side_effect=[True, True])
        mw = _middleware(
            mode="destructive",
            server_name="test",
            engine=engine,
            high_risk_requires_double_confirmation=True,
        )
        mw.tool_registry["delete_file"] = mt.Tool(
            name="delete_file",
            inputSchema={},
            annotations=_ann(destructive=True),
        )
        ctx = _make_context("delete_file", {"path": "/tmp/x"})
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await mw.on_call_tool(ctx, call_next)

        assert not result.isError
        assert engine.request_approval.await_count == 2

    async def test_write_tool_denied_returns_error(self):
        mw = _middleware(mode="destructive", server_name="test")
        mw.tool_registry["delete_file"] = mt.Tool(name="delete_file", inputSchema={})
        ctx = _make_context("delete_file", elicit_result=AcceptedElicitation(data=False))
        call_next = AsyncMock()

        result = await mw.on_call_tool(ctx, call_next)

        call_next.assert_not_awaited()
        assert result.isError is True

    async def test_declined_elicitation_returns_error(self):
        mw = _middleware(mode="destructive", server_name="test")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file", elicit_result=DeclinedElicitation())

        result = await mw.on_call_tool(ctx, AsyncMock())

        assert result.isError is True

    async def test_cancelled_elicitation_returns_error(self):
        mw = _middleware(mode="destructive", server_name="test")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file", elicit_result=CancelledElicitation())

        result = await mw.on_call_tool(ctx, AsyncMock())

        assert result.isError is True

    async def test_always_deny_blocks_without_elicitation(self):
        mw = _middleware(mode="destructive", always_deny=["dangerous_tool"], server_name="test")
        ctx = _make_context("dangerous_tool")
        call_next = AsyncMock()

        result = await mw.on_call_tool(ctx, call_next)

        assert result.isError is True
        call_next.assert_not_awaited()

    async def test_indeterminate_retries_then_approves(self):
        engine = AsyncMock()
        engine.request_approval = AsyncMock(side_effect=[None, True])
        mw = _middleware(
            mode="destructive",
            server_name="test",
            engine=engine,
            approval_retry_attempts=2,
            approval_retry_initial_backoff_seconds=0,
        )
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file", {"path": "/tmp/x"})
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        result = await mw.on_call_tool(ctx, call_next)

        assert result.isError is False
        assert engine.request_approval.await_count == 2
        call_next.assert_awaited_once()

    async def test_dedupe_arg_subset_uses_configured_keys(self):
        engine = AsyncMock()
        engine.request_approval = AsyncMock(return_value=True)
        mw = _middleware(
            mode="destructive",
            server_name="test",
            engine=engine,
            approval_ttl_seconds=60,
            approval_dedupe_key_fields=["tool", "args"],
            approval_dedupe_arg_keys=["path"],
        )
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        ctx1 = _make_context("write_file", {"path": "/tmp/a", "content": "x"})
        ctx2 = _make_context("write_file", {"path": "/tmp/a", "content": "y"})

        await mw.on_call_tool(ctx1, call_next)
        await mw.on_call_tool(ctx2, call_next)

        # second call hits approval cache because only "path" participates in dedupe key
        assert engine.request_approval.await_count == 1
        assert ctx1.fastmcp_context.elicit.await_count == 0
        assert ctx2.fastmcp_context.elicit.await_count == 0

    async def test_deny_pattern_blocks_without_elicitation(self):
        mw = _middleware(mode="destructive", deny_patterns=["delete_*"], server_name="test")
        ctx = _make_context("delete_user")
        call_next = AsyncMock()

        result = await mw.on_call_tool(ctx, call_next)

        assert result.isError is True
        call_next.assert_not_awaited()

    async def test_allow_pattern_bypasses_elicitation(self):
        mw = _middleware(mode="all", allow_patterns=["list_*"], server_name="test")
        ctx = _make_context("list_users")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_always_allow_bypasses_elicitation(self):
        mw = _middleware(mode="all", always_allow=["safe_tool"], server_name="test")
        ctx = _make_context("safe_tool")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_mode_none_passes_everything(self):
        mw = _middleware(mode="none", server_name="test")
        ctx = _make_context("delete_everything")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_no_fastmcp_context_returns_error(self):
        mw = _middleware(mode="destructive", server_name="test")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        msg = mt.CallToolRequestParams(name="write_file", arguments={})
        ctx = MiddlewareContext(message=msg, fastmcp_context=None)

        result = await mw.on_call_tool(ctx, AsyncMock())

        assert result.isError is True

    async def test_elicitation_not_supported_returns_error(self):
        mw = _middleware(mode="destructive", server_name="test")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file", supports_elicitation=False)

        result = await mw.on_call_tool(ctx, AsyncMock())

        assert result.isError is True

    async def test_custom_annotation_override_marks_tool_destructive(self):
        mw = _middleware(
            mode="annotated",
            custom_annotations={"ambiguous": {"destructiveHint": True}},
            server_name="test",
        )
        mw.tool_registry["ambiguous"] = mt.Tool(name="ambiguous", inputSchema={})
        ctx = _make_context("ambiguous", elicit_result=AcceptedElicitation(data=True))
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        # In annotated mode, only destructiveHint=true triggers elicitation.
        # Our custom annotation should have triggered it.
        ctx.fastmcp_context.elicit.assert_awaited_once()

    async def test_custom_annotation_marks_tool_read_only(self):
        mw = _middleware(
            mode="destructive",
            custom_annotations={"write_file": {"readOnlyHint": True}},
            server_name="test",
        )
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        # readOnlyHint=True should bypass approval even with write name
        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run mode
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDryRunMode:
    async def test_dry_run_passes_even_blocked_tools(self):
        mw = _middleware(
            mode="destructive", always_deny=["delete_all"], dry_run=True, server_name="test"
        )
        ctx = _make_context("delete_all")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0

    async def test_dry_run_passes_write_tools_without_elicitation(self):
        mw = _middleware(mode="destructive", dry_run=True, server_name="test")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        call_next.assert_awaited_once()
        assert ctx.fastmcp_context.elicit.await_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Audit logging
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAuditLogging:
    async def test_approved_call_logged(self, tmp_path):
        log_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=log_path)
        mw = _middleware(mode="destructive", audit=audit, server_name="fs")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file", elicit_result=AcceptedElicitation(data=True))
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        import json

        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["decision"] == "approved"
        assert records[0]["tool"] == "write_file"
        assert records[0]["server"] == "fs"

    async def test_blocked_call_logged(self, tmp_path):
        log_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=log_path)
        mw = _middleware(
            mode="destructive", always_deny=["bad_tool"], audit=audit, server_name="fs"
        )
        ctx = _make_context("bad_tool")

        await mw.on_call_tool(ctx, AsyncMock())

        import json

        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert records[0]["decision"] == "blocked"

    async def test_passed_call_logged(self, tmp_path):
        log_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=log_path)
        mw = _middleware(mode="destructive", audit=audit, server_name="fs")
        mw.tool_registry["list_files"] = mt.Tool(
            name="list_files", inputSchema={}, annotations=_ann(read_only=True)
        )
        ctx = _make_context("list_files")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        import json

        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert records[0]["decision"] == "passed"

    async def test_denied_call_logged(self, tmp_path):
        log_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=log_path)
        mw = _middleware(mode="destructive", audit=audit, server_name="fs")
        mw.tool_registry["delete_file"] = mt.Tool(name="delete_file", inputSchema={})
        ctx = _make_context("delete_file", elicit_result=AcceptedElicitation(data=False))

        await mw.on_call_tool(ctx, AsyncMock())

        import json

        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert records[0]["decision"] == "denied"

    async def test_dry_run_logged_correctly(self, tmp_path):
        log_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=log_path, dry_run=True)
        mw = _middleware(mode="destructive", dry_run=True, audit=audit, server_name="fs")
        mw.tool_registry["write_file"] = mt.Tool(name="write_file", inputSchema={})
        ctx = _make_context("write_file")
        call_next = AsyncMock(return_value=mt.CallToolResult(content=[]))

        await mw.on_call_tool(ctx, call_next)

        import json

        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert records[0]["decision"] == "dry_run"
        assert records[0]["dry_run"] is True
