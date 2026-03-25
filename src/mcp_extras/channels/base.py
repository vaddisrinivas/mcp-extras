"""Backward-compatible channel base classes.

.. deprecated::
    Use :class:`mcp_extras.engines.ApprovalEngine` directly.
    :class:`ApprovalChannel` is now an alias for :class:`ApprovalEngine` and
    :class:`ApprovalRequest` / :class:`ApprovalResult` are retained for
    compatibility only.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass

from mcp_extras.engines import ApprovalContext, ApprovalEngine


def _warn_legacy_channel_api(channel_name: str) -> None:
    warnings.warn(
        (
            f"{channel_name} uses legacy channels API and will be removed in a future release; "
            "prefer ApprovalEngine + ApprovalTransport from mcp_extras.engines/transports."
        ),
        DeprecationWarning,
        stacklevel=3,
    )


@dataclass
class ApprovalRequest:
    """Legacy request dataclass — prefer :class:`~mcp_extras.engines.ApprovalContext`."""

    server_name: str
    tool_name: str
    arguments: dict
    tool_description: str = ""
    destructive_hint: bool = False
    read_only_hint: bool = False


@dataclass
class ApprovalResult:
    """Legacy result dataclass — the new API returns plain ``bool | None``."""

    approved: bool
    reason: str = ""


class ApprovalChannel(ApprovalEngine):
    """Abstract base for approval channels.

    .. deprecated::
        Subclass :class:`~mcp_extras.engines.ApprovalEngine` instead
        and implement :meth:`request_approval`.  This class is kept for
        backward compatibility.
    """

    def __init__(self) -> None:
        _warn_legacy_channel_api(self.__class__.__name__)

    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        """Bridge new-style engine call to legacy ``request()`` method."""
        req = ApprovalRequest(
            server_name=ctx.server_name,
            tool_name=ctx.tool_name,
            arguments=ctx.args,
            tool_description=ctx.description,
            destructive_hint=(ctx.annotations.destructiveHint if ctx.annotations else False),
            read_only_hint=(ctx.annotations.readOnlyHint if ctx.annotations else False),
        )
        result = await self.request(req)
        return result.approved

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        """Ask for approval.  Override this in legacy subclasses."""
        raise NotImplementedError

    def _format_request(self, req: ApprovalRequest) -> str:
        """Human-readable summary for display."""
        lines = [
            "🔐 Approval required",
            f"  Server : {req.server_name}",
            f"  Tool   : {req.tool_name}",
        ]
        if req.tool_description:
            lines.append(f"  Desc   : {req.tool_description}")
        if req.arguments:
            pretty = json.dumps(req.arguments, indent=2, ensure_ascii=False)
            if len(pretty) > 400:
                pretty = pretty[:400] + "\n  ..."
            lines.append(f"  Args   :\n{pretty}")
        if req.destructive_hint:
            lines.append("  ⚠️  Marked DESTRUCTIVE by server")
        return "\n".join(lines)
