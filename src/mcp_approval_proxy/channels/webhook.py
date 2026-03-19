"""Webhook approval channel — POST to an HTTP endpoint, wait for response.

Request body (JSON):
  {
    "server": "filesystem",
    "tool": "write_file",
    "arguments": {"path": "/etc/passwd", "content": "..."},
    "description": "Write content to a file",
    "destructiveHint": false
  }

Expected response body (JSON):
  { "approved": true, "reason": "looks fine" }
  or
  { "approved": false, "reason": "nope" }

Timeout defaults to 120 seconds.
"""

from __future__ import annotations

import json
import sys

import httpx

from .base import ApprovalChannel, ApprovalRequest, ApprovalResult


class WebhookChannel(ApprovalChannel):
    def __init__(self, url: str, timeout: float = 120.0, headers: dict | None = None):
        self.url = url
        self.timeout = timeout
        self.headers = headers or {}

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        payload = {
            "server": req.server_name,
            "tool": req.tool_name,
            "arguments": req.arguments,
            "description": req.tool_description,
            "destructiveHint": req.destructive_hint,
            "readOnlyHint": req.read_only_hint,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self.url,
                    json=payload,
                    headers={"Content-Type": "application/json", **self.headers},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            print(f"[approval-proxy] webhook error: {exc}", file=sys.stderr)
            return ApprovalResult(approved=False, reason=f"Webhook error: {exc}")

        approved = bool(data.get("approved", False))
        reason = data.get("reason", "")
        return ApprovalResult(approved=approved, reason=reason)
