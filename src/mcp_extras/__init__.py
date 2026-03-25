"""
MCP Extras — approval proxy middleware + Claude Code channel SDK for Python.

Approval proxy (gates write/destructive MCP tool calls behind approval)::

    from mcp_extras import ApprovalMiddleware
    proxy.add_middleware(ApprovalMiddleware(mode="destructive", server_name="my-server"))

Channel SDK (push events into Claude Code sessions)::

    from mcp_extras.channel import ChannelServer

    ch = ChannelServer("my-channel", instructions="Events arrive as <channel>...")
    await ch.notify("build failed on main")
    await ch.run_stdio()

See README for full documentation.
"""

__version__ = "0.3.0"

from .audit import AuditLogger
from .channel import ChannelServer
from .config import ProxyConfig, ServerConfig, load_upstream_config
from .decorators import approval_required
from .engines import (
    ApprovalContext,
    ApprovalEngine,
    CallbackEngine,
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
    # Channel SDK
    "ChannelServer",
    # Middleware
    "ApprovalMiddleware",
    # Engines
    "ApprovalEngine",
    "ApprovalContext",
    "ElicitationEngine",
    "WAHAEngine",
    "WebhookEngine",
    "WhatsAppEngine",
    "CallbackEngine",
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
