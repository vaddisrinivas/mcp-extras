"""
MCP Approval Proxy — gates write/destructive MCP tool calls behind approval.

Quick start (library usage)::

    from fastmcp.client import Client
    from fastmcp.client.transports import StdioTransport
    from fastmcp.server import create_proxy
    from mcp_approval_proxy import ApprovalMiddleware

    transport = StdioTransport("npx", ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
    proxy = create_proxy(Client(transport))
    proxy.add_middleware(ApprovalMiddleware(mode="destructive", server_name="filesystem"))
    proxy.run(transport="stdio")

Using an external approval engine (e.g. WhatsApp via nanoclaw)::

    from mcp_approval_proxy import ApprovalMiddleware
    from mcp_approval_proxy.engines import WhatsAppEngine

    mw = ApprovalMiddleware(
        mode="destructive",
        server_name="my-server",
        engine=WhatsAppEngine(bridge_url="http://localhost:9003"),
    )

Elicitation + WhatsApp fallback::

    from mcp_approval_proxy.engines import ChainedEngine, ElicitationEngine, WhatsAppEngine

    mw = ApprovalMiddleware(
        engine=ChainedEngine([
            ElicitationEngine(timeout=30),
            WhatsAppEngine(bridge_url="http://localhost:9003"),
        ]),
    )

Inline tool decoration (in-process servers only)::

    from mcp_approval_proxy.decorators import approval_required
    from fastmcp import FastMCP

    server = FastMCP("my-server")

    @server.tool()
    @approval_required(force=True, risk="high", reason="Removes data permanently")
    def delete_record(id: str) -> str: ...

    mw = ApprovalMiddleware(mode="destructive", server_name="my-server")
    await mw.register_from_server(server)
"""

__version__ = "0.2.0"

from .audit import AuditLogger
from .config import ProxyConfig, ServerConfig, load_upstream_config
from .decorators import approval_required
from .engines import (
    ApprovalContext,
    ApprovalEngine,
    ChainedEngine,
    ElicitationEngine,
    WAHAEngine,
    WebhookEngine,
    WhatsAppEngine,
)
from .errors import (
    ApprovalPolicyError,
    ApprovalProxyError,
    ApprovalTimeoutError,
    ApprovalTransportError,
)
from .middleware import ApprovalMiddleware
from .proxy import build_proxy
from .transports import (
    ApprovalTransport,
    ChainedTransport,
    NanoclawApprovalsTransport,
    TransportPolicy,
    WhatsAppPollTransport,
    build_whatsapp_transport,
)

__all__ = [
    "__version__",
    # Middleware
    "ApprovalMiddleware",
    # Engines
    "ApprovalEngine",
    "ApprovalContext",
    "ElicitationEngine",
    "WAHAEngine",
    "WebhookEngine",
    "WhatsAppEngine",
    "ChainedEngine",
    # Errors
    "ApprovalProxyError",
    "ApprovalPolicyError",
    "ApprovalTransportError",
    "ApprovalTimeoutError",
    # Decorator
    "approval_required",
    # Infra
    "AuditLogger",
    "ProxyConfig",
    "ServerConfig",
    "build_proxy",
    "load_upstream_config",
    # Transport API
    "ApprovalTransport",
    "TransportPolicy",
    "WhatsAppPollTransport",
    "NanoclawApprovalsTransport",
    "ChainedTransport",
    "build_whatsapp_transport",
]
