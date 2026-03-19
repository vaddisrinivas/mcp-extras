"""FastMCP middleware that intercepts tool calls and gates them behind approval.

Architecture:
  MCP Client (Claude Code)
       │  stdio / SSE
       ▼
  ApprovalProxy (this module — FastMCP server)
       │  on_call_tool() intercepts ALL tool calls
       │  ├── read-only / always-allow  →  forward immediately
       │  └── write / destructive       →  send elicitation/create back to client
       │            ├── approved  →  forward to upstream
       │            └── denied    →  return error CallToolResult
       ▼
  Upstream MCP Server (subprocess or remote)
"""

from __future__ import annotations

import re
import sys
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .channels.base import ApprovalChannel, ApprovalRequest
from .channels.elicitation import ElicitationChannel

# ── Tool name patterns that suggest write/destructive operations ─────────────
_WRITE_PATTERNS = re.compile(
    r"(write|create|update|delete|remove|move|rename|insert|append|"
    r"set|put|post|patch|exec|run|trash|kill|drop|truncate|clear|"
    r"reset|destroy|overwrite|replace|modify|edit|push|deploy|"
    r"upload|import|import_|send|publish|commit|merge|rebase|"
    r"checkout|branch|tag|release|rollback|restore|wipe|purge|"
    r"format|partition|mount|umount|enable|disable|start|stop|"
    r"restart|kill|terminate|shutdown|reboot|install|uninstall)",
    re.IGNORECASE,
)


def _needs_approval(
    tool_name: str,
    annotations: dict,
    mode: str,
    always_allow: set[str],
    always_deny: set[str],
) -> bool | None:
    """
    Returns:
        True  → needs approval
        False → skip approval (always allow)
        None  → hard deny (blocked entirely, no approval)
    """
    lname = tool_name.lower()

    if lname in always_deny:
        return None  # hard block

    if lname in always_allow:
        return False  # skip approval

    if mode == "none":
        return False  # proxy passthrough — no approval for anything

    if mode == "all":
        return True  # approve everything

    read_only = annotations.get("readOnlyHint", False)
    destructive = annotations.get("destructiveHint", False)

    if mode == "annotated":
        # Only gate tools explicitly marked destructive
        return destructive or None  # None would be wrong here; use False for non-destructive
        # Correction: return True only if destructive, False otherwise
        return destructive

    # mode == "destructive" (default)
    if read_only:
        return False  # explicitly safe, skip approval

    if destructive:
        return True  # explicitly destructive, always approve

    # Heuristic: check tool name for write-like patterns
    return bool(_WRITE_PATTERNS.search(tool_name))


class ApprovalMiddleware(Middleware):
    """
    Intercepts `call_tool` requests.  For each intercepted call:
    1. Look up the tool's annotations (readOnlyHint, destructiveHint).
    2. Decide whether approval is needed based on `mode`.
    3. If needed: fire the approval channel (default: MCP elicitation/create).
    4. If approved (or not needed): forward to upstream via call_next.
    5. If denied: return a denial CallToolResult immediately.
    """

    def __init__(
        self,
        channel: ApprovalChannel,
        mode: str = "destructive",
        always_allow: list[str] | None = None,
        always_deny: list[str] | None = None,
        server_name: str = "upstream",
    ):
        self.channel = channel
        self.mode = mode
        self.always_allow: set[str] = {t.lower() for t in (always_allow or [])}
        self.always_deny: set[str] = {t.lower() for t in (always_deny or [])}
        self.server_name = server_name
        # Tool metadata cache: {tool_name: {"description": ..., "annotations": {...}}}
        self._tool_cache: dict[str, dict[str, Any]] = {}

    def update_tool_cache(self, tools: list[Any]) -> None:
        """Call this after connecting to the upstream to cache tool metadata."""
        for t in tools:
            name = getattr(t, "name", None)
            if name:
                self._tool_cache[name] = {
                    "description": getattr(t, "description", "") or "",
                    "annotations": _annotations_dict(t),
                }

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        # Extract tool name + args from context
        tool_name, tool_args = _extract_call(context)
        if tool_name is None:
            return await call_next(context)

        meta = self._tool_cache.get(tool_name, {})
        annotations = meta.get("annotations", {})
        description = meta.get("description", "")

        decision = _needs_approval(
            tool_name,
            annotations,
            self.mode,
            self.always_allow,
            self.always_deny,
        )

        if decision is None:
            # Hard deny — blocked by always_deny list
            return _deny_result(f"⛔ Tool `{tool_name}` is blocked by policy.")

        if not decision:
            # Skip approval — read-only or always-allow
            return await call_next(context)

        # ── Request approval ──────────────────────────────────────────────────
        # Attach the current MCP session to the elicitation channel (if it is one)
        if isinstance(self.channel, ElicitationChannel):
            session = _extract_session(context)
            if session is not None:
                self.channel.attach_session(session)

        req = ApprovalRequest(
            server_name=self.server_name,
            tool_name=tool_name,
            arguments=tool_args,
            tool_description=description,
            destructive_hint=annotations.get("destructiveHint", False),
            read_only_hint=annotations.get("readOnlyHint", False),
        )

        result = await self.channel.request(req)

        if result.approved:
            return await call_next(context)
        else:
            reason = result.reason or "Denied"
            return _deny_result(f"❌ Tool call `{tool_name}` denied. {reason}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_call(context: MiddlewareContext) -> tuple[str | None, dict]:
    """Extract tool name and arguments from middleware context."""
    msg = context.message
    # FastMCP v2 layout: message.params.name / message.params.arguments
    try:
        return msg.params.name, (msg.params.arguments or {})
    except AttributeError:
        pass
    # Fallback for different FastMCP versions
    try:
        return msg.name, (msg.arguments or {})
    except AttributeError:
        pass
    return None, {}


def _extract_session(context: MiddlewareContext) -> Any | None:
    """Try to extract the live ServerSession from middleware context."""
    # Path 1: context.request_context.session (mcp library's RequestContext)
    try:
        return context.request_context.session
    except AttributeError:
        pass
    # Path 2: context.server_context.session
    try:
        return context.server_context.session
    except AttributeError:
        pass
    # Path 3: context.session
    try:
        return context.session
    except AttributeError:
        pass
    print("[approval-proxy] Could not locate session in middleware context", file=sys.stderr)
    return None


def _annotations_dict(tool: Any) -> dict:
    """Extract annotations dict from a tool object."""
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return {}
    if isinstance(ann, dict):
        return ann
    # Pydantic model
    try:
        return ann.model_dump(exclude_none=True)
    except AttributeError:
        pass
    # Dataclass / NamedTuple
    try:
        return {k: getattr(ann, k) for k in vars(ann)}
    except Exception:
        return {}


def _deny_result(message: str):
    """Return a CallToolResult that signals denial."""
    # Import here to avoid circular imports at module load time
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        isError=True,
    )
