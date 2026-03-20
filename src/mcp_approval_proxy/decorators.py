"""Tool-level approval hints via the ``@approval_required`` decorator.

Apply ``@approval_required`` to FastMCP tool functions to attach inline
approval metadata.  When you call
:meth:`ApprovalMiddleware.register_from_server` the middleware scans the
server's tools, reads this metadata, and updates its policy accordingly —
no config file needed.

Quick reference
---------------
.. code-block:: python

    from fastmcp import FastMCP
    from mcp_approval_proxy.decorators import approval_required

    server = FastMCP(name="my-server")

    # Always gate — even in mode="none"
    @server.tool()
    @approval_required(force=True, risk="high", reason="Removes a file permanently")
    def delete_file(path: str) -> str: ...

    # Hard-block — never runs, no elicitation
    @server.tool()
    @approval_required(always_deny=True)
    def drop_database() -> str: ...

    # Whitelist — bypass all gating regardless of mode
    @server.tool()
    @approval_required(always_allow=True)
    def list_files(path: str) -> list[str]: ...

    # Override risk + add a ToolAnnotations hint
    @server.tool()
    @approval_required(
        risk="medium",
        annotations={"destructiveHint": True},
    )
    def overwrite_config(content: str) -> str: ...

Metadata storage
----------------
The decorator writes a dict to ``func.__approval_meta__``.
:meth:`ApprovalMiddleware.register_from_server` reads this attribute when it
introspects a FastMCP server's tool manager.  The metadata travels with the
*function object*, so it only works for **in-process** servers — not for
subprocess or HTTP upstreams (use ``customAnnotations`` in config for those).
"""

from __future__ import annotations

from typing import Any

#: Attribute name written onto decorated functions.
APPROVAL_META_ATTR = "__approval_meta__"


def approval_required(
    *,
    force: bool = False,
    always_allow: bool = False,
    always_deny: bool = False,
    risk: str | None = None,
    reason: str | None = None,
    annotations: dict[str, Any] | None = None,
):
    """Attach approval metadata to a FastMCP tool function.

    Only **one** of ``force``, ``always_allow``, and ``always_deny`` may be
    ``True`` at the same time.

    Parameters
    ----------
    force:
        Always require approval for this tool, regardless of mode or name
        heuristics.  Useful for tools that look harmless but are not.
    always_allow:
        Always pass this tool through without approval.  Takes effect even
        when ``mode="all"``.
    always_deny:
        Hard-block this tool.  No elicitation is sent; the call returns an
        error immediately.
    risk:
        Override the computed risk level: ``"high"``, ``"medium"``, or
        ``"low"``.  Shown in the elicitation dialog and audit log.
    reason:
        A short human-readable explanation displayed in the elicitation
        dialog and logged to the audit file.
    annotations:
        Explicit ``ToolAnnotations``-compatible field overrides, e.g.
        ``{"destructiveHint": True, "readOnlyHint": False}``.
    """
    if sum([force, always_allow, always_deny]) > 1:
        raise ValueError(
            "At most one of force / always_allow / always_deny may be True in @approval_required"
        )

    meta: dict[str, Any] = {
        "force": force,
        "always_allow": always_allow,
        "always_deny": always_deny,
        "risk": risk,
        "reason": reason,
        "annotations": annotations or {},
    }

    def decorator(func):
        setattr(func, APPROVAL_META_ATTR, meta)
        return func

    return decorator
