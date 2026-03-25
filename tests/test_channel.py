"""Tests for the ChannelServer SDK (FastMCP-native)."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

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
        assert msg.message.root.method == "notifications/tools/list_changed"

    def test_builds_permission_verdict(self):
        msg = _notif(_PERMISSION_VERDICT_METHOD, {"request_id": "abcde", "behavior": "allow"})
        root = msg.message.root
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
        assert ch._content_transformer is None
        assert ch._shutdown_hooks == []
        assert ch._write_stream is None
        assert isinstance(ch.server, FastMCP)

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

    def test_server_is_fastmcp(self):
        ch = ChannelServer("myapp")
        assert isinstance(ch.server, FastMCP)
        assert ch.server.name == "myapp"


# ── FastMCP delegation ───────────────────────────────────────────


class TestFastMCPDelegation:
    async def test_mount(self):
        ch = ChannelServer("main")
        child = FastMCP("child")

        @child.tool()
        def greet(name: str) -> str:
            return f"hello {name}"

        ch.mount(child)
        assert await ch.server.get_tool("greet") is not None

    async def test_mount_with_namespace(self):
        ch = ChannelServer("main")
        child = FastMCP("child")

        @child.tool()
        def greet(name: str) -> str:
            return f"hello {name}"

        ch.mount(child, namespace="svc")
        assert await ch.server.get_tool("svc_greet") is not None

    async def test_tool_decorator(self):
        ch = ChannelServer("test")

        @ch.tool()
        def ping() -> str:
            return "pong"

        assert await ch.server.get_tool("ping") is not None

    def test_add_middleware(self):
        from mcp_extras.middleware import ApprovalMiddleware

        ch = ChannelServer("test")
        initial_count = len(ch.server.middleware)
        mw = ApprovalMiddleware(mode="none", server_name="test")
        ch.add_middleware(mw)
        assert len(ch.server.middleware) == initial_count + 1


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

        params = mock_ws.send.call_args[0][0].message.root.params
        assert params["content"] == "my *** data"
        assert params["meta"]["masked"] == "true"

    async def test_no_transformer_passes_through(self):
        ch = ChannelServer("test")
        await ch.notify("raw content")

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.call_args[0][0].message.root.params["content"] == "raw content"


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
        _, meta = ch._queue.get_nowait()
        assert meta == {}

    async def test_drops_when_full(self):
        ch = ChannelServer("test", queue_size=1)
        await ch.notify("first")
        await ch.notify("second")
        assert ch._queue.qsize() == 1
        content, _ = ch._queue.get_nowait()
        assert content == "first"

    async def test_multiple_events_in_order(self):
        ch = ChannelServer("test", queue_size=10)
        for i in range(5):
            await ch.notify(f"event-{i}")
        assert ch._queue.qsize() == 5
        for i in range(5):
            content, _ = ch._queue.get_nowait()
            assert content == f"event-{i}"

    async def test_meta_none_becomes_empty(self):
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
        params = mock_ws.send.call_args[0][0].message.root.params
        assert params["content"] == "hello world"
        assert params["meta"]["type"] == "test"

    async def test_handles_tools_changed(self):
        ch = ChannelServer("test")
        await ch.signal_tools_changed()

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.call_args[0][0].message.root.method == "notifications/tools/list_changed"

    async def test_sets_write_stream(self):
        ch = ChannelServer("test")
        mock_ws = AsyncMock()
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
        contents = [c.args[0].message.root.params["content"] for c in mock_ws.send.call_args_list]
        assert contents == ["first", "second", "third"]

    async def test_send_failure_does_not_crash(self):
        ch = ChannelServer("test")
        await ch.notify("will fail")
        await ch.notify("will succeed")

        mock_ws = AsyncMock()
        mock_ws.send.side_effect = [RuntimeError("broken"), None]
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task
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
        methods = [c.args[0].message.root.method for c in mock_ws.send.call_args_list]
        assert methods[0] == _CHANNEL_METHOD
        assert methods[1] == "notifications/tools/list_changed"
        assert methods[2] == _CHANNEL_METHOD


# ── Init options ─────────────────────────────────────────────────


class TestInitOptions:
    def test_basic_channel(self):
        ch = ChannelServer("test")
        opts = ch._build_init_options()
        assert opts is not None

    def test_with_permission_relay(self):
        ch = ChannelServer("test", permission_relay=True)
        opts = ch._build_init_options()
        assert opts is not None


# ── Permission verdict ────────────────────────────────────────────


class TestPermissionVerdict:
    async def test_send_allow(self):
        ch = ChannelServer("test", permission_relay=True)
        mock_ws = AsyncMock()
        ch._write_stream = mock_ws

        await ch.send_permission_verdict("abcde", "allow")

        params = mock_ws.send.call_args[0][0].message.root.params
        assert params["request_id"] == "abcde"
        assert params["behavior"] == "allow"

    async def test_send_deny(self):
        ch = ChannelServer("test", permission_relay=True)
        mock_ws = AsyncMock()
        ch._write_stream = mock_ws

        await ch.send_permission_verdict("fghij", "deny")
        assert mock_ws.send.call_args[0][0].message.root.params["behavior"] == "deny"

    async def test_no_stream_is_noop(self):
        ch = ChannelServer("test")
        await ch.send_permission_verdict("abcde", "allow")

    async def test_send_failure_suppressed(self):
        ch = ChannelServer("test")
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = RuntimeError("broken")
        ch._write_stream = mock_ws
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
        handler = mock_signal.call_args_list[0].args[1]
        with pytest.raises(SystemExit):
            handler()
        assert called == ["a", "b"]


# ── Signal tools changed ─────────────────────────────────────────


class TestSignalToolsChanged:
    async def test_queues_signal(self):
        ch = ChannelServer("test")
        await ch.signal_tools_changed()
        assert ch._queue.qsize() == 1
        _, meta = ch._queue.get_nowait()
        assert meta.get("_tools_changed") == "true"

    async def test_suppressed_when_full(self):
        ch = ChannelServer("test", queue_size=1)
        await ch.notify("blocking")
        await ch.signal_tools_changed()
        assert ch._queue.qsize() == 1


# ── End-to-end ────────────────────────────────────────────────────


class TestEndToEnd:
    async def test_channel_event_notification_format(self):
        ch = ChannelServer("my-bot")
        await ch.notify("user says hello", meta={"sender": "alice", "chat_id": "123"})

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        root = mock_ws.send.call_args[0][0].message.root
        assert root.jsonrpc == "2.0"
        assert root.method == _CHANNEL_METHOD
        assert root.params["content"] == "user says hello"
        assert root.params["meta"]["sender"] == "alice"

    async def test_permission_verdict_format(self):
        ch = ChannelServer("my-bot", permission_relay=True)
        mock_ws = AsyncMock()
        ch._write_stream = mock_ws

        await ch.send_permission_verdict("xyzwk", "allow")

        root = mock_ws.send.call_args[0][0].message.root
        assert root.method == _PERMISSION_VERDICT_METHOD
        assert root.params["request_id"] == "xyzwk"

    async def test_mounted_service_tools_discoverable(self):
        ch = ChannelServer("main")
        svc = FastMCP("memory")

        @svc.tool()
        def memory_write(app: str, entity: str, name: str) -> str:
            return "ok"

        @svc.tool()
        def memory_read(app: str) -> str:
            return "[]"

        ch.mount(svc)

        assert await ch.server.get_tool("memory_write") is not None
        assert await ch.server.get_tool("memory_read") is not None

    async def test_namespaced_mount(self):
        ch = ChannelServer("main")
        svc = FastMCP("storage")

        @svc.tool()
        def save_file(path: str, content: str) -> str:
            return f"saved {path}"

        ch.mount(svc, namespace="fs")
        assert await ch.server.get_tool("fs_save_file") is not None

    async def test_channel_with_transformer_and_events(self):
        ch = ChannelServer("masked-bot")

        def redact_phones(content, meta):
            import re
            return re.sub(r"\d{10,}", "[REDACTED]", content), meta

        ch.set_content_transformer(redact_phones)
        await ch.notify("call 1234567890")
        await ch.notify("no phone")

        mock_ws = AsyncMock()
        drain_task = asyncio.create_task(ch._drain_notifications(mock_ws))
        await asyncio.sleep(0.05)
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert mock_ws.send.call_count == 2
        first = mock_ws.send.call_args_list[0].args[0].message.root.params["content"]
        assert "[REDACTED]" in first
        assert "1234567890" not in first
