"""Base class for approval channels."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ApprovalRequest:
    server_name: str
    tool_name: str
    arguments: dict
    # Populated from tool list metadata
    tool_description: str = ""
    destructive_hint: bool = False
    read_only_hint: bool = False


@dataclass
class ApprovalResult:
    approved: bool
    reason: str = ""


class ApprovalChannel(ABC):
    """Abstract base — implement `request()` to ask the user for approval."""

    @abstractmethod
    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        """Ask for approval. Returns ApprovalResult immediately (may block until answered)."""

    def _format_request(self, req: ApprovalRequest) -> str:
        """Human-readable summary for display."""
        lines = [
            f"🔐 Approval required",
            f"  Server : {req.server_name}",
            f"  Tool   : {req.tool_name}",
        ]
        if req.tool_description:
            lines.append(f"  Desc   : {req.tool_description}")
        if req.arguments:
            pretty = json.dumps(req.arguments, indent=2, ensure_ascii=False)
            # Truncate very long args
            if len(pretty) > 400:
                pretty = pretty[:400] + "\n  ..."
            lines.append(f"  Args   :\n{pretty}")
        if req.destructive_hint:
            lines.append("  ⚠️  Marked DESTRUCTIVE by server")
        return "\n".join(lines)
