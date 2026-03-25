"""
Claude Code Channel SDK for Python (FastMCP-native).

Build MCP servers that push events into Claude Code sessions.
Supports one-way (alerts/webhooks) and two-way (chat bridges),
permission relay, FastMCP mount() for service composition,
and middleware for tool gating.

One-way channel::

    from mcp_extras.channel import ChannelServer

    ch = ChannelServer("webhook", instructions="Events from webhook channel...")
    await ch.notify("build failed on main", meta={"severity": "high"})
    await ch.run_stdio()

Two-way with mounted services::

    from fastmcp import FastMCP
    from mcp_extras.channel import ChannelServer

    memory = FastMCP("memory")
    @memory.tool()
    def memory_write(app: str, entity: str, name: str) -> str: ...

    ch = ChannelServer("my-app", instructions="...")
    ch.mount(memory)  # memory tools available through the channel
    await ch.run_stdio()

With approval middleware::

    from mcp_extras import ApprovalMiddleware
    ch = ChannelServer("my-app")
    ch.add_middleware(ApprovalMiddleware(mode="destructive"))
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable
from typing import Any

import anyio
from fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
)

_CHANNEL_METHOD = "notifications/claude/channel"
_PERMISSION_REQUEST_METHOD = "notifications/claude/channel/permission_request"
_PERMISSION_VERDICT_METHOD = "notifications/claude/channel/permission"

ContentTransformer = Callable[[str, dict], tuple[str, dict]]


def _notif(method: str, params: dict) -> SessionMessage:
    """Build an MCP SessionMessage notification."""
    return SessionMessage(
        JSONRPCMessage(root=JSONRPCNotification(jsonrpc="2.0", method=method, params=params))
    )


class ChannelServer:
    """Python SDK for building Claude Code channel servers.

    Wraps a FastMCP server with ``claude/channel`` capability declaration,
    an async notification queue, and transport setup (stdio/SSE).
    Supports ``mount()``, ``add_middleware()``, and all FastMCP composition.

    Args:
        name: Server name (appears as ``source`` attribute on ``<channel>`` tags).
        instructions: Added to Claude's system prompt.
        permission_relay: If True, declares ``claude/channel/permission`` capability.
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
        self._content_transformer: ContentTransformer | None = None
        self._shutdown_hooks: list[Callable] = []
        self._write_stream: Any = None

        # The underlying FastMCP server — supports mount(), add_middleware(), @tool, etc.
        self.server = FastMCP(name, instructions=instructions or None)

    # ── FastMCP delegation (mount, middleware, tools) ─────────────

    def mount(self, server: FastMCP, namespace: str | None = None, **kwargs: Any) -> None:
        """Mount another FastMCP server — its tools/resources become available."""
        self.server.mount(server, namespace=namespace, **kwargs)

    def add_middleware(self, middleware: Any) -> None:
        """Add FastMCP middleware (e.g. ApprovalMiddleware)."""
        self.server.add_middleware(middleware)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        """Register a tool on the underlying FastMCP server."""
        return self.server.tool(*args, **kwargs)

    def resource(self, *args: Any, **kwargs: Any) -> Any:
        """Register a resource on the underlying FastMCP server."""
        return self.server.resource(*args, **kwargs)

    # ── Content transformer ──────────────────────────────────────

    def set_content_transformer(self, fn: ContentTransformer) -> None:
        """Set a function that transforms (content, meta) before notification emission."""
        self._content_transformer = fn

    def on_shutdown(self, hook: Callable) -> None:
        """Register a cleanup function called on SIGTERM/SIGINT."""
        self._shutdown_hooks.append(hook)

    # ── Notification emission ────────────────────────────────────

    async def notify(self, content: str, meta: dict[str, str] | None = None) -> None:
        """Push a channel event to Claude Code."""
        try:
            self._queue.put_nowait((content, meta or {}))
        except asyncio.QueueFull:
            print(f"[{self.name}] notify queue full, dropping", file=sys.stderr, flush=True)

    async def send_permission_verdict(self, request_id: str, behavior: str) -> None:
        """Send an allow/deny verdict back to Claude Code."""
        if self._write_stream is None:
            return
        with contextlib.suppress(Exception):
            await self._write_stream.send(
                _notif(_PERMISSION_VERDICT_METHOD, {
                    "request_id": request_id,
                    "behavior": behavior,
                })
            )

    async def signal_tools_changed(self) -> None:
        """Signal Claude Code that the tool list has changed."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(("", {"_tools_changed": "true"}))

    # ── Internal: notification drain ─────────────────────────────

    async def _drain_notifications(self, write_stream: Any) -> None:
        """Drain notification queue and write to MCP stream."""
        self._write_stream = write_stream
        while True:
            content, meta = await self._queue.get()

            if meta.get("_tools_changed"):
                with contextlib.suppress(Exception):
                    await write_stream.send(
                        _notif("notifications/tools/list_changed", {})
                    )
                continue

            if self._content_transformer:
                content, meta = self._content_transformer(content, meta)

            try:
                await write_stream.send(
                    _notif(_CHANNEL_METHOD, {"content": content, "meta": meta})
                )
            except Exception as e:
                print(f"[{self.name}] notify failed: {e}", file=sys.stderr, flush=True)

    # ── Internal ─────────────────────────────────────────────────

    def _build_init_options(self) -> Any:
        """Build initialization options with channel capabilities."""
        from mcp.server import NotificationOptions

        experimental: dict[str, dict] = {"claude/channel": {}}
        if self._permission_relay:
            experimental["claude/channel/permission"] = {}
        return self.server._mcp_server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True),
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
        """Run as a stdio channel (Claude Code spawns this as subprocess)."""
        init_opts = self._build_init_options()
        self._install_signals()

        async with self.server._lifespan_manager():
            async with stdio_server() as (rs, ws), anyio.create_task_group() as tg:
                tg.start_soon(self.server._mcp_server.run, rs, ws, init_opts)
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
        """Run as an SSE channel with optional extra Starlette routes."""
        import uvicorn
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route

        init_opts = self._build_init_options()
        self._install_signals()

        sset = SseServerTransport("/messages/")

        async def _sse(req: Any) -> None:
            async with (
                sset.connect_sse(req.scope, req.receive, req._send) as (rs, ws),
                anyio.create_task_group() as tg,
            ):
                tg.start_soon(self.server._mcp_server.run, rs, ws, init_opts)
                tg.start_soon(self._drain_notifications, ws)

        routes = [
            Route("/health", endpoint=lambda r: JSONResponse({"status": "ok"})),
            Route("/sse", endpoint=_sse),
            Mount("/messages/", app=sset.handle_post_message),
        ]
        if extra_routes:
            routes.extend(extra_routes)

        app = Starlette(routes=routes)

        async with self.server._lifespan_manager(), anyio.create_task_group() as tg:
            tg.start_soon(
                uvicorn.Server(
                    uvicorn.Config(app, host=host, port=port, log_level="warning")
                ).serve
            )
            for task in extra_tasks or []:
                tg.start_soon(task)
