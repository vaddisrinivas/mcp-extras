"""
approval-proxy — MCP-native approval proxy

Wraps any MCP server with a transparent approval layer.  When a write or
destructive tool is called the proxy sends an MCP-native ``elicitation/create``
request back to the client (Claude Code, Claude Desktop) — no side channels,
no webhooks, no external polling.

Usage::

    approval-proxy --upstream ./mcp.json [options]

Examples::

    # Gate all write tools with native elicitation (default)
    approval-proxy --upstream ./mcp.json

    # Use a specific server from Claude Desktop config
    approval-proxy --upstream ~/.claude/claude_desktop_config.json --server filesystem

    # Require approval for every tool call
    approval-proxy --upstream ./mcp.json --mode all

    # Pass-through proxy (no approval) — useful for debugging
    approval-proxy --upstream ./mcp.json --mode none

    # Hard-deny specific tools; always allow others
    approval-proxy --upstream ./mcp.json \\
        --deny  "delete_*,destroy_*" \\
        --allow "read_*,list_*,get_*"

    # Dry-run: log what would be gated, never block
    approval-proxy --upstream ./mcp.json --dry-run

    # Write an audit log
    approval-proxy --upstream ./mcp.json --audit-log /tmp/approvals.jsonl

    # 30-second elicitation timeout, auto-deny on timeout
    approval-proxy --upstream ./mcp.json --timeout 30 --timeout-action deny
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .config import ProxyConfig, load_upstream_config
from .proxy import build_proxy

console = Console()


def _parse_patterns(value: str) -> list[str]:
    """Split comma-separated patterns, stripping whitespace."""
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


def _is_pattern(s: str) -> bool:
    """Check if string contains fnmatch wildcards."""
    return any(c in s for c in ("*", "?", "["))


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Transparent MCP proxy that gates write/destructive tool calls behind MCP-native elicitation/create approval.",
)
@click.option(
    "--upstream",
    "-u",
    required=True,
    type=click.Path(exists=True),
    metavar="FILE",
    help="Path to upstream MCP server config JSON (Claude Desktop / claude.json format)",
    show_default=False,
)
@click.option(
    "--server",
    "-s",
    default=None,
    metavar="NAME",
    help="Which server from the config to proxy (default: first server)",
    show_default=False,
)
@click.option(
    "--mode",
    "-m",
    default="destructive",
    type=click.Choice(["destructive", "all", "annotated", "none"]),
    help="Which tool calls require approval",
    show_default=True,
)
@click.option(
    "--allow",
    multiple=True,
    metavar="PATTERN",
    help="Tool names or fnmatch patterns that bypass approval (e.g. 'read_*')",
    show_default=False,
)
@click.option(
    "--deny",
    multiple=True,
    metavar="PATTERN",
    help="Tool names or fnmatch patterns that are permanently blocked (e.g. 'delete_*')",
    show_default=False,
)
@click.option(
    "--timeout",
    type=float,
    default=None,
    metavar="SECONDS",
    help="Seconds to wait for elicitation response (0 to disable)",
    show_default=False,
)
@click.option(
    "--timeout-action",
    type=click.Choice(["approve", "deny"]),
    default=None,
    metavar="ACTION",
    help="Action on timeout",
    show_default=False,
)
@click.option(
    "--approve-ttl",
    type=float,
    default=None,
    metavar="SECONDS",
    help="Cache identical approved tool calls for this many seconds",
    show_default=False,
)
@click.option(
    "--explain",
    is_flag=True,
    default=False,
    help="Return policy/risk details in deny/block responses",
    show_default=True,
)
@click.option(
    "--high-risk-double-confirm",
    is_flag=True,
    default=False,
    help="Require two approvals for high-risk actions",
    show_default=True,
)
@click.option(
    "--approval-retry-attempts",
    type=int,
    default=None,
    metavar="N",
    help="Retry indeterminate approval decisions up to N attempts",
    show_default=False,
)
@click.option(
    "--approval-retry-backoff",
    type=float,
    default=None,
    metavar="SECONDS",
    help="Initial retry backoff for approval retries",
    show_default=False,
)
@click.option(
    "--approval-retry-max-backoff",
    type=float,
    default=None,
    metavar="SECONDS",
    help="Maximum retry backoff for approval retries",
    show_default=False,
)
@click.option(
    "--approval-retry-multiplier",
    type=float,
    default=None,
    metavar="FLOAT",
    help="Retry backoff multiplier for approval retries",
    show_default=False,
)
@click.option(
    "--approval-dedupe-key-fields",
    default=None,
    metavar="FIELDS",
    help="Approval dedupe key fields (server,tool,args,risk)",
    show_default=False,
)
@click.option(
    "--approval-dedupe-arg-keys",
    default=None,
    metavar="KEYS",
    help="Optional subset of argument keys used for dedupe",
    show_default=False,
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log approval decisions but never actually block a call",
    show_default=True,
)
@click.option(
    "--audit-log",
    type=click.Path(),
    default=None,
    metavar="FILE",
    help="Append every decision as a JSON line to FILE",
    show_default=False,
)
@click.option(
    "--transport",
    default="stdio",
    type=click.Choice(["stdio", "sse", "streamable-http"]),
    help="MCP transport to expose",
    show_default=True,
)
@click.option(
    "--host",
    default="127.0.0.1",
    metavar="HOST",
    help="Bind host (sse/streamable-http only)",
    show_default=True,
)
@click.option(
    "--port",
    type=int,
    default=8765,
    metavar="PORT",
    help="Bind port (sse/streamable-http only)",
    show_default=True,
)
@click.version_option(version=__version__)
def main(
    upstream: str,
    server: str | None,
    mode: str,
    allow: tuple[str, ...],
    deny: tuple[str, ...],
    timeout: float | None,
    timeout_action: str | None,
    approve_ttl: float | None,
    explain: bool,
    high_risk_double_confirm: bool,
    approval_retry_attempts: int | None,
    approval_retry_backoff: float | None,
    approval_retry_max_backoff: float | None,
    approval_retry_multiplier: float | None,
    approval_dedupe_key_fields: str | None,
    approval_dedupe_arg_keys: str | None,
    dry_run: bool,
    audit_log: str | None,
    transport: str,
    host: str,
    port: int,
) -> None:
    """Transparent MCP approval proxy."""
    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            _run(
                upstream=upstream,
                server=server,
                mode=mode,
                allow=allow,
                deny=deny,
                timeout=timeout,
                timeout_action=timeout_action,
                approve_ttl=approve_ttl,
                explain=explain,
                high_risk_double_confirm=high_risk_double_confirm,
                approval_retry_attempts=approval_retry_attempts,
                approval_retry_backoff=approval_retry_backoff,
                approval_retry_max_backoff=approval_retry_max_backoff,
                approval_retry_multiplier=approval_retry_multiplier,
                approval_dedupe_key_fields=approval_dedupe_key_fields,
                approval_dedupe_arg_keys=approval_dedupe_arg_keys,
                dry_run=dry_run,
                audit_log=audit_log,
                transport=transport,
                host=host,
                port=port,
            )
        )


async def _run(
    upstream: str,
    server: str | None,
    mode: str,
    allow: tuple[str, ...],
    deny: tuple[str, ...],
    timeout: float | None,
    timeout_action: str | None,
    approve_ttl: float | None,
    explain: bool,
    high_risk_double_confirm: bool,
    approval_retry_attempts: int | None,
    approval_retry_backoff: float | None,
    approval_retry_max_backoff: float | None,
    approval_retry_multiplier: float | None,
    approval_dedupe_key_fields: str | None,
    approval_dedupe_arg_keys: str | None,
    dry_run: bool,
    audit_log: str | None,
    transport: str,
    host: str,
    port: int,
) -> None:
    """Async run implementation."""
    servers, file_proxy_cfg = load_upstream_config(Path(upstream))

    if not servers:
        console.print(f"[red]Error:[/red] No servers found in {upstream}", file=sys.stderr)
        raise SystemExit(1)

    if server:
        matching = [s for s in servers if s.name == server]
        if not matching:
            available = ", ".join(s.name for s in servers)
            console.print(
                f"[red]Error:[/red] Server {server!r} not found. Available: {available}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        server_cfg = matching[0]
    else:
        server_cfg = servers[0]
        if len(servers) > 1:
            console.print(
                f"[yellow]Warning:[/yellow] Multiple servers in config — using {server_cfg.name!r}. "
                "Use --server to select another.",
                file=sys.stderr,
            )

    # CLI flags override the approvalProxy section in the file
    proxy_cfg = ProxyConfig(
        dry_run=dry_run or file_proxy_cfg.dry_run,
        audit_log=audit_log or file_proxy_cfg.audit_log,
        default_timeout=timeout if timeout is not None else file_proxy_cfg.default_timeout,
        default_timeout_action=timeout_action or file_proxy_cfg.default_timeout_action,
        approval_ttl_seconds=(
            approve_ttl if approve_ttl is not None else file_proxy_cfg.approval_ttl_seconds
        ),
        explain_decisions=explain or file_proxy_cfg.explain_decisions,
        high_risk_requires_double_confirmation=(
            high_risk_double_confirm or file_proxy_cfg.high_risk_requires_double_confirmation
        ),
        approval_retry_attempts=(
            approval_retry_attempts
            if approval_retry_attempts is not None
            else file_proxy_cfg.approval_retry_attempts
        ),
        approval_retry_initial_backoff_seconds=(
            approval_retry_backoff
            if approval_retry_backoff is not None
            else file_proxy_cfg.approval_retry_initial_backoff_seconds
        ),
        approval_retry_max_backoff_seconds=(
            approval_retry_max_backoff
            if approval_retry_max_backoff is not None
            else file_proxy_cfg.approval_retry_max_backoff_seconds
        ),
        approval_retry_backoff_multiplier=(
            approval_retry_multiplier
            if approval_retry_multiplier is not None
            else file_proxy_cfg.approval_retry_backoff_multiplier
        ),
        approval_retryable_status_codes=file_proxy_cfg.approval_retryable_status_codes,
        approval_on_timeout=file_proxy_cfg.approval_on_timeout,
        approval_on_transport_error=file_proxy_cfg.approval_on_transport_error,
        approval_allow_insecure_http=file_proxy_cfg.approval_allow_insecure_http,
        approval_allowed_hosts=file_proxy_cfg.approval_allowed_hosts,
        approval_auth_token=file_proxy_cfg.approval_auth_token,
        approval_dedupe_key_fields=(
            _parse_patterns(approval_dedupe_key_fields)
            if approval_dedupe_key_fields
            else file_proxy_cfg.approval_dedupe_key_fields
        ),
        approval_dedupe_arg_keys=(
            _parse_patterns(approval_dedupe_arg_keys)
            if approval_dedupe_arg_keys
            else file_proxy_cfg.approval_dedupe_arg_keys
        ),
    )

    # Flatten allow/deny tuples to strings, then parse patterns
    raw_allow = list(allow)
    raw_deny = list(deny)

    always_allow = [t for t in raw_allow if not _is_pattern(t)]
    allow_patterns = [t for t in raw_allow if _is_pattern(t)]
    always_deny = [t for t in raw_deny if not _is_pattern(t)]
    deny_patterns = [t for t in raw_deny if _is_pattern(t)]

    proxy = await build_proxy(
        server_cfg=server_cfg,
        proxy_cfg=proxy_cfg,
        mode=mode,
        always_allow=always_allow,
        always_deny=always_deny,
        allow_patterns=allow_patterns,
        deny_patterns=deny_patterns,
    )

    flags = []
    if proxy_cfg.dry_run:
        flags.append("DRY-RUN")
    if proxy_cfg.audit_log:
        flags.append(f"audit→{proxy_cfg.audit_log}")
    flag_str = f"  [{', '.join(flags)}]" if flags else ""

    console.print(
        f"[cyan][approval-proxy][/cyan] {server_cfg.name!r}  "
        f"mode={mode}  transport={transport}"
        f"{flag_str}",
        file=sys.stderr,
    )

    if transport == "stdio":
        proxy.run(transport="stdio")
    elif transport == "sse":
        proxy.run(transport="sse", host=host, port=port)
    else:
        proxy.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
