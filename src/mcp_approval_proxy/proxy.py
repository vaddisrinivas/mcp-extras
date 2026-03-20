"""Build and run the approval proxy for a single upstream MCP server."""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from fastmcp.client import Client
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy

from .audit import AuditLogger
from .config import ProxyConfig, ServerConfig
from .middleware import ApprovalMiddleware


def _build_transport(
    server_cfg: ServerConfig,
) -> StdioTransport | SSETransport | StreamableHttpTransport:
    """Construct the appropriate client transport from server config."""
    if server_cfg.transport_type == "sse":
        return SSETransport(url=server_cfg.url, headers=server_cfg.headers or None)
    if server_cfg.transport_type == "http":
        return StreamableHttpTransport(url=server_cfg.url, headers=server_cfg.headers or None)
    # Default: stdio subprocess
    env = {**os.environ, **server_cfg.env}
    return StdioTransport(command=server_cfg.command, args=server_cfg.args, env=env)


async def build_proxy(
    server_cfg: ServerConfig,
    proxy_cfg: ProxyConfig,
    mode: str,
    always_allow: list[str],
    always_deny: list[str],
    allow_patterns: list[str] | None = None,
    deny_patterns: list[str] | None = None,
):
    """
    Connect to the upstream MCP server, pre-fetch the tool list for annotation
    caching, attach the approval middleware, and return a ready
    :class:`~fastmcp.server.proxy.FastMCPProxy`.

    Parameters
    ----------
    server_cfg:
        Server transport and per-server approval rules.
    proxy_cfg:
        Global proxy settings (dry-run, audit log, timeouts).
    mode:
        Global approval mode (overridden by ``server_cfg.mode`` if set).
    always_allow:
        Global always-allow list (merged with server-level list).
    always_deny:
        Global always-deny list (merged with server-level list).
    allow_patterns:
        Global fnmatch allow patterns.
    deny_patterns:
        Global fnmatch deny patterns.
    """
    transport = _build_transport(server_cfg)
    client = Client(transport)

    # Merge global and server-level rules
    effective_mode = server_cfg.mode or mode
    effective_allow = list(always_allow) + list(server_cfg.always_allow)
    effective_deny = list(always_deny) + list(server_cfg.always_deny)
    effective_allow_patterns = list(allow_patterns or []) + list(server_cfg.allow_patterns)
    effective_deny_patterns = list(deny_patterns or []) + list(server_cfg.deny_patterns)
    effective_timeout = server_cfg.timeout or proxy_cfg.default_timeout
    effective_timeout_action = server_cfg.timeout_action or proxy_cfg.default_timeout_action
    effective_approval_ttl = (
        server_cfg.approval_ttl_seconds
        if server_cfg.approval_ttl_seconds is not None
        else proxy_cfg.approval_ttl_seconds
    )
    effective_explain = (
        server_cfg.explain_decisions
        if server_cfg.explain_decisions is not None
        else proxy_cfg.explain_decisions
    )
    effective_double_confirm = (
        server_cfg.high_risk_requires_double_confirmation
        if server_cfg.high_risk_requires_double_confirmation is not None
        else proxy_cfg.high_risk_requires_double_confirmation
    )
    effective_retry_attempts = (
        server_cfg.approval_retry_attempts
        if server_cfg.approval_retry_attempts is not None
        else proxy_cfg.approval_retry_attempts
    )
    effective_retry_initial_backoff = (
        server_cfg.approval_retry_initial_backoff_seconds
        if server_cfg.approval_retry_initial_backoff_seconds is not None
        else proxy_cfg.approval_retry_initial_backoff_seconds
    )
    effective_retry_multiplier = (
        server_cfg.approval_retry_backoff_multiplier
        if server_cfg.approval_retry_backoff_multiplier is not None
        else proxy_cfg.approval_retry_backoff_multiplier
    )
    effective_retry_max_backoff = (
        server_cfg.approval_retry_max_backoff_seconds
        if server_cfg.approval_retry_max_backoff_seconds is not None
        else proxy_cfg.approval_retry_max_backoff_seconds
    )
    effective_dedupe_fields = (
        server_cfg.approval_dedupe_key_fields
        if server_cfg.approval_dedupe_key_fields
        else proxy_cfg.approval_dedupe_key_fields
    )
    effective_dedupe_arg_keys = (
        server_cfg.approval_dedupe_arg_keys
        if server_cfg.approval_dedupe_arg_keys
        else proxy_cfg.approval_dedupe_arg_keys
    )

    audit = AuditLogger(path=proxy_cfg.audit_log, dry_run=proxy_cfg.dry_run)

    middleware = ApprovalMiddleware(
        mode=effective_mode,
        always_allow=effective_allow,
        always_deny=effective_deny,
        allow_patterns=effective_allow_patterns,
        deny_patterns=effective_deny_patterns,
        custom_annotations=server_cfg.custom_annotations,
        timeout=effective_timeout,
        timeout_action=effective_timeout_action,
        dry_run=proxy_cfg.dry_run,
        audit=audit,
        server_name=server_cfg.name,
        approval_ttl_seconds=effective_approval_ttl,
        explain_decisions=effective_explain,
        high_risk_requires_double_confirmation=effective_double_confirm,
        approval_retry_attempts=effective_retry_attempts,
        approval_retry_initial_backoff_seconds=effective_retry_initial_backoff,
        approval_retry_backoff_multiplier=effective_retry_multiplier,
        approval_retry_max_backoff_seconds=effective_retry_max_backoff,
        approval_dedupe_key_fields=effective_dedupe_fields,
        approval_dedupe_arg_keys=effective_dedupe_arg_keys,
    )

    proxy = create_proxy(client, name=f"approval-proxy/{server_cfg.name}")

    # Augment lifespan: pre-fetch tool list once at startup
    original_lifespan = proxy.lifespan

    @asynccontextmanager
    async def _augmented_lifespan(server):
        async with original_lifespan(server):
            try:
                async with client:
                    tools = await client.list_tools()
                    middleware.tool_registry = {t.name: t for t in tools}
                    print(
                        f"[approval-proxy] {server_cfg.name!r}: "
                        f"{len(tools)} tool(s) indexed  "
                        f"mode={effective_mode}  dry_run={proxy_cfg.dry_run}",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(
                    f"[approval-proxy] Warning: could not pre-fetch tool list: {exc}",
                    file=sys.stderr,
                )
            yield

    proxy.lifespan = _augmented_lifespan
    proxy.add_middleware(middleware)
    return proxy
