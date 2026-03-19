"""Build and run the approval proxy for a single upstream server."""

from __future__ import annotations

import os
import sys

from fastmcp import FastMCP
from fastmcp.client import Client

from .config import ServerConfig
from .middleware import ApprovalMiddleware
from .channels.base import ApprovalChannel
from .channels.elicitation import ElicitationChannel
from .channels.cli import CliChannel
from .channels.webhook import WebhookChannel
from .channels.whatsapp import WhatsAppChannel


def build_channel(
    channel_type: str,
    webhook_url: str | None = None,
    whatsapp_bridge: str | None = None,
    fallback_type: str = "cli",
) -> ApprovalChannel:
    """
    Build the primary approval channel, with an optional fallback.

    channel_type: "elicitation" | "webhook" | "whatsapp" | "cli"
    """

    def _fallback() -> ApprovalChannel | None:
        if fallback_type == "cli":
            return CliChannel()
        if fallback_type == "webhook" and webhook_url:
            return WebhookChannel(url=webhook_url)
        return None

    if channel_type == "elicitation":
        return ElicitationChannel(fallback=_fallback())

    if channel_type == "webhook":
        if not webhook_url:
            raise ValueError("--webhook-url is required when --channel=webhook")
        return WebhookChannel(url=webhook_url)

    if channel_type == "whatsapp":
        bridge = whatsapp_bridge or os.environ.get("WHATSAPP_BRIDGE_URL", "http://localhost:9003")
        return WhatsAppChannel(bridge_url=bridge)

    if channel_type == "cli":
        return CliChannel()

    raise ValueError(f"Unknown channel type: {channel_type!r}")


async def build_proxy(
    server_cfg: ServerConfig,
    channel: ApprovalChannel,
    mode: str,
    always_allow: list[str],
    always_deny: list[str],
) -> FastMCP:
    """
    Connect to upstream MCP server, fetch tool list, attach middleware, return proxy.
    """
    # Build environment: inherit current env + override with server-specific vars
    env = {**os.environ, **server_cfg.env}

    # FastMCP client connection string: "command args..." or use MCPServerStdio
    # FastMCP.as_proxy accepts a Client, connection string, or MCPConfig
    client = Client(
        server_params={
            "command": server_cfg.command,
            "args": server_cfg.args,
            "env": env,
        }
    )

    # Create proxy
    proxy = FastMCP.as_proxy(client, name=f"approval-proxy/{server_cfg.name}")

    # Determine effective approval settings (server config overrides global)
    effective_mode = server_cfg.mode or mode
    effective_allow = list(always_allow) + list(server_cfg.always_allow)
    effective_deny = list(always_deny) + list(server_cfg.always_deny)

    # Build middleware
    middleware = ApprovalMiddleware(
        channel=channel,
        mode=effective_mode,
        always_allow=effective_allow,
        always_deny=effective_deny,
        server_name=server_cfg.name,
    )

    # Pre-fetch tool list to populate annotation cache
    # We do this lazily via lifespan to avoid blocking at startup
    @proxy.on_startup
    async def _cache_tools():
        try:
            async with client:
                tools = await client.list_tools()
                middleware.update_tool_cache(tools)
                print(
                    f"[approval-proxy] Connected to {server_cfg.name!r} — "
                    f"{len(tools)} tool(s) indexed",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"[approval-proxy] Warning: could not pre-fetch tools: {exc}", file=sys.stderr)

    proxy.add_middleware(middleware)
    return proxy
