"""
approval-proxy — MCP-native approval proxy CLI

Usage:
  approval-proxy --upstream ./mcp.json [options]

Examples:
  # Proxy a single server, use native MCP elicitation for approval
  approval-proxy --upstream ./mcp.json

  # Proxy 'filesystem' server from Claude Desktop config, webhook fallback
  approval-proxy --upstream ~/.claude/claude_desktop_config.json \\
                 --server filesystem \\
                 --channel elicitation \\
                 --fallback webhook \\
                 --webhook-url http://localhost:8080/approve

  # All tools require approval, no fallback (hard-fail if client can't elicit)
  approval-proxy --upstream ./mcp.json --mode all

  # Pass-through proxy (no approval, useful for testing)
  approval-proxy --upstream ./mcp.json --mode none
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_upstream_config
from .proxy import build_channel, build_proxy


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="approval-proxy",
        description="Transparent MCP proxy that gates write/destructive tool calls behind approval.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Upstream config ───────────────────────────────────────────────────────
    p.add_argument(
        "--upstream", "-u",
        required=True,
        metavar="FILE",
        help="Path to MCP server config (Claude Desktop JSON format)",
    )
    p.add_argument(
        "--server", "-s",
        default=None,
        metavar="NAME",
        help="Which server from the config to proxy (default: first server)",
    )

    # ── Approval mode ─────────────────────────────────────────────────────────
    p.add_argument(
        "--mode", "-m",
        default="destructive",
        choices=["destructive", "all", "annotated", "none"],
        help=(
            "Which tool calls require approval. "
            "'destructive' (default): write-pattern names + destructiveHint. "
            "'all': every tool call. "
            "'annotated': only tools with destructiveHint=true. "
            "'none': passthrough, no approval."
        ),
    )
    p.add_argument(
        "--allow",
        default="",
        metavar="tool1,tool2,...",
        help="Comma-separated tool names that NEVER require approval",
    )
    p.add_argument(
        "--deny",
        default="",
        metavar="tool1,tool2,...",
        help="Comma-separated tool names that are ALWAYS blocked (hard deny)",
    )

    # ── Approval channel ──────────────────────────────────────────────────────
    p.add_argument(
        "--channel", "-c",
        default="elicitation",
        choices=["elicitation", "webhook", "whatsapp", "cli"],
        help=(
            "Approval channel. "
            "'elicitation' (default): MCP-native elicitation/create sent to the client. "
            "'webhook': POST to --webhook-url and wait for JSON response. "
            "'whatsapp': Send poll to nanoclaw host-bridge. "
            "'cli': Interactive y/n prompt on stderr."
        ),
    )
    p.add_argument(
        "--fallback",
        default="cli",
        choices=["cli", "webhook", "none"],
        help="Fallback channel when elicitation is unsupported by the client (default: cli)",
    )
    p.add_argument(
        "--webhook-url",
        default=None,
        metavar="URL",
        help="Webhook endpoint URL (required for --channel=webhook or --fallback=webhook)",
    )
    p.add_argument(
        "--whatsapp-bridge",
        default=None,
        metavar="URL",
        help="nanoclaw host-bridge base URL (default: http://localhost:9003)",
    )

    # ── Transport ─────────────────────────────────────────────────────────────
    p.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport to expose (default: stdio)",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (SSE / streamable-http only, default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to bind (SSE / streamable-http only, default: 8765)",
    )

    return p.parse_args()


async def _run(args: argparse.Namespace) -> None:
    # ── Load config ───────────────────────────────────────────────────────────
    servers = load_upstream_config(args.upstream)
    if not servers:
        print(f"[approval-proxy] No servers found in {args.upstream}", file=sys.stderr)
        sys.exit(1)

    if args.server:
        matching = [s for s in servers if s.name == args.server]
        if not matching:
            available = ", ".join(s.name for s in servers)
            print(
                f"[approval-proxy] Server {args.server!r} not found. Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)
        server_cfg = matching[0]
    else:
        server_cfg = servers[0]
        if len(servers) > 1:
            print(
                f"[approval-proxy] Multiple servers in config, using {server_cfg.name!r}. "
                f"Use --server to select another.",
                file=sys.stderr,
            )

    # ── Build channel ─────────────────────────────────────────────────────────
    channel = build_channel(
        channel_type=args.channel,
        webhook_url=args.webhook_url,
        whatsapp_bridge=args.whatsapp_bridge,
        fallback_type=args.fallback if args.fallback != "none" else None,
    )

    # ── Build proxy ───────────────────────────────────────────────────────────
    always_allow = [t.strip() for t in args.allow.split(",") if t.strip()]
    always_deny = [t.strip() for t in args.deny.split(",") if t.strip()]

    proxy = await build_proxy(
        server_cfg=server_cfg,
        channel=channel,
        mode=args.mode,
        always_allow=always_allow,
        always_deny=always_deny,
    )

    print(
        f"[approval-proxy] Starting proxy for {server_cfg.name!r} "
        f"| mode={args.mode} | channel={args.channel} | transport={args.transport}",
        file=sys.stderr,
    )

    # ── Run proxy ─────────────────────────────────────────────────────────────
    if args.transport == "stdio":
        proxy.run(transport="stdio")
    elif args.transport == "sse":
        proxy.run(transport="sse", host=args.host, port=args.port)
    elif args.transport == "streamable-http":
        proxy.run(transport="streamable-http", host=args.host, port=args.port)


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
