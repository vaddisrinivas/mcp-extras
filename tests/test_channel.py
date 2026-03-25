"""Tests for the ChannelServer SDK."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import Resource, ResourceTemplate, TextContent, TextResourceContents, Tool

from mcp_extras.channel import (
    _CHANNEL_METHOD,
    _PERMISSION_VERDICT_METHOD,
    ChannelServer,
    _notif,
)

# ── _notif helper ────────────────────────────────────────────────


class TestNotifHelper:
    def test_builds_session_message(self):
        msg = _notif("notifications/claude/channel", {"content": "hello", "meta": {"k": "v"}})
        root = msg.message.root
        assert root.method == "notifications/claude/channel"
        assert root.params["content"] == "hello"
        assert root.params["meta"]["k"] == "v"

    def test_builds_tools_list_changed(self):
        msg = _notif("notifications/tools/list_changed", {})
        root = msg.message.root
        assert root.method == "notifications/tools/list_changed"
        assert root.params == {}

    def test_builds_permission_verdict(self):
        msg = _notif(
            _PERMISSION_VERDICT_METHOD,
            {"request_id": "abcde", "behavior": "allow"},
        )
        root = msg.message.root
        assert root.method == _PERMISSION_VERDICT_METHOD
        assert root.params["request_id"] == "abcde"
        assert root.params["behavior"] == "allow"

    def test_empty_meta(self):
        msg = _notif(_CHANNEL_METHOD, {"content": "hi", "meta": {}})
        assert msg.message.root.params["meta"] == {}


# ── ChannelServer init ───────────────────────────────────────────


class TestChannelServerInit:
    def test_defaults(self):
        ch = ChannelServer("test")
        assert ch.name == "test"
        assert ch._instructions == ""
        assert ch._permission_relay is False
        assert ch._tools == []
        assert ch._tool_call_handler is None
        assert ch._permission_handler is None
        assert ch._content_transformer is None
        assert ch._resource_list_handler is None
        assert ch._resource_template_handler is None
        assert ch._resource_read_handler is None
        assert ch._shutdown_hooks == []
        assert ch._server is None
        assert ch._write_stream is None

    def test_with_all_options(self):
        ch = ChannelServer(
            "webhook",
            instructions="Events arrive as <channel>...",
            permission_relay=True,
            queue_size=10,
        )
        assert ch.name == "webhook"
        assert ch._instructions == "Events arrive as <channel>..."
        assert ch._permission_relay is True
        assert ch._queue.maxsize == 10

    def test_default_queue_size(self):
        ch = ChannelServer("test")
        assert ch._queue.maxsize == 256


# ── Tool registration ────────────────────────────────────────────


class TestToolRegistration:
    def test_add_single_tool(self):
        ch = ChannelServer("test")
        tool = Tool(
            name="reply",
            description="Send reply",
            inputSchema={"type": "object", "properties": {"text": {"type": "string"}}},
        )
        ch.add_tool(tool)
        assert len(ch._tools) == 1
        assert ch._tools[0].name == "reply"

    def test_add_multiple_tools(self):
        ch = ChannelServer("test")
        ch.add_tool(Tool(name="reply", description="Reply", inputSchema={"type": "object"}))
        ch.add_tool(Tool(name="react", description="React", inputSchema={"type": "object"}))
        assert len(ch._tools) == 2
        assert ch._tools[0].name == "reply"
        assert ch._tools[1].name == "react"

    def test_on_tool_call_as_decorator(self):
        ch = ChannelServer("test")

        @ch.on_tool_call
        async def handler(name, args):
            return [TextContent(type="text", text="ok")]

        assert ch._tool_call_handler is handler

    def test_on_tool_call_direct_assignment(self):
        ch = ChannelServer("test")

        async def handler(name, args):
            return [TextContent(type="text", text="ok")]

        ch.on_tool_call(handler)
        assert ch._tool_call_handler is handler


# ── Permission request handler ────────────────────────────────────


class TestPermissionRequestHandler:
    def test_decorator(self):
        ch = ChannelServer("test")

        @ch.on_permission_request
        async def handler(request_id, tool_name, description, input_preview):
            pass

        assert ch._permission_handler is handler

    def test_direct_assignment(self):
        ch = ChannelServer("test")

        async def handler(request_id, tool_name, description, input_preview):
            pass

        ch.on_permission_request(handler)
        assert ch._permission_handler is handler


# ── Content transformer ──────────────────────────────────────────


class TestContentTransformer:
    def test_set_transformer(self):
        ch = ChannelServer("test")

        def mask(content, meta):
            return content.upper(), meta

        ch.set_content_transformer(mask)
        assert ch._content_transformer is mask

    async def test_transformer_applied_during_drain(self):
        ch = ChannelServer("test")

        def mask(content, meta):
            return content.replace("secret", "***"), {"masked": "true", **meta}

        ch.set_content_transformer(mask)
        await ch.notify("my secret data", meta={"sender": "alice"})

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.called
        sent = mock_ws.send.call_args[0][0]
        params = sent.message.root.params
        assert params["content"] == "my *** data"
        assert params["meta"]["masked"] == "true"
        assert params["meta"]["sender"] == "alice"

    async def test_no_transformer_passes_through(self):
        ch = ChannelServer("test")
        await ch.notify("raw content")

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        params = mock_ws.send.call_args[0][0].message.root.params
        assert params["content"] == "raw content"


# ── Notify ────────────────────────────────────────────────────────


class TestNotify:
    async def test_queues_event_with_meta(self):
        ch = ChannelServer("test", queue_size=5)
        await ch.notify("build failed", meta={"severity": "high"})
        assert ch._queue.qsize() == 1
        content, meta = ch._queue.get_nowait()
        assert content == "build failed"
        assert meta == {"severity": "high"}

    async def test_default_meta_is_empty_dict(self):
        ch = ChannelServer("test")
        await ch.notify("hello")
        content, meta = ch._queue.get_nowait()
        assert content == "hello"
        assert meta == {}

    async def test_drops_when_full_without_raising(self):
        ch = ChannelServer("test", queue_size=1)
        await ch.notify("first")
        await ch.notify("second")  # should not raise
        assert ch._queue.qsize() == 1
        content, _ = ch._queue.get_nowait()
        assert content == "first"

    async def test_multiple_events_queued_in_order(self):
        ch = ChannelServer("test", queue_size=10)
        for i in range(5):
            await ch.notify(f"event-{i}")
        assert ch._queue.qsize() == 5
        for i in range(5):
            content, _ = ch._queue.get_nowait()
            assert content == f"event-{i}"

    async def test_meta_none_becomes_empty_dict(self):
        ch = ChannelServer("test")
        await ch.notify("test", meta=None)
        _, meta = ch._queue.get_nowait()
        assert meta == {}


# ── Drain notifications ──────────────────────────────────────────


class TestDrainNotifications:
    async def test_sends_channel_notification(self):
        ch = ChannelServer("test")
        await ch.notify("hello world", meta={"type": "test"})

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        mock_ws.send.assert_called_once()
        sent = mock_ws.send.call_args[0][0]
        params = sent.message.root.params
        assert params["content"] == "hello world"
        assert params["meta"]["type"] == "test"

    async def test_handles_tools_changed_signal(self):
        ch = ChannelServer("test")
        await ch.signal_tools_changed()

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        mock_ws.send.assert_called_once()
        sent = mock_ws.send.call_args[0][0]
        assert sent.message.root.method == "notifications/tools/list_changed"

    async def test_sets_write_stream(self):
        ch = ChannelServer("test")
        mock_ws = AsyncMock()
        assert ch._write_stream is None

        await ch.notify("test")
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert ch._write_stream is mock_ws

    async def test_multiple_events_drained_in_order(self):
        ch = ChannelServer("test")
        await ch.notify("first")
        await ch.notify("second")
        await ch.notify("third")

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.call_count == 3
        contents = [
            call.args[0].message.root.params["content"]
            for call in mock_ws.send.call_args_list
        ]
        assert contents == ["first", "second", "third"]

    async def test_send_failure_does_not_crash_drain(self):
        ch = ChannelServer("test")
        await ch.notify("will fail")
        await ch.notify("will succeed")

        mock_ws = AsyncMock()
        mock_ws.send.side_effect = [RuntimeError("broken pipe"), None]
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.call_count == 2

    async def test_tools_changed_send_failure_suppressed(self):
        ch = ChannelServer("test")
        await ch.signal_tools_changed()
        await ch.notify("after signal")

        mock_ws = AsyncMock()
        mock_ws.send.side_effect = [RuntimeError("broken"), None]
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        # Both attempted: tools_changed (failed silently) + channel event
        assert mock_ws.send.call_count == 2

    async def test_interleaved_tools_changed_and_events(self):
        ch = ChannelServer("test")
        await ch.notify("event-1")
        await ch.signal_tools_changed()
        await ch.notify("event-2")

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.call_count == 3
        methods = [call.args[0].message.root.method for call in mock_ws.send.call_args_list]
        assert methods[0] == _CHANNEL_METHOD
        assert methods[1] == "notifications/tools/list_changed"
        assert methods[2] == _CHANNEL_METHOD


# ── Server build ─────────────────────────────────────────────────


class TestBuildServer:
    def test_one_way_channel(self):
        ch = ChannelServer("webhook", instructions="one-way alerts")
        server = ch._build_server()
        assert server is not None
        assert ch._server is server

    def test_two_way_with_tools(self):
        ch = ChannelServer("chat")
        ch.add_tool(
            Tool(name="reply", description="Reply", inputSchema={"type": "object", "properties": {}})
        )
        server = ch._build_server()
        assert server is not None

    def test_with_resource_handlers(self):
        ch = ChannelServer("test")

        @ch.on_list_resources
        async def list_res():
            return []

        @ch.on_read_resource
        async def read_res(uri):
            return []

        server = ch._build_server()
        assert server is not None

    def test_with_resource_template_handler(self):
        ch = ChannelServer("test")

        @ch.on_list_resource_templates
        async def list_tpl():
            return []

        server = ch._build_server()
        assert server is not None

    def test_rebuild_replaces_server(self):
        ch = ChannelServer("test")
        s1 = ch._build_server()
        s2 = ch._build_server()
        assert ch._server is s2
        assert s1 is not s2


class TestCreateInitOptions:
    def test_basic_channel(self):
        ch = ChannelServer("test")
        server = ch._build_server()
        opts = ch._create_init_options(server)
        assert opts is not None

    def test_with_permission_relay(self):
        ch = ChannelServer("test", permission_relay=True)
        server = ch._build_server()
        opts = ch._create_init_options(server)
        assert opts is not None


# ── Tool call handler dispatch ────────────────────────────────────


class TestToolCallDispatch:
    def test_build_server_registers_tool_handlers_when_tools_present(self):
        ch = ChannelServer("chat")
        ch.add_tool(
            Tool(name="reply", description="Reply", inputSchema={"type": "object", "properties": {}})
        )

        called = []

        @ch.on_tool_call
        async def handler(name, args):
            called.append((name, args))
            return [TextContent(type="text", text="ok")]

        ch._build_server()
        # Handler is registered
        assert ch._tool_call_handler is handler

    def test_no_tools_no_handler_registration(self):
        ch = ChannelServer("webhook")
        ch._build_server()
        # Server built without tool handlers
        assert ch._tools == []


# ── Permission verdict ────────────────────────────────────────────


class TestPermissionVerdict:
    async def test_send_allow(self):
        ch = ChannelServer("test", permission_relay=True)
        mock_ws = AsyncMock()
        ch._write_stream = mock_ws

        await ch.send_permission_verdict("abcde", "allow")

        mock_ws.send.assert_called_once()
        sent = mock_ws.send.call_args[0][0]
        params = sent.message.root.params
        assert params["request_id"] == "abcde"
        assert params["behavior"] == "allow"

    async def test_send_deny(self):
        ch = ChannelServer("test", permission_relay=True)
        mock_ws = AsyncMock()
        ch._write_stream = mock_ws

        await ch.send_permission_verdict("fghij", "deny")

        sent = mock_ws.send.call_args[0][0]
        assert sent.message.root.params["behavior"] == "deny"

    async def test_no_stream_is_noop(self):
        ch = ChannelServer("test")
        # Should not raise when no write stream
        await ch.send_permission_verdict("abcde", "allow")

    async def test_send_failure_suppressed(self):
        ch = ChannelServer("test", permission_relay=True)
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = RuntimeError("broken")
        ch._write_stream = mock_ws

        # Should not raise
        await ch.send_permission_verdict("abcde", "allow")


# ── Shutdown hooks ────────────────────────────────────────────────


class TestShutdownHooks:
    def test_register_hooks(self):
        ch = ChannelServer("test")
        ch.on_shutdown(lambda: None)
        ch.on_shutdown(lambda: None)
        assert len(ch._shutdown_hooks) == 2

    def test_install_signals(self):
        ch = ChannelServer("test")
        with patch.object(signal, "signal") as mock_signal:
            ch._install_signals()
            calls = {c.args[0] for c in mock_signal.call_args_list}
            assert signal.SIGTERM in calls
            assert signal.SIGINT in calls

    def test_shutdown_handler_calls_hooks(self):
        ch = ChannelServer("test")
        called = []
        ch.on_shutdown(lambda: called.append("a"))
        ch.on_shutdown(lambda: called.append("b"))

        with patch.object(signal, "signal") as mock_signal:
            ch._install_signals()

        # Get the handler that was registered
        handler = mock_signal.call_args_list[0].args[1]

        with pytest.raises(SystemExit):
            handler()

        assert called == ["a", "b"]

    def test_shutdown_hook_exception_suppressed(self):
        ch = ChannelServer("test")
        ch.on_shutdown(lambda: (_ for _ in ()).throw(ValueError("boom")))
        ch.on_shutdown(lambda: None)  # second hook should still run

        with patch.object(signal, "signal") as mock_signal:
            ch._install_signals()

        handler = mock_signal.call_args_list[0].args[1]
        with pytest.raises(SystemExit):
            handler()


# ── Signal tools changed ─────────────────────────────────────────


class TestSignalToolsChanged:
    async def test_queues_signal(self):
        ch = ChannelServer("test")
        await ch.signal_tools_changed()
        assert ch._queue.qsize() == 1
        content, meta = ch._queue.get_nowait()
        assert meta.get("_tools_changed") == "true"
        assert content == ""

    async def test_suppressed_when_full(self):
        ch = ChannelServer("test", queue_size=1)
        await ch.notify("blocking")
        await ch.signal_tools_changed()  # should not raise
        assert ch._queue.qsize() == 1


# ── Resource handlers ─────────────────────────────────────────────


class TestResourceHandlers:
    def test_list_resources_decorator(self):
        ch = ChannelServer("test")

        @ch.on_list_resources
        async def list_res():
            return [Resource(name="test", uri="test://x", description="x")]

        assert ch._resource_list_handler is list_res

    def test_read_resource_decorator(self):
        ch = ChannelServer("test")

        @ch.on_read_resource
        async def read_res(uri):
            return [TextResourceContents(uri=uri, text="data", mimeType="text/plain")]

        assert ch._resource_read_handler is read_res

    def test_list_resource_templates_decorator(self):
        ch = ChannelServer("test")

        @ch.on_list_resource_templates
        async def list_tpl():
            return [
                ResourceTemplate(
                    name="memory",
                    uriTemplate="c3://memory/{app}",
                    description="Memory",
                )
            ]

        assert ch._resource_template_handler is list_tpl

    def test_no_resource_handlers_by_default(self):
        ch = ChannelServer("test")
        assert ch._resource_list_handler is None
        assert ch._resource_template_handler is None
        assert ch._resource_read_handler is None


# ── End-to-end: full drain cycle ──────────────────────────────────


class TestEndToEnd:
    async def test_channel_event_notification_format(self):
        """Verify the exact wire format of a channel notification."""
        ch = ChannelServer("my-bot")
        await ch.notify("user says hello", meta={"sender": "alice", "chat_id": "123"})

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        sent = mock_ws.send.call_args[0][0]
        root = sent.message.root
        assert root.jsonrpc == "2.0"
        assert root.method == _CHANNEL_METHOD
        assert root.params["content"] == "user says hello"
        assert root.params["meta"]["sender"] == "alice"
        assert root.params["meta"]["chat_id"] == "123"

    async def test_permission_verdict_notification_format(self):
        """Verify the exact wire format of a permission verdict."""
        ch = ChannelServer("my-bot", permission_relay=True)
        mock_ws = AsyncMock()
        ch._write_stream = mock_ws

        await ch.send_permission_verdict("xyzwk", "allow")

        sent = mock_ws.send.call_args[0][0]
        root = sent.message.root
        assert root.jsonrpc == "2.0"
        assert root.method == _PERMISSION_VERDICT_METHOD
        assert root.params["request_id"] == "xyzwk"
        assert root.params["behavior"] == "allow"

    async def test_two_way_channel_lifecycle(self):
        """Build a two-way channel with tool, notify, and drain."""
        ch = ChannelServer("chat", instructions="Reply with the reply tool.")
        ch.add_tool(
            Tool(
                name="reply",
                description="Send a reply",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chat_id": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["chat_id", "text"],
                },
            )
        )

        replies = []

        @ch.on_tool_call
        async def handle(name, arguments):
            replies.append((name, arguments))
            return [TextContent(type="text", text="sent")]

        # Simulate inbound message
        await ch.notify("hi from user", meta={"chat_id": "42", "sender": "bob"})

        # Drain
        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        # Verify notification went out
        assert mock_ws.send.call_count == 1
        params = mock_ws.send.call_args[0][0].message.root.params
        assert params["meta"]["chat_id"] == "42"

        # Simulate Claude calling the reply tool
        result = await ch._tool_call_handler("reply", {"chat_id": "42", "text": "hello back"})
        assert result[0].text == "sent"
        assert replies == [("reply", {"chat_id": "42", "text": "hello back"})]

    async def test_channel_with_transformer_and_multiple_events(self):
        """Full cycle: notify, transform, drain multiple events."""
        ch = ChannelServer("masked-bot")

        def redact_phones(content, meta):
            import re
            return re.sub(r"\d{10,}", "[REDACTED]", content), meta

        ch.set_content_transformer(redact_phones)

        await ch.notify("call me at 1234567890", meta={"type": "msg"})
        await ch.notify("no phone here", meta={"type": "msg"})

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.call_count == 2
        first = mock_ws.send.call_args_list[0].args[0].message.root.params["content"]
        second = mock_ws.send.call_args_list[1].args[0].message.root.params["content"]
        assert "[REDACTED]" in first
        assert "1234567890" not in first
        assert second == "no phone here"
