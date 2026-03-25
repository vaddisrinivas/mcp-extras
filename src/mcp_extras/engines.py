"""Approval engine abstractions.

The :class:`ApprovalEngine` ABC decouples *how* approval is requested from the
middleware's classification and policy logic.

Built-in engines
----------------
:class:`ElicitationEngine`
    MCP-native ``elicitation/create`` — default when the connected client
    supports it (Claude Code, Claude Desktop).
:class:`WAHAEngine`
    Sends a WhatsApp **text** message via a WAHA (WhatsApp HTTP API) service
    and polls for a text reply.  Works with the NOWEB engine — does **not**
    use WhatsApp polls (which NOWEB cannot decrypt).
:class:`WhatsAppEngine`
    Legacy engine that forwards approval requests to a nanoclaw host-bridge
    ``/whatsapp_poll`` or ``/approvals`` HTTP endpoint.
:class:`ChainedEngine`
    Tries engines in order; uses the first that returns a definitive
    ``True``/``False`` (rather than ``None``).

Subclassing
-----------
Implement ``request_approval`` and return:

* ``True``  — approved (tool call proceeds)
* ``False`` — denied (tool call is blocked)
* ``None``  — indeterminate (engine could not decide; ``ChainedEngine``
               will try the next engine; standalone use treats as deny)

Example — elicitation first, WAHA text-message fallback::

    from mcp_extras.engines import ChainedEngine, ElicitationEngine, WAHAEngine

    mw = ApprovalMiddleware(
        engine=ChainedEngine([
            ElicitationEngine(timeout=30, fallthrough_on_timeout=True),
            WAHAEngine(
                waha_url="http://waha:3000",
                chat_id="18128035718@c.us",
            ),
        ]),
    )

Example — legacy WhatsApp bridge::

    from mcp_extras.engines import WhatsAppEngine
    from mcp_extras import ApprovalMiddleware

    mw = ApprovalMiddleware(
        engine=WhatsAppEngine(bridge_url="http://localhost:9003"),
    )
"""

from __future__ import annotations

import asyncio
import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .transports import ApprovalTransport, TransportPolicy, build_whatsapp_transport

if TYPE_CHECKING:
    import mcp.types as mt

# ── Approval context ──────────────────────────────────────────────────────────


@dataclass
class ApprovalContext:
    """All information available when an approval decision is needed.

    Passed to :meth:`ApprovalEngine.request_approval` on every gated call.
    Engines may use whichever fields are relevant to them.
    """

    server_name: str
    tool_name: str
    args: dict
    risk: str = "unknown"
    description: str = ""
    reason: str = ""
    annotations: mt.ToolAnnotations | None = None
    #: MCP session context (FastMCP ``Context``); only useful for
    #: :class:`ElicitationEngine`.  Other engines may ignore this.
    fastmcp_context: Any | None = None


# ── Abstract base ─────────────────────────────────────────────────────────────


class ApprovalEngine(ABC):
    """Abstract base class for approval backends.

    Subclass this to add new approval channels (WhatsApp, Slack, PagerDuty,
    email, etc.).  Register an instance with :class:`ApprovalMiddleware` via
    the ``engine`` constructor parameter.
    """

    @abstractmethod
    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        """Ask for approval.

        Returns
        -------
        ``True``
            Approved — forward the tool call to upstream.
        ``False``
            Denied — return an error result to the client.
        ``None``
            Indeterminate — this engine could not decide (e.g. connection
            error, unsupported client).  A :class:`ChainedEngine` will try
            the next engine; a standalone middleware treats this as deny.
        """


# ── MCP-native elicitation ────────────────────────────────────────────────────


