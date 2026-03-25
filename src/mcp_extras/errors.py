"""Custom error taxonomy for approval-proxy."""

from __future__ import annotations


class ApprovalProxyError(Exception):
    """Base class for all proxy-specific errors."""


class ApprovalPolicyError(ApprovalProxyError):
    """Raised when policy configuration or mode is invalid."""


class ApprovalTransportError(ApprovalProxyError):
    """Raised when an external approval transport is unreachable or invalid."""


class ApprovalTimeoutError(ApprovalProxyError):
    """Raised when an approval operation exceeds its timeout budget."""
