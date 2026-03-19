"""WhatsApp approval channel — sends a poll via nanoclaw host-bridge.

Posts to the nanoclaw host-bridge webhook endpoint which fires a WhatsApp poll.
The bridge blocks until the user votes and returns the result.

Host-bridge endpoint (POST):
  http://host.docker.internal:9003/whatsapp_poll
  or
  http://localhost:9003/whatsapp_poll

Body:
  { "question": "...", "options": ["✅ Approve", "❌ Deny"] }

Response:
  { "choice": "✅ Approve" }  or  { "choice": "❌ Deny" }
"""

from __future__ import annotations

import json
import sys

import httpx

from .base import ApprovalChannel, ApprovalRequest, ApprovalResult

_DEFAULT_BRIDGE = "http://localhost:9003"
_APPROVE = "✅ Approve"
_DENY = "❌ Deny"


class WhatsAppChannel(ApprovalChannel):
    def __init__(self, bridge_url: str = _DEFAULT_BRIDGE, timeout: float = 300.0):
        self.bridge_url = bridge_url.rstrip("/")
        self.timeout = timeout

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        question = self._build_question(req)

        payload = {
            "question": question,
            "options": [_APPROVE, _DENY],
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.bridge_url}/whatsapp_poll",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            print(f"[approval-proxy] whatsapp bridge error: {exc}", file=sys.stderr)
            return ApprovalResult(approved=False, reason=f"WhatsApp bridge error: {exc}")

        choice = data.get("choice", "")
        approved = choice == _APPROVE
        return ApprovalResult(approved=approved, reason=f"WhatsApp vote: {choice}")

    def _build_question(self, req: ApprovalRequest) -> str:
        lines = [f"🔐 Approve tool call?"]
        lines.append(f"Server: {req.server_name}")
        lines.append(f"Tool: {req.tool_name}")
        if req.arguments:
            short = json.dumps(req.arguments, ensure_ascii=False)
            if len(short) > 200:
                short = short[:200] + "..."
            lines.append(f"Args: {short}")
        if req.destructive_hint:
            lines.append("⚠️ Marked destructive")
        return "\n".join(lines)