class ElicitationEngine(ApprovalEngine):
    """MCP-native ``elicitation/create`` approval (default).

    Sends an approval dialog to the connected MCP client (Claude Code,
    Claude Desktop) via the standard ``elicitation/create`` protocol message.

    Returns ``None`` — and prints a warning — if the client does not support
    elicitation, allowing a :class:`ChainedEngine` to fall back to another
    backend (e.g. :class:`WhatsAppEngine`).

    Parameters
    ----------
    timeout:
        Seconds to wait for the user response (default: 120).
    timeout_action:
        ``"approve"`` or ``"deny"`` (default) when the timeout expires.
        Ignored when ``fallthrough_on_timeout=True``.
    fallthrough_on_timeout:
        When ``True``, a timeout returns ``None`` instead of acting on
        ``timeout_action``, allowing a :class:`ChainedEngine` to try the
        next engine (e.g. :class:`WhatsAppEngine`).  Defaults to ``False``.
    """

    def __init__(
        self,
        timeout: float = 120.0,
        timeout_action: str = "deny",
        fallthrough_on_timeout: bool = False,
    ) -> None:
        self.timeout = timeout
        self.timeout_action = timeout_action
        self.fallthrough_on_timeout = fallthrough_on_timeout

    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation

        fastmcp_ctx = ctx.fastmcp_context
        if fastmcp_ctx is None:
            return None  # No session context; signal fall-through

        if not await _client_supports_elicitation(fastmcp_ctx):
            print(
                f"[approval-proxy] Client does not support elicitation "
                f"for `{ctx.tool_name}` — trying next engine",
                file=sys.stderr,
            )
            return None  # Signal fall-through to next engine

        message = _build_elicitation_message(
            server_name=ctx.server_name,
            tool_name=ctx.tool_name,
            tool_args=ctx.args,
            description=ctx.description,
            annotations=ctx.annotations,
            risk=ctx.risk,
            reason=ctx.reason,
        )

        try:
            result = await asyncio.wait_for(
                fastmcp_ctx.elicit(message, response_type=bool),
                timeout=self.timeout,
            )
        except TimeoutError:
            print(
                f"[approval-proxy] Elicitation timeout for `{ctx.tool_name}` "
                f"(>{self.timeout}s) — "
                + (
                    "falling through to next engine"
                    if self.fallthrough_on_timeout
                    else f"action={self.timeout_action}"
                ),
                file=sys.stderr,
            )
            if self.fallthrough_on_timeout:
                return None  # Let ChainedEngine try next engine (e.g. WhatsApp)
            return self.timeout_action == "approve"
        except Exception as exc:
            print(
                f"[approval-proxy] elicitation error for `{ctx.tool_name}`: {exc}",
                file=sys.stderr,
            )
            return None  # Signal fall-through

        if isinstance(result, AcceptedElicitation):
            return bool(result.data)
        if isinstance(result, DeclinedElicitation):
            return False
        return False  # CancelledElicitation or unknown


# ── WhatsApp via nanoclaw host-bridge ─────────────────────────────────────────


