"""MCP-native elicitation channel.

When a write tool is intercepted, sends `elicitation/create` back to the MCP client
(Claude Code / Claude Desktop) through the existing MCP session.  The client shows a
native approval dialog — no external polling, no WhatsApp, no CLI prompt needed.

Spec: https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .base import ApprovalChannel, ApprovalRequest, ApprovalResult

# Elicitation schema — a simple boolean approval form
_APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {
            "type": "boolean",
            "title": "Approve this tool call?",
            "description": "Select true to allow, false to deny.",
        },
        "reason": {
            "type": "string",
            "title": "Reason (optional)",
            "description": "Why you approved or denied this action.",
        },
    },
    "required": ["approved"],
}


class ElicitationChannel(ApprovalChannel):
    """
    Approval via MCP elicitation/create.

    The `session` must be a live mcp.server.session.ServerSession that supports
    sending reverse-direction requests to the client.

    Usage inside FastMCP middleware:
        channel = ElicitationChannel()
        channel.attach_session(context.request_context.session)
        result = await channel.request(req)
    """

    def __init__(self, fallback: ApprovalChannel | None = None):
        # Fallback channel used when elicitation is not supported by the client
        self.fallback = fallback
        self._session: Any = None

    def attach_session(self, session: Any) -> None:
        """Attach the current ServerSession (called per-request from middleware)."""
        self._session = session

    def _client_supports_elicitation(self) -> bool:
        """Check if client declared elicitation capability."""
        try:
            caps = self._session.client_params.capabilities
            return caps is not None and caps.elicitation is not None
        except AttributeError:
            return False

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        if self._session is None:
            print("[approval-proxy] No session attached — falling back", file=sys.stderr)
            if self.fallback:
                return await self.fallback.request(req)
            return ApprovalResult(approved=False, reason="No session available")

        if not self._client_supports_elicitation():
            if self.fallback:
                print("[approval-proxy] Client does not support elicitation — using fallback", file=sys.stderr)
                return await self.fallback.request(req)
            # No fallback: deny with a clear message
            return ApprovalResult(
                approved=False,
                reason="MCP client does not support elicitation and no fallback is configured",
            )

        return await self._elicit(req)

    async def _elicit(self, req: ApprovalRequest) -> ApprovalResult:
        """Send elicitation/create to the client and wait for the response."""
        message = self._build_message(req)

        try:
            # mcp SDK: ServerSession.elicit(message, requested_schema) → ElicitResult
            result = await self._session.elicit(
                message=message,
                requested_schema=_APPROVAL_SCHEMA,
            )
        except Exception as exc:
            print(f"[approval-proxy] elicitation/create failed: {exc}", file=sys.stderr)
            if self.fallback:
                return await self.fallback.request(req)
            return ApprovalResult(approved=False, reason=f"Elicitation error: {exc}")

        # result.action: "accept" | "decline" | "cancel"
        action = getattr(result, "action", None)
        content = getattr(result, "content", {}) or {}

        if action == "accept":
            approved = bool(content.get("approved", True))
            reason = content.get("reason", "")
            return ApprovalResult(approved=approved, reason=reason)
        elif action == "decline":
            return ApprovalResult(approved=False, reason="User declined in client")
        else:
            # "cancel" or unknown
            return ApprovalResult(approved=False, reason=f"Elicitation cancelled (action={action})")

    def _build_message(self, req: ApprovalRequest) -> str:
        lines = [
            f"🔐 **Tool call requires approval**",
            f"",
            f"**Server:** `{req.server_name}`",
            f"**Tool:** `{req.tool_name}`",
        ]
        if req.tool_description:
            lines.append(f"**Description:** {req.tool_description}")
        if req.destructive_hint:
            lines.append(f"⚠️ This tool is marked **destructive** by the server.")
        if req.arguments:
            pretty = json.dumps(req.arguments, indent=2, ensure_ascii=False)
            if len(pretty) > 600:
                pretty = pretty[:600] + "\n..."
            lines.append(f"\n**Arguments:**\n```json\n{pretty}\n```")
        return "\n".join(lines)
