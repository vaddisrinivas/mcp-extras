"""
Claude Code Channel SDK for Python.

Build MCP servers that push events into Claude Code sessions.
Supports one-way (alerts/webhooks) and two-way (chat bridges) channels,
plus permission relay for remote tool approval.

One-way channel::

    from mcp_extras.channel import ChannelServer

    ch = ChannelServer("webhook", instructions="Events from webhook channel...")
    # Push events from your HTTP handler, message queue, etc.
    await ch.notify("build failed on main", meta={"severity": "high"})
    await ch.run_stdio()

Two-way channel with reply tool::

    from mcp_extras.channel import ChannelServer
    from mcp.types import Tool

    ch = ChannelServer("my-chat", instructions="Reply with the reply tool.")
    ch.add_tool(Tool(name="reply", description="Send reply", inputSchema={...}))

    @ch.on_tool_call
    async def handle(name, arguments):
        if name == "reply":
            send_to_chat(arguments["chat_id"], arguments["text"])
            return [TextContent(type="text", text="sent")]

    await ch.run_stdio()

Permission relay::

    ch = ChannelServer("my-chat", permission_relay=True)

    @ch.on_permission_request
    async def handle(request_id, tool_name, description, input_preview):
        send_to_chat(f"Approve {tool_name}? Reply 'yes {request_id}' or 'no {request_id}'")

    # When user replies:
    await ch.send_permission_verdict(request_id, "allow")
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
    Resource,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
    Tool,
)

_CHANNEL_METHOD = "notifications/claude/channel"
_PERMISSION_REQUEST_METHOD = "notifications/claude/channel/permission_request"
_PERMISSION_VERDICT_METHOD = "notifications/claude/channel/permission"

ToolCallHandler = Callable[[str, dict], Awaitable[list[TextContent]]]
PermissionRequestHandler = Callable[[str, str, str, str], Awaitable[None]]
ContentTransformer = Callable[[str, dict], tuple[str, dict]]
ResourceListHandler = Callable[[], Awaitable[list[Resource]]]
ResourceTemplateListHandler = Callable[[], Awaitable[list[ResourceTemplate]]]
ResourceReadHandler = Callable[[Any], Awaitable[list[TextResourceContents]]]


def _notif(method: str, params: dict) -> SessionMessage:
    """Build an MCP SessionMessage notification."""
    return SessionMessage(
        JSONRPCMessage(root=JSONRPCNotification(jsonrpc="2.0", method=method, params=params))
    )


class ChannelServer:
    """Python SDK for building Claude Code channel servers.

    Wraps an MCP Server with ``claude/channel`` capability declaration,
    an async notification queue, and transport setup (stdio/SSE).

    Args:
        name: Server name (appears as ``source`` attribute on ``<channel>`` tags).
        instructions: Added to Claude's system prompt. Tell Claude what events
            to expect, whether to reply, and how.
        permission_relay: If True, declares ``claude/channel/permission`` capability
            so this channel can receive and relay tool approval prompts.
        queue_size: Max queued notifications before dropping.
    """

    def __init__(
        self,
        name: str,
        instructions: str = "",
        *,
        permission_relay: bool = False,
        queue_size: int = 256,
    ):
        self.name = name
        self._instructions = instructions
        self._permission_relay = permission_relay
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=queue_size)
        self._tools: list[Tool] = []
        self._tool_call_handler: ToolCallHandler | None = None
        self._permission_handler: PermissionRequestHandler | None = None
        self._content_transformer: ContentTransformer | None = None
        self._resource_list_handler: ResourceListHandler | None = None
        self._resource_template_handler: ResourceTemplateListHandler | None = None
        self._resource_read_handler: ResourceReadHandler | None = None
        self._shutdown_hooks: list[Callable] = []
        self._server: Server | None = None
        self._write_stream: Any = None

    # ── Configuration ────────────────────────────────────────────

    def add_tool(self, tool: Tool) -> None:
        """Register a tool that Claude can call back through this channel."""
        self._tools.append(tool)

    def on_tool_call(self, handler: ToolCallHandler) -> ToolCallHandler:
        """Register the handler for incoming tool calls (decorator or direct)."""
        self._tool_call_handler = handler
        return handler

    def on_permission_request(self, handler: PermissionRequestHandler) -> PermissionRequestHandler:
        """Register handler for permission relay requests from Claude Code."""
        self._permission_handler = handler
        return handler

    def set_content_transformer(self, fn: ContentTransformer) -> None:
        """Set a function that transforms (content, meta) before emission.

        Useful for RBAC masking, sanitization, etc.
        """
        self._content_transformer = fn

    def on_shutdown(self, hook: Callable) -> None:
        """Register a cleanup function called on SIGTERM/SIGINT."""
        self._shutdown_hooks.append(hook)

    def on_list_resources(self, handler: ResourceListHandler) -> ResourceListHandler:
        """Register handler for listing MCP resources."""
        self._resource_list_handler = handler
        return handler

    def on_list_resource_templates(
        self, handler: ResourceTemplateListHandler
    ) -> ResourceTemplateListHandler:
        """Register handler for listing MCP resource templates."""
        self._resource_template_handler = handler
        return handler

    def on_read_resource(self, handler: ResourceReadHandler) -> ResourceReadHandler:
        """Register handler for reading MCP resources."""
        self._resource_read_handler = handler
        return handler

    # ── Notification emission ────────────────────────────────────

    async def notify(self, content: str, meta: dict[str, str] | None = None) -> None:
        """Push a channel event to Claude Code.

        Args:
            content: Event body (becomes body of ``<channel>`` tag).
            meta: Key-value pairs (become attributes on ``<channel>`` tag).
        """
        try:
            self._queue.put_nowait((content, meta or {}))
        except asyncio.QueueFull:
            print(f"[{self.name}] notify queue full, dropping", file=sys.stderr, flush=True)

    async def send_permission_verdict(self, request_id: str, behavior: str) -> None:
        """Send an allow/deny verdict back to Claude Code.

        Args:
            request_id: The five-letter ID from the permission request.
            behavior: ``"allow"`` or ``"deny"``.
        """
        if self._write_stream is None:
            return
        with contextlib.suppress(Exception):
            await self._write_stream.send(
                _notif(_PERMISSION_VERDICT_METHOD, {
                    "request_id": request_id,
                    "behavior": behavior,
                })
            )

    # ── Internal: notification drain ─────────────────────────────

    async def _drain_notifications(self, write_stream: Any) -> None:
        """Drain notification queue and write to MCP stream."""
        self._write_stream = write_stream
        while True:
            content, meta = await self._queue.get()

            # tools_changed is a special internal signal
            if meta.get("_tools_changed"):
                with contextlib.suppress(Exception):
                    await write_stream.send(
                        _notif("notifications/tools/list_changed", {})
                    )
                continue

            # Apply content transformer (RBAC masking, sanitization, etc.)
            if self._content_transformer:
                content, meta = self._content_transformer(content, meta)

            try:
                await write_stream.send(
                    _notif(_CHANNEL_METHOD, {"content": content, "meta": meta})
                )
            except Exception as e:
                print(f"[{self.name}] notify failed: {e}", file=sys.stderr, flush=True)

    # ── Internal: MCP server setup ───────────────────────────────

    def _build_server(self) -> Server:
        """Create the MCP Server with channel capabilities."""
        experimental: dict[str, dict] = {"claude/channel": {}}
        if self._permission_relay:
            experimental["claude/channel/permission"] = {}

        capabilities: dict[str, Any] = {"experimental": experimental}
        if self._tools:
            capabilities["tools"] = {}

        server = Server(
            self.name,
            instructions=self._instructions or None,
        )

        # Tool handlers
        if self._tools:
            @server.list_tools()
            async def _list_tools() -> list[Tool]:
                return self._tools

            @server.call_tool()
            async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
                if self._tool_call_handler:
                    return await self._tool_call_handler(name, arguments)
                return [TextContent(type="text", text=f"Error: no handler for tool '{name}'")]

        # Resource handlers
        if self._resource_list_handler:
            @server.list_resources()
            async def _list_resources() -> list[Resource]:
                return await self._resource_list_handler()

        if self._resource_template_handler:
            @server.list_resource_templates()
            async def _list_templates() -> list[ResourceTemplate]:
                return await self._resource_template_handler()

        if self._resource_read_handler:
            @server.read_resource()
            async def _read_resource(uri: Any) -> list[TextResourceContents]:
                return await self._resource_read_handler(uri)

        self._server = server
        return server

    def _create_init_options(self, server: Server) -> Any:
        """Build initialization options with channel capability."""
        experimental: dict[str, dict] = {"claude/channel": {}}
        if self._permission_relay:
            experimental["claude/channel/permission"] = {}
        return server.create_initialization_options(
            experimental_capabilities=experimental,
        )

    def _install_signals(self) -> None:
        """Install SIGTERM/SIGINT handlers for graceful shutdown."""
        def _shutdown(*_args: Any) -> None:
            for hook in self._shutdown_hooks:
                with contextlib.suppress(Exception):
                    hook()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

    # ── Transports ───────────────────────────────────────────────

    async def run_stdio(
        self,
        extra_tasks: list[Callable[[], Any]] | None = None,
    ) -> None:
        """Run as a stdio channel (Claude Code spawns this as subprocess).

        Args:
            extra_tasks: Additional async callables to run in the task group
                (e.g. adapter.connect, file watchers).
        """
        server = self._build_server()
        init_opts = self._create_init_options(server)
        self._install_signals()

        async with stdio_server() as (rs, ws), anyio.create_task_group() as tg:
            tg.start_soon(server.run, rs, ws, init_opts)
            tg.start_soon(self._drain_notifications, ws)
            for task in extra_tasks or []:
                tg.start_soon(task)

    async def run_sse(
        self,
        host: str = "127.0.0.1",
        port: int = 3000,
        extra_routes: list | None = None,
        extra_tasks: list[Callable[[], Any]] | None = None,
    ) -> None:
        """Run as an SSE channel with optional extra Starlette routes.

        Args:
            host: Bind address.
            port: Bind port.
            extra_routes: Additional Starlette Route objects to mount.
            extra_tasks: Additional async callables to run in the task group.
        """
        import uvicorn
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route

        server = self._build_server()
        init_opts = self._create_init_options(server)
        self._install_signals()

        sset = SseServerTransport("/messages/")

        async def _sse(req: Any) -> None:
            async with (
                sset.connect_sse(req.scope, req.receive, req._send) as (rs, ws),
                anyio.create_task_group() as tg,
            ):
                tg.start_soon(server.run, rs, ws, init_opts)
                tg.start_soon(self._drain_notifications, ws)

        routes = [
            Route("/health", endpoint=lambda r: JSONResponse({"status": "ok"})),
            Route("/sse", endpoint=_sse),
            Mount("/messages/", app=sset.handle_post_message),
        ]
        if extra_routes:
            routes.extend(extra_routes)

        app = Starlette(routes=routes)

        async with anyio.create_task_group() as tg:
            tg.start_soon(
                uvicorn.Server(
                    uvicorn.Config(app, host=host, port=port, log_level="warning")
                ).serve
            )
            for task in extra_tasks or []:
                tg.start_soon(task)

    # ── Convenience: signal tools_changed ────────────────────────

    async def signal_tools_changed(self) -> None:
        """Signal Claude Code that the tool list has changed (triggers re-list)."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(("", {"_tools_changed": "true"}))
