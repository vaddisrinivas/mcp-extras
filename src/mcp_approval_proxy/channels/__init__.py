"""
Legacy notification/fallback channels (compatibility layer).

.. deprecated::
    Use :mod:`mcp_approval_proxy.engines` and
    :mod:`mcp_approval_proxy.transports` directly. These channel classes are
    retained only for backward compatibility.

For new code prefer::

    from mcp_approval_proxy.engines import ApprovalEngine, WhatsAppEngine

Note: WhatsAppChannel has been removed. The WhatsApp approval implementation
lives in the host-bridge server (nanoclaw). Use WhatsAppEngine from engines
which calls the host-bridge /whatsapp_poll endpoint.
"""

from .base import ApprovalChannel, ApprovalRequest, ApprovalResult
from .webhook import WebhookChannel

__all__ = [
    "ApprovalChannel",
    "ApprovalRequest",
    "ApprovalResult",
    "WebhookChannel",
]