class WhatsAppEngine(ApprovalEngine):
    """Sends approval requests as WhatsApp polls via the nanoclaw host-bridge.

    Supports two API styles:

    1) ``/whatsapp_poll`` (legacy bridge style):
       POST a question/options payload and receive ``{"choice": "..."} ``
       when the user votes.

    2) ``/approvals`` + ``/approvals/{id}`` (nanoclaw approvals API):
       POST creates the approval poll; GET polls for ``approved``/``denied``.

    Legacy endpoint (POST ``/whatsapp_poll``)::

        {
            "question": "...",
            "options": ["✅ Approve", "❌ Deny"]
        }

    Response::

        { "choice": "✅ Approve" }

    Nanoclaw approvals endpoints::

        POST /approvals       { "message": "...", "timeoutMs": 120000 } -> { "id": "..." }
        GET  /approvals/{id}  -> { "status": "pending"|"approved"|"denied" }

    Parameters
    ----------
    bridge_url:
        Base URL of the nanoclaw host-bridge.
        Defaults to ``http://localhost:9003``; use
        ``http://host.docker.internal:9003`` from inside Docker.
    timeout:
        Seconds to wait for the vote (default: 300 s = 5 minutes).
    api_mode:
        ``"auto"`` (default) tries ``/whatsapp_poll`` first, then nanoclaw
        ``/approvals`` fallback. ``"whatsapp_poll"`` forces legacy mode.
        ``"approvals"`` forces nanoclaw approvals mode.
    poll_interval:
        Poll interval in seconds for ``/approvals/{id}`` status checks.
    transport_policy:
        Optional transport behavior (retry policy, timeout/error actions,
        allowed hosts, auth token, HTTP hardening).
    transport:
        Inject a fully custom transport implementation.
    """

    _APPROVE = "✅ Approve"
    _DENY = "❌ Deny"

    def __init__(
        self,
        bridge_url: str = "http://localhost:9003",
        timeout: float = 300.0,
        api_mode: str = "auto",
        poll_interval: float = 1.0,
        transport_policy: TransportPolicy | None = None,
        transport: ApprovalTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self.transport = transport or build_whatsapp_transport(
            bridge_url=bridge_url,
            api_mode=api_mode,
            poll_interval=poll_interval,
            policy=transport_policy,
        )

    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        question = self._build_question(ctx)
        return await self.transport.request(
            question=question,
            timeout=self.timeout,
            tool_name=ctx.tool_name,
        )

    def _build_question(self, ctx: ApprovalContext) -> str:
        risk_emoji = _RISK_EMOJI.get(ctx.risk, "⚪")
        lines = [
            "🔐 Approve tool call?",
            f"{risk_emoji} {ctx.risk.upper()} RISK  |  {ctx.server_name} / {ctx.tool_name}",
        ]
        if ctx.description:
            lines.append(f"📄 {ctx.description}")
        if ctx.reason:
            lines.append(f"💬 {ctx.reason}")
        if ctx.args:
            short = json.dumps(ctx.args, ensure_ascii=False)
            if len(short) > 200:
                short = short[:200] + "…"
            lines.append(f"🔧 Args: {short}")
        if ctx.annotations and getattr(ctx.annotations, "destructiveHint", False):
            lines.append("⚠️ Marked destructive by server")
        lines.extend(["", f"Reply with: {self._APPROVE}  or  {self._DENY}"])
        return "\n".join(lines)


# ── WAHA (WhatsApp HTTP API) engine ───────────────────────────────────────────

_WAHA_APPROVE_WORDS = frozenset({"👍", "✅", "yes", "YES", "Yes", "ok", "OK", "approve", "y", "Y"})
_WAHA_DENY_WORDS = frozenset({"❌", "no", "NO", "No", "deny", "denied", "n", "N", "cancel"})


class WAHAEngine(ApprovalEngine):
    """WhatsApp approval via WAHA (WhatsApp HTTP API) — text messages only.

    Sends a text message to ``chat_id`` using the WAHA REST API, then polls
    for a text reply containing an approval or denial keyword.

    Uses **text messages** — not WhatsApp polls — so it works with the NOWEB
    engine which cannot decrypt poll vote updates.

    Parameters
    ----------
    waha_url:
        Base URL of the WAHA service (e.g. ``http://waha:3000``).
        Accessible from inside Docker via the service name.
    chat_id:
        WhatsApp chat JID to send to.  Format: ``18128035718@c.us`` for
        individuals, ``XXXXXXXXXX-XXXXXXXXXX@g.us`` for groups.
        WAHA normalises ``@s.whatsapp.net`` → ``@c.us`` automatically.
    session:
        WAHA session name (default: ``"default"``).
    api_key:
        WAHA API key (``X-Api-Key`` header) when auth is enabled.
    timeout:
        Total seconds to wait for a reply (default: 300 = 5 minutes).
    poll_interval:
        Seconds between message-list polls (default: 2).

    Approval / denial keywords
    --------------------------
    Approve: 👍  ✅  yes  ok  approve  y  (case variants)
    Deny:    ❌  no  deny  denied  cancel  n  (case variants)
    """

    def __init__(
        self,
        waha_url: str = "http://waha:3000",
        chat_id: str = "",
        session: str = "default",
        api_key: str = "",
        timeout: float = 300.0,
        poll_interval: float = 2.0,
    ) -> None:
        if not chat_id:
            raise ValueError("WAHAEngine requires chat_id (e.g. '18128035718@c.us')")
        self.waha_url = waha_url.rstrip("/")
        # Normalise @s.whatsapp.net → @c.us (WAHA uses @c.us for individuals)
        self.chat_id = chat_id.replace("@s.whatsapp.net", "@c.us")
        self.session = session
        self.api_key = api_key
        self.timeout = timeout
        self.poll_interval = poll_interval
        # Serialise approvals — only one pending request at a time.
        # asyncio.Lock() is safe to create without a running loop in Python 3.10+.
        self._lock = asyncio.Lock()

    def _get_lock(self) -> asyncio.Lock:
        return self._lock

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    def _build_message(self, ctx: ApprovalContext) -> str:
        risk_emoji = _RISK_EMOJI.get(ctx.risk, "⚪")
        action = ctx.args.get("action_summary") or ctx.reason or ctx.description or ctx.tool_name
        other_args = {k: v for k, v in ctx.args.items() if k != "action_summary"}
        if len(other_args) == 1:
            detail = str(next(iter(other_args.values())))[:250]
        elif other_args:
            detail = json.dumps(other_args, ensure_ascii=False)[:250]
        else:
            detail = ""
        lines = [
            f"🔐 *Approval Required* {risk_emoji} {ctx.risk.upper()} RISK",
            "",
            f"📋 *What:* {action}",
            f"🔧 *Tool:* `{ctx.tool_name}`",
        ]
        if detail:
            lines.append(f"⚡ `{detail}`")
        lines += ["", "Reply 👍 to approve or ❌ to deny"]
        return "\n".join(lines)

    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        import httpx

        message = self._build_message(ctx)

        async with self._get_lock():
            # Record time just before sending so we only look at replies after this
            import time as _time

            sent_at = int(_time.time())

            # ── Send text message ─────────────────────────────────────────────
            send_url = f"{self.waha_url}/api/sendText"
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        send_url,
                        json={"chatId": self.chat_id, "text": message, "session": self.session},
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                print(
                    f"[approval-proxy] WAHAEngine: approval request sent for `{ctx.tool_name}`",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(
                    f"[approval-proxy] WAHAEngine: send failed for `{ctx.tool_name}`: {exc}",
                    file=sys.stderr,
                )
                return None  # Fall through to next engine

            # ── Poll for reply ────────────────────────────────────────────────
            messages_url = f"{self.waha_url}/api/{self.session}/chats/{self.chat_id}/messages"
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout

            while loop.time() < deadline:
                await asyncio.sleep(self.poll_interval)
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(
                            messages_url,
                            params={"limit": 20, "downloadMedia": "false"},
                            headers=self._headers(),
                        )
                        resp.raise_for_status()
                        msgs = resp.json()
                    # msgs is a list, newest last or newest first depending on WAHA version
                    for m in msgs:
                        if m.get("fromMe"):
                            continue  # skip our own messages
                        ts = m.get("timestamp") or m.get("t") or 0
                        try:
                            ts_int = int(ts)
                            # Normalize milliseconds to seconds
                            if ts_int > 1_000_000_000_000:
                                ts_int = ts_int // 1000
                        except (ValueError, TypeError):
                            ts_int = 0
                        if ts_int < sent_at:
                            continue  # message predates our request
                        text = (m.get("body") or m.get("text") or "").strip().lower()
                        if text in _WAHA_APPROVE_WORDS:
                            print(
                                f"[approval-proxy] WAHAEngine: ✅ approved `{ctx.tool_name}` (reply: {text!r})",
                                file=sys.stderr,
                            )
                            return True
                        if text in _WAHA_DENY_WORDS:
                            print(
                                f"[approval-proxy] WAHAEngine: ❌ denied `{ctx.tool_name}` (reply: {text!r})",
                                file=sys.stderr,
                            )
                            return False
                except Exception as exc:
                    print(f"[approval-proxy] WAHAEngine: poll error: {exc}", file=sys.stderr)

            print(f"[approval-proxy] WAHAEngine: ⏱ timeout for `{ctx.tool_name}`", file=sys.stderr)
            return False


# ── Webhook engine ───────────────────────────────────────────────────────────


class WebhookEngine(ApprovalEngine):
    """Send approval requests to a webhook using MCP elicitation/create format.

    Posts a JSON request in the MCP ``ElicitRequestFormParams`` schema format
    (Nov 2025 MCP spec) and expects a response in the ``ElicitResult`` format.

    POST body::

        {
          "mode": "form",
          "message": "<approval question>",
          "requestedSchema": {
            "type": "object",
            "properties": {
              "approved": {"type": "boolean", "title": "Approve", "description": "Approve or deny"},
              "reason": {"type": "string", "title": "Reason", "description": "Optional reason"}
            },
            "required": ["approved"]
          }
        }

    Expected response (``ElicitResult`` format)::

        {
          "action": "accept|decline|cancel",
          "content": {"approved": true, "reason": "..."}
        }

    Result determination:
    - ``action=accept`` + ``content.approved=true`` → ``True``
    - ``action=accept`` + ``content.approved=false`` → ``False``
    - ``action=decline`` or ``cancel`` → ``False``

    Parameters
    ----------
    url:
        Webhook URL to POST the elicitation request to.
    timeout:
        Seconds to wait for the webhook response (default: 120).
    headers:
        Optional dict of additional HTTP headers to include.
    """

    def __init__(
        self,
        url: str,
        timeout: float = 120.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.headers = headers or {}

    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        import httpx

        message = _build_elicitation_message(
            server_name=ctx.server_name,
            tool_name=ctx.tool_name,
            tool_args=ctx.args,
            description=ctx.description,
            annotations=ctx.annotations,
            risk=ctx.risk,
            reason=ctx.reason,
        )

        request_body = {
            "mode": "form",
            "message": message,
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "approved": {
                        "type": "boolean",
                        "title": "Approve",
                        "description": "Approve or deny",
                    },
                    "reason": {
                        "type": "string",
                        "title": "Reason",
                        "description": "Optional reason",
                    },
                },
                "required": ["approved"],
            },
        }

        try:
            headers = {"Content-Type": "application/json", **self.headers}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=request_body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            print(
                f"[approval-proxy] WebhookEngine: request failed for `{ctx.tool_name}`: {exc}",
                file=sys.stderr,
            )
            return None  # Fall through to next engine

        # Parse ElicitResult response
        action = data.get("action", "").lower()
        if action in {"decline", "cancel"}:
            print(
                f"[approval-proxy] WebhookEngine: ❌ denied `{ctx.tool_name}` (action={action!r})",
                file=sys.stderr,
            )
            return False

        if action == "accept":
            content = data.get("content", {})
            approved = content.get("approved", False)
            print(
                f"[approval-proxy] WebhookEngine: {'✅ approved' if approved else '❌ denied'} `{ctx.tool_name}`",
                file=sys.stderr,
            )
            return bool(approved)

        print(
            f"[approval-proxy] WebhookEngine: unknown action {action!r} for `{ctx.tool_name}`",
            file=sys.stderr,
        )
        return False


# ── Chained engine ────────────────────────────────────────────────────────────


class ChainedEngine(ApprovalEngine):
    """Try approval engines in sequence; use the first definitive result.

    If an engine returns ``None`` (cannot decide / error), the next engine
    is tried.  If *all* engines return ``None``, ``default`` is returned
    (``False`` by default — deny).

    Example — elicitation first, WhatsApp as fallback::

        engine = ChainedEngine([
            ElicitationEngine(timeout=30),
            WhatsAppEngine(bridge_url="http://localhost:9003"),
        ])
    """

    def __init__(self, engines: list[ApprovalEngine], default: bool = False) -> None:
        if not engines:
            raise ValueError("ChainedEngine requires at least one engine")
        self.engines = engines
        self.default = default

    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        for engine in self.engines:
            result = await engine.request_approval(ctx)
            if result is not None:
                return result
        # All engines indeterminate
        return self.default


# ── Callback engine ───────────────────────────────────────────────────────────


class CallbackEngine(ApprovalEngine):
    """Delegates approval to an async callback function.

    Use this to integrate custom approval logic without subclassing.
    The callback receives an :class:`ApprovalContext` and must return
    ``True`` (approved), ``False`` (denied), or ``None`` (indeterminate).

    Example — poll-based approval via a chat adapter::

        async def poll_approve(ctx: ApprovalContext) -> bool | None:
            question = f"Allow {ctx.tool_name}?"
            poll_id = await adapter.send_poll(host_jid, question, ["Yes", "No"])
            # ... wait for poll response ...
            return result

        engine = CallbackEngine(poll_approve)
        middleware = ApprovalMiddleware(engine=engine)

    Example — simple auto-approve for low risk::

        async def auto_approve(ctx: ApprovalContext) -> bool | None:
            return True if ctx.risk == "low" else None  # fall through to next engine

        engine = ChainedEngine([CallbackEngine(auto_approve), ElicitationEngine()])
    """

    def __init__(self, callback: Any) -> None:
        self._callback = callback

    async def request_approval(self, ctx: ApprovalContext) -> bool | None:
        return await self._callback(ctx)


# ── Shared helpers ────────────────────────────────────────────────────────────

_RISK_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢", "unknown": "⚪"}


async def _client_supports_elicitation(ctx: Any) -> bool:
    """Return True if the connected MCP client supports elicitation/create."""
    try:
        return await ctx.client_supports_extension("elicitation")
    except Exception:
        pass
    try:
        caps = ctx.session.client_params.capabilities
        return caps is not None and getattr(caps, "elicitation", None) is not None
    except Exception:
        return False


def _build_elicitation_message(
    server_name: str,
    tool_name: str,
    tool_args: dict,
    description: str,
    annotations: mt.ToolAnnotations | None,
    risk: str = "unknown",
    reason: str = "",
) -> str:
    """Build the markdown message shown in the MCP elicitation dialog."""
    emoji = _RISK_EMOJI.get(risk, "⚪")
    risk_label = risk.upper()

    lines = [
        f"🔐 **Approval required** — {emoji} {risk_label} RISK",
        "",
        f"**Server:** `{server_name}`  |  **Tool:** `{tool_name}`",
    ]
    if description:
        lines.append(f"*{description}*")
    if reason:
        lines.append(f"💬 _{reason}_")

    hints: list[str] = []
    if annotations:
        if annotations.destructiveHint:
            hints.append("⚠️ marked **destructive** by server")
        if annotations.readOnlyHint:
            hints.append("✅ marked **read-only** by server")
        if getattr(annotations, "idempotentHint", None):
            hints.append("♻️ idempotent")
    if hints:
        lines.append("  ".join(hints))

    if tool_args:
        pretty = json.dumps(tool_args, indent=2, ensure_ascii=False)
        if len(pretty) > 600:
            pretty = pretty[:600] + "\n  … (truncated)"
        lines.append(f"\n**Arguments:**\n```json\n{pretty}\n```")

    lines.append("\nAllow this tool call?")
    return "\n".join(lines)
