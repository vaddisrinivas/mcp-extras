"""FastMCP middleware that intercepts tool calls and gates them behind approval.

Architecture::

  MCP Client (Claude Code / Claude Desktop)
       │  stdio / SSE / streamable-http
       ▼
  ApprovalProxy  ← this module
       │  on_call_tool() intercepts ALL tool calls
       │  ├── hard-blocked (always_deny / deny_patterns)   → return error
       │  ├── pass-through (read-only / always_allow / mode=none) → forward
       │  └── needs approval  →  ApprovalEngine.request_approval()
       │            │                ├── ElicitationEngine (MCP-native)
       │            │                ├── WhatsAppEngine (nanoclaw bridge)
       │            │                └── ChainedEngine  (try in order)
       │            ├── approved  →  forward to upstream via call_next
       │            └── denied    →  return error CallToolResult
       ▼
  Upstream MCP Server (subprocess or HTTP)
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import re
import sys
import time
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

from .audit import AuditLogger, _Timer


class _DenyResult(ToolResult):
    """ToolResult subclass that carries isError=True for denied/blocked tool calls.

    FastMCP's on_call_tool must return ToolResult (not mt.CallToolResult), and
    to_mcp_result() must produce a CallToolResult with isError=True so the MCP
    client sees a proper error response rather than a success with deny text.
    """

    isError: bool = True  # type: ignore[assignment]  # extra field via model_config

    model_config = {"extra": "allow"}

    def to_mcp_result(self):  # type: ignore[override]
        return mt.CallToolResult(
            content=self.content,
            isError=True,
        )


from .engines import (  # noqa: E402
    ApprovalContext,
    ApprovalEngine,
    ElicitationEngine,  # re-exported for backward compat
)

_VALID_DEDUPE_FIELDS = {"server", "tool", "args", "risk"}

# ── Write-pattern heuristic ───────────────────────────────────────────────────
# Matched after splitting snake_case / camelCase / kebab-case into word tokens.
_WRITE_WORDS = frozenset(
    {
        "write",
        "create",
        "update",
        "delete",
        "remove",
        "move",
        "rename",
        "insert",
        "append",
        "set",
        "put",
        "post",
        "patch",
        "execute",
        "exec",
        "run",
        "trash",
        "kill",
        "drop",
        "truncate",
        "clear",
        "reset",
        "destroy",
        "overwrite",
        "replace",
        "modify",
        "edit",
        "push",
        "deploy",
        "upload",
        "import",
        "send",
        "publish",
        "commit",
        "merge",
        "checkout",
        "tag",
        "release",
        "rollback",
        "restore",
        "wipe",
        "purge",
        "format",
        "mount",
        "enable",
        "disable",
        "start",
        "stop",
        "restart",
        "terminate",
        "shutdown",
        "install",
        "uninstall",
        "add",
        "save",
        "store",
        "submit",
    }
)

# HIGH-risk words used for risk classification independent of the write gate
_HIGH_RISK_WORDS = frozenset(
    {
        "delete",
        "destroy",
        "remove",
        "truncate",
        "drop",
        "wipe",
        "purge",
        "kill",
        "terminate",
        "shutdown",
        "rm",
        "format",
        "uninstall",
    }
)

# Splits snake_case, kebab-case, camelCase, and PascalCase into word tokens
_SPLIT_RE = re.compile(
    r"[_\-\s]"  # snake / kebab / space
    r"|(?<=[a-z])(?=[A-Z])"  # camelCase: e → C
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # ABCDef → ABC / Def
)


def _word_tokens(name: str) -> list[str]:
    """Split a tool name into lowercase word tokens."""
    return [w.lower() for w in _SPLIT_RE.split(name) if w]


def _is_write_heuristic(tool_name: str) -> bool:
    """Return True if the tool name contains a write-like word segment."""
    return any(w in _WRITE_WORDS for w in _word_tokens(tool_name))


def _risk_level(
    tool_name: str,
    annotations: mt.ToolAnnotations | None,
    mode: str,
) -> str:
    """Classify tool risk as 'high', 'medium', 'low', or 'unknown'."""
    tokens = _word_tokens(tool_name)
    destructive = annotations.destructiveHint if annotations else False

    if destructive or any(w in _HIGH_RISK_WORDS for w in tokens):
        return "high"
    if _is_write_heuristic(tool_name):
        return "medium"
    if mode == "all":
        return "low"
    return "unknown"


# ── Decision logic ────────────────────────────────────────────────────────────


def _needs_approval(
    tool_name: str,
    annotations: mt.ToolAnnotations | None,
    mode: str,
    always_allow: frozenset[str],
    always_deny: frozenset[str],
    allow_patterns: list[str],
    deny_patterns: list[str],
    force_approve: frozenset[str],
) -> bool | None:
    """
    Classify a tool call.

    Returns:
        ``None``  — hard block (always_deny / deny_patterns)
        ``False`` — skip approval (allowed / read-only / mode=none)
        ``True``  — request approval
    """
    lname = tool_name.lower()

    # ── Hard deny ─────────────────────────────────────────────────────────────
    if lname in always_deny:
        return None
    if any(fnmatch.fnmatch(lname, p) for p in deny_patterns):
        return None

    # ── Always allow ──────────────────────────────────────────────────────────
    if lname in always_allow:
        return False
    if any(fnmatch.fnmatch(lname, p) for p in allow_patterns):
        return False

    # ── Force approve (from @approval_required(force=True)) ───────────────────
    if lname in force_approve:
        return True

    # ── Mode shortcuts ────────────────────────────────────────────────────────
    if mode == "none":
        return False
    if mode == "all":
        return True

    read_only = annotations.readOnlyHint if annotations else False
    destructive = annotations.destructiveHint if annotations else False

    if mode == "annotated":
        return bool(destructive)

    # ── mode == "destructive" (default) ───────────────────────────────────────
    if read_only:
        return False
    if destructive:
        return True

    return _is_write_heuristic(tool_name)


def _resolve_annotations(
    tool_name: str,
    tool: mt.Tool | None,
    custom_annotations: dict[str, dict],
) -> mt.ToolAnnotations | None:
    """
    Return effective annotations, merging any ``customAnnotations`` overrides.
    """
    base: dict = {}
    if tool and tool.annotations:
        try:
            base = tool.annotations.model_dump(exclude_none=True)
        except AttributeError:
            base = {}

    overrides = custom_annotations.get(tool_name.lower(), {})
    if not overrides:
        return tool.annotations if tool else None

    merged = {**base, **overrides}
    return mt.ToolAnnotations(
        **{k: v for k, v in merged.items() if k in mt.ToolAnnotations.model_fields}
    )


def _deny(message: str) -> _DenyResult:
    return _DenyResult(content=[mt.TextContent(type="text", text=message)])


# ── Main middleware ───────────────────────────────────────────────────────────


class ApprovalMiddleware(Middleware):
    """
    Intercepts ``call_tool`` requests, classifies them by risk, and either
    forwards them immediately, hard-blocks them, or delegates to an
    :class:`~mcp_approval_proxy.engines.ApprovalEngine` for approval.

    Parameters
    ----------
    mode:
        Approval mode — ``"destructive"`` (default), ``"all"``, ``"annotated"``,
        or ``"none"`` (passthrough).
    always_allow:
        Exact tool names that bypass approval.
    always_deny:
        Exact tool names that are permanently blocked (no elicitation).
    allow_patterns:
        fnmatch glob patterns — matching tool names bypass approval.
    deny_patterns:
        fnmatch glob patterns — matching tool names are permanently blocked.
    custom_annotations:
        Override tool annotations per tool name.  Example::

            {"some_tool": {"destructiveHint": True}}
    engine:
        :class:`~mcp_approval_proxy.engines.ApprovalEngine` instance to use
        for approval requests.  Defaults to
        :class:`~mcp_approval_proxy.engines.ElicitationEngine` with the
        supplied ``timeout`` / ``timeout_action``.
        Pass a :class:`~mcp_approval_proxy.engines.WhatsAppEngine` or
        :class:`~mcp_approval_proxy.engines.ChainedEngine` here to route
        approvals through an external channel.
    timeout:
        Seconds to wait (used only when ``engine`` is not supplied, to
        configure the default :class:`ElicitationEngine`).
    timeout_action:
        ``"approve"`` or ``"deny"`` on timeout (same caveat as ``timeout``).
    dry_run:
        If ``True``, log decisions but never actually block a call.
    audit:
        :class:`~mcp_approval_proxy.audit.AuditLogger` instance.
    server_name:
        Display name for this upstream (used in messages and audit log).
    """

    def __init__(
        self,
        mode: str = "destructive",
        always_allow: list[str] | None = None,
        always_deny: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        deny_patterns: list[str] | None = None,
        custom_annotations: dict[str, dict] | None = None,
        engine: ApprovalEngine | None = None,
        timeout: float = 120.0,
        timeout_action: str = "deny",
        dry_run: bool = False,
        audit: AuditLogger | None = None,
        server_name: str = "upstream",
        approval_ttl_seconds: float = 0.0,
        explain_decisions: bool = False,
        high_risk_requires_double_confirmation: bool = False,
        approval_retry_attempts: int = 1,
        approval_retry_initial_backoff_seconds: float = 0.0,
        approval_retry_backoff_multiplier: float = 2.0,
        approval_retry_max_backoff_seconds: float = 5.0,
        approval_dedupe_key_fields: list[str] | None = None,
        approval_dedupe_arg_keys: list[str] | None = None,
    ) -> None:
        self.mode = mode
        self.always_allow = frozenset(t.lower() for t in (always_allow or []))
        self.always_deny = frozenset(t.lower() for t in (always_deny or []))
        self.allow_patterns = [p.lower() for p in (allow_patterns or [])]
        self.deny_patterns = [p.lower() for p in (deny_patterns or [])]
        self.custom_annotations = {k.lower(): v for k, v in (custom_annotations or {}).items()}
        self.engine: ApprovalEngine = engine or ElicitationEngine(
            timeout=timeout, timeout_action=timeout_action
        )
        self.dry_run = dry_run
        self.audit = audit or AuditLogger(None, dry_run=dry_run)
        self.server_name = server_name
        self.approval_ttl_seconds = max(0.0, float(approval_ttl_seconds))
        self.explain_decisions = explain_decisions
        self.high_risk_requires_double_confirmation = high_risk_requires_double_confirmation
        self.approval_retry_attempts = max(1, int(approval_retry_attempts))
        self.approval_retry_initial_backoff_seconds = max(
            0.0, float(approval_retry_initial_backoff_seconds)
        )
        self.approval_retry_backoff_multiplier = max(1.0, float(approval_retry_backoff_multiplier))
        self.approval_retry_max_backoff_seconds = max(
            0.0, float(approval_retry_max_backoff_seconds)
        )
        dedupe_fields = [
            v.lower() for v in (approval_dedupe_key_fields or ["server", "tool", "args"])
        ]
        invalid_dedupe_fields = [v for v in dedupe_fields if v not in _VALID_DEDUPE_FIELDS]
        if invalid_dedupe_fields:
            raise ValueError(f"invalid approval_dedupe_key_fields: {invalid_dedupe_fields}")
        self.approval_dedupe_key_fields = dedupe_fields
        self.approval_dedupe_arg_keys = list(approval_dedupe_arg_keys or [])

        # Populated after connecting to upstream: {tool_name: mt.Tool}
        self.tool_registry: dict[str, mt.Tool] = {}

        # Extra state set by register_from_server() / _apply_decorator_meta()
        self._force_approve: set[str] = set()
        self._risk_overrides: dict[str, str] = {}
        self._reason_overrides: dict[str, str] = {}
        self._approval_cache: dict[str, float] = {}
        self._approval_locks: dict[str, asyncio.Lock] = {}

    def _approval_key(self, tool_name: str, tool_args: dict, risk: str) -> str:
        """Stable key for deduplicating identical approval prompts."""
        key_payload: dict[str, Any] = {}
        if "server" in self.approval_dedupe_key_fields:
            key_payload["server"] = self.server_name
        if "tool" in self.approval_dedupe_key_fields:
            key_payload["tool"] = tool_name.lower()
        if "risk" in self.approval_dedupe_key_fields:
            key_payload["risk"] = risk
        if "args" in self.approval_dedupe_key_fields:
            if self.approval_dedupe_arg_keys:
                key_payload["args"] = {
                    key: tool_args.get(key)
                    for key in self.approval_dedupe_arg_keys
                    if key in tool_args
                }
            else:
                key_payload["args"] = tool_args
        payload = json.dumps(key_payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _is_approval_cached(self, key: str) -> bool:
        if self.approval_ttl_seconds <= 0:
            return False
        now = time.monotonic()
        exp = self._approval_cache.get(key)
        if exp is None:
            return False
        if exp <= now:
            self._approval_cache.pop(key, None)
            return False
        return True

    def _cache_approval(self, key: str) -> None:
        if self.approval_ttl_seconds <= 0:
            return
        self._approval_cache[key] = time.monotonic() + self.approval_ttl_seconds
        # Opportunistic cleanup: evict expired entries when cache grows large.
        if len(self._approval_cache) > 500:
            now = time.monotonic()
            expired = [k for k, exp in self._approval_cache.items() if exp <= now]
            for k in expired:
                del self._approval_cache[k]

    def _deny_message(self, tool_name: str, reason: str, risk: str) -> str:
        if not self.explain_decisions:
            return f"❌ Tool call `{tool_name}` denied."
        return f"❌ Tool call `{tool_name}` denied. {reason} (risk={risk}, mode={self.mode})"

    # ── Server introspection ──────────────────────────────────────────────────

    async def register_from_server(self, server: Any) -> None:
        """Populate ``tool_registry`` from a live FastMCP server and read
        :func:`~mcp_approval_proxy.decorators.approval_required` decorator
        metadata from each tool function.

        Call this instead of (or in addition to) manually assigning
        ``tool_registry`` when wrapping an in-process server.

        Parameters
        ----------
        server:
            A :class:`fastmcp.FastMCP` instance.
        """
        from fastmcp.client import Client

        from .decorators import APPROVAL_META_ATTR

        async with Client(server) as client:
            tools = await client.list_tools()
        self.tool_registry = {t.name: t for t in tools}

        # Read @approval_required metadata from function objects.
        # FastMCP 3.x: server.list_tools() returns FunctionTool objects that
        # expose the original callable via .fn, which may carry __approval_meta__.
        try:
            server_tools = await server.list_tools()
            for server_tool in server_tools:
                fn = getattr(server_tool, "fn", None)
                if fn is None:
                    continue
                meta = getattr(fn, APPROVAL_META_ATTR, None)
                if meta is None:
                    continue
                self._apply_decorator_meta(server_tool.name, meta)
        except Exception as exc:
            print(
                f"[approval-proxy] warning: failed to read decorator metadata: {exc}",
                file=sys.stderr,
            )

    def _apply_decorator_meta(self, tool_name: str, meta: dict) -> None:
        """Merge ``@approval_required`` metadata into this middleware's policy."""
        lname = tool_name.lower()

        if meta.get("always_allow"):
            self.always_allow = self.always_allow | {lname}
        elif meta.get("always_deny"):
            self.always_deny = self.always_deny | {lname}
        elif meta.get("force"):
            self._force_approve.add(lname)

        if meta.get("annotations"):
            merged = {**self.custom_annotations.get(lname, {}), **meta["annotations"]}
            self.custom_annotations[lname] = merged

        if meta.get("risk"):
            self._risk_overrides[lname] = meta["risk"]

        if meta.get("reason"):
            self._reason_overrides[lname] = meta["reason"]

    # ── Middleware hook ───────────────────────────────────────────────────────

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: Any,
    ) -> ToolResult:
        tool_name: str = context.message.name
        tool_args: dict = context.message.arguments or {}
        timer = _Timer()
        lname = tool_name.lower()

        tool = self.tool_registry.get(tool_name)
        annotations = _resolve_annotations(tool_name, tool, self.custom_annotations)
        description = (tool.description or "") if tool else ""

        # Apply risk / reason overrides from @approval_required decorator
        risk = self._risk_overrides.get(lname) or _risk_level(tool_name, annotations, self.mode)
        reason = self._reason_overrides.get(lname, "")

        decision = _needs_approval(
            tool_name,
            annotations,
            self.mode,
            self.always_allow,
            self.always_deny,
            self.allow_patterns,
            self.deny_patterns,
            frozenset(self._force_approve),
        )

        # ── Hard block ────────────────────────────────────────────────────────
        if decision is None:
            if self.dry_run:
                print(
                    f"[approval-proxy] [DRY-RUN] would block `{tool_name}` — passing through",
                    file=sys.stderr,
                )
                self.audit.log(
                    server=self.server_name,
                    tool=tool_name,
                    args=tool_args,
                    decision="dry_run",
                    risk=risk,
                    reason="hard-blocked policy (dry-run pass)",
                    mode=self.mode,
                    duration_ms=timer.elapsed_ms(),
                )
                return await call_next(context)

            self.audit.log(
                server=self.server_name,
                tool=tool_name,
                args=tool_args,
                decision="blocked",
                risk=risk,
                reason="policy: always_deny / deny_patterns",
                mode=self.mode,
                duration_ms=timer.elapsed_ms(),
            )
            reason_text = "blocked by policy (always_deny/deny_patterns)"
            if not self.explain_decisions:
                return _deny(f"⛔ Tool `{tool_name}` is blocked by policy.")
            return _deny(
                f"⛔ Tool `{tool_name}` is blocked. {reason_text} (mode={self.mode}, risk={risk})"
            )

        # ── Pass-through (no approval needed) ─────────────────────────────────
        if not decision:
            self.audit.log(
                server=self.server_name,
                tool=tool_name,
                args=tool_args,
                decision="passed",
                risk=risk,
                reason="allowed without approval",
                mode=self.mode,
                duration_ms=timer.elapsed_ms(),
            )
            return await call_next(context)

        # ── Dry-run: would need approval but pass through ─────────────────────
        if self.dry_run:
            print(
                f"[approval-proxy] [DRY-RUN] would request approval for `{tool_name}` "
                f"({risk} risk) — passing through",
                file=sys.stderr,
            )
            self.audit.log(
                server=self.server_name,
                tool=tool_name,
                args=tool_args,
                decision="dry_run",
                risk=risk,
                reason="would require approval (dry-run pass)",
                mode=self.mode,
                duration_ms=timer.elapsed_ms(),
            )
            return await call_next(context)

        approval_key = self._approval_key(tool_name, tool_args, risk)
        if self._is_approval_cached(approval_key):
            self.audit.log(
                server=self.server_name,
                tool=tool_name,
                args=tool_args,
                decision="passed",
                risk=risk,
                reason="approval cache hit",
                mode=self.mode,
                duration_ms=timer.elapsed_ms(),
            )
            return await call_next(context)

        lock = self._approval_locks.setdefault(approval_key, asyncio.Lock())

        try:
            async with lock:
                if self._is_approval_cached(approval_key):
                    self.audit.log(
                        server=self.server_name,
                        tool=tool_name,
                        args=tool_args,
                        decision="passed",
                        risk=risk,
                        reason="approval cache hit",
                        mode=self.mode,
                        duration_ms=timer.elapsed_ms(),
                    )
                    return await call_next(context)

                # ── Delegate to approval engine ───────────────────────────────
                approval_ctx = ApprovalContext(
                    server_name=self.server_name,
                    tool_name=tool_name,
                    args=tool_args,
                    risk=risk,
                    description=description,
                    reason=reason,
                    annotations=annotations,
                    fastmcp_context=context.fastmcp_context,
                )

                approved: bool | None = None
                delay = self.approval_retry_initial_backoff_seconds
                for attempt in range(1, self.approval_retry_attempts + 1):
                    try:
                        approved = await self.engine.request_approval(approval_ctx)
                    except Exception as exc:
                        print(
                            f"[approval-proxy] engine error for `{tool_name}` (attempt {attempt}/{self.approval_retry_attempts}): {exc}",
                            file=sys.stderr,
                        )
                        approved = None
                        if attempt >= self.approval_retry_attempts:
                            self.audit.log(
                                server=self.server_name,
                                tool=tool_name,
                                args=tool_args,
                                decision="error",
                                risk=risk,
                                reason=str(exc),
                                mode=self.mode,
                                duration_ms=timer.elapsed_ms(),
                            )
                            return _deny(f"❌ Approval engine error for `{tool_name}`: {exc}")

                    if approved is not None:
                        break

                    if attempt < self.approval_retry_attempts and delay > 0:
                        await asyncio.sleep(min(delay, self.approval_retry_max_backoff_seconds))
                        delay = min(
                            max(delay, 0.0) * self.approval_retry_backoff_multiplier,
                            self.approval_retry_max_backoff_seconds,
                        )

                # None from engine after retry budget = indeterminate -> deny
                if approved is None:
                    approved = False

                # Optional two-step high-risk confirmation.
                if approved and risk == "high" and self.high_risk_requires_double_confirmation:
                    try:
                        second_ctx = ApprovalContext(
                            server_name=self.server_name,
                            tool_name=tool_name,
                            args=tool_args,
                            risk=risk,
                            description=description,
                            reason=(reason + " | Final confirmation required").strip(" |"),
                            annotations=annotations,
                            fastmcp_context=context.fastmcp_context,
                        )
                        second = await self.engine.request_approval(second_ctx)
                        approved = bool(second)
                    except Exception as exc:
                        print(
                            f"[approval-proxy] second confirmation failed for `{tool_name}`: {exc}",
                            file=sys.stderr,
                        )
                        approved = False

                if approved:
                    self._cache_approval(approval_key)
                    self.audit.log(
                        server=self.server_name,
                        tool=tool_name,
                        args=tool_args,
                        decision="approved",
                        risk=risk,
                        reason=f"approved via {type(self.engine).__name__}",
                        mode=self.mode,
                        duration_ms=timer.elapsed_ms(),
                    )
                    return await call_next(context)

                self.audit.log(
                    server=self.server_name,
                    tool=tool_name,
                    args=tool_args,
                    decision="denied",
                    risk=risk,
                    reason=f"denied via {type(self.engine).__name__}",
                    mode=self.mode,
                    duration_ms=timer.elapsed_ms(),
                )
                return _deny(
                    self._deny_message(
                        tool_name,
                        f"denied via {type(self.engine).__name__}",
                        risk,
                    )
                )
        finally:
            # Locks are lightweight; skip cleanup to avoid TOCTOU race.
            # The dict grows at most to the number of unique approval keys seen.
            pass
