"""Tests for ApprovalEngine implementations and helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_approval_proxy.engines import (
    ApprovalContext,
    ChainedEngine,
    ElicitationEngine,
    WhatsAppEngine,
    _build_elicitation_message,
    _client_supports_elicitation,
)
from mcp_approval_proxy.transports import TransportPolicy


def _ctx(**kwargs) -> ApprovalContext:
    defaults = {
        "server_name": "test-server",
        "tool_name": "write_file",
        "args": {"path": "/tmp/x", "content": "hello"},
        "risk": "medium",
        "description": "Write a file",
    }
    return ApprovalContext(**{**defaults, **kwargs})


def _fastmcp_ctx(supports=True, elicit_result=None):
    from fastmcp.server.elicitation import AcceptedElicitation

    ctx = AsyncMock()
    ctx.client_supports_extension = AsyncMock(return_value=supports)
    if elicit_result is None:
        elicit_result = AcceptedElicitation(data=True)
    ctx.elicit = AsyncMock(return_value=elicit_result)
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# ElicitationEngine
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestElicitationEngine:
    async def test_approved_returns_true(self):
        from fastmcp.server.elicitation import AcceptedElicitation

        engine = ElicitationEngine()
        fctx = _fastmcp_ctx(elicit_result=AcceptedElicitation(data=True))
        ctx = _ctx(fastmcp_context=fctx)
        assert await engine.request_approval(ctx) is True

    async def test_denied_returns_false(self):
        from fastmcp.server.elicitation import AcceptedElicitation

        engine = ElicitationEngine()
        fctx = _fastmcp_ctx(elicit_result=AcceptedElicitation(data=False))
        ctx = _ctx(fastmcp_context=fctx)
        assert await engine.request_approval(ctx) is False

    async def test_declined_returns_false(self):
        from fastmcp.server.elicitation import DeclinedElicitation

        engine = ElicitationEngine()
        fctx = _fastmcp_ctx(elicit_result=DeclinedElicitation())
        ctx = _ctx(fastmcp_context=fctx)
        assert await engine.request_approval(ctx) is False

    async def test_no_context_returns_none(self):
        engine = ElicitationEngine()
        ctx = _ctx(fastmcp_context=None)
        assert await engine.request_approval(ctx) is None

    async def test_client_no_elicitation_returns_none(self):
        engine = ElicitationEngine()
        fctx = _fastmcp_ctx(supports=False)
        ctx = _ctx(fastmcp_context=fctx)
        assert await engine.request_approval(ctx) is None

    async def test_timeout_deny_action(self):
        import asyncio

        engine = ElicitationEngine(timeout=0.01, timeout_action="deny")
        fctx = AsyncMock()
        fctx.client_supports_extension = AsyncMock(return_value=True)

        async def _slow_elicit(*a, **kw):
            await asyncio.sleep(10)
            return None

        fctx.elicit = _slow_elicit
        ctx = _ctx(fastmcp_context=fctx)
        result = await engine.request_approval(ctx)
        assert result is False

    async def test_timeout_approve_action(self):
        import asyncio

        engine = ElicitationEngine(timeout=0.01, timeout_action="approve")
        fctx = AsyncMock()
        fctx.client_supports_extension = AsyncMock(return_value=True)

        async def _slow_elicit(*a, **kw):
            await asyncio.sleep(10)
            return None

        fctx.elicit = _slow_elicit
        ctx = _ctx(fastmcp_context=fctx)
        result = await engine.request_approval(ctx)
        assert result is True

    async def test_exception_during_elicitation_returns_none(self):
        engine = ElicitationEngine()
        fctx = AsyncMock()
        fctx.client_supports_extension = AsyncMock(return_value=True)
        fctx.elicit = AsyncMock(side_effect=RuntimeError("boom"))
        ctx = _ctx(fastmcp_context=fctx)
        result = await engine.request_approval(ctx)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# WhatsAppEngine
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestWhatsAppEngine:
    async def test_approve_vote(self):
        engine = WhatsAppEngine(bridge_url="http://localhost:9003")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choice": "✅ Approve"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await engine.request_approval(_ctx())
        assert result is True

    async def test_deny_vote(self):
        engine = WhatsAppEngine(bridge_url="http://localhost:9003")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choice": "❌ Deny"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await engine.request_approval(_ctx())
        assert result is False

    async def test_network_error_returns_none_when_fallback(self):
        engine = WhatsAppEngine(transport_policy=TransportPolicy(on_transport_error="fallback"))

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("network failure"))
            mock_client_cls.return_value = mock_client

            result = await engine.request_approval(_ctx())
        assert result is None

    async def test_network_error_returns_false_when_no_fallback(self):
        engine = WhatsAppEngine(transport_policy=TransportPolicy(on_transport_error="deny"))

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("network failure"))
            mock_client_cls.return_value = mock_client

            result = await engine.request_approval(_ctx())
        assert result is False

    async def test_build_question_includes_risk(self):
        engine = WhatsAppEngine()
        ctx = _ctx(risk="high", description="Permanently deletes file")
        question = engine._build_question(ctx)
        assert "HIGH" in question
        assert "🔴" in question
        assert "write_file" in question

    async def test_build_question_includes_reason(self):
        engine = WhatsAppEngine()
        ctx = _ctx(reason="This is dangerous")
        question = engine._build_question(ctx)
        assert "This is dangerous" in question

    async def test_build_question_truncates_long_args(self):
        engine = WhatsAppEngine()
        ctx = _ctx(args={"data": "x" * 500})
        question = engine._build_question(ctx)
        assert "…" in question

    async def test_approvals_api_mode_approved(self):
        engine = WhatsAppEngine(
            bridge_url="http://localhost:9003",
            api_mode="approvals",
            poll_interval=0.001,
        )

        create_resp = MagicMock()
        create_resp.json.return_value = {"id": "appr-1"}
        create_resp.raise_for_status = MagicMock()

        pending_resp = MagicMock()
        pending_resp.status_code = 200
        pending_resp.json.return_value = {"status": "pending"}
        pending_resp.raise_for_status = MagicMock()

        approved_resp = MagicMock()
        approved_resp.status_code = 200
        approved_resp.json.return_value = {"status": "approved"}
        approved_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=create_resp)
            mock_client.get = AsyncMock(side_effect=[pending_resp, approved_resp])
            mock_client_cls.return_value = mock_client

            result = await engine.request_approval(_ctx())

        assert result is True
        mock_client.post.assert_awaited_once()
        assert mock_client.get.await_count >= 2

    async def test_auto_mode_falls_back_to_approvals_api(self):
        engine = WhatsAppEngine(
            bridge_url="http://localhost:9003",
            api_mode="auto",
            poll_interval=0.001,
        )

        create_resp = MagicMock()
        create_resp.json.return_value = {"id": "appr-2"}
        create_resp.raise_for_status = MagicMock()

        denied_resp = MagicMock()
        denied_resp.status_code = 200
        denied_resp.json.return_value = {"status": "denied"}
        denied_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            # First legacy endpoint fails, then approvals API is used.
            mock_client.post = AsyncMock(side_effect=[Exception("404"), create_resp])
            mock_client.get = AsyncMock(return_value=denied_resp)
            mock_client_cls.return_value = mock_client

            result = await engine.request_approval(_ctx())

        assert result is False
        assert mock_client.post.await_count == 2
        mock_client.get.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# ChainedEngine
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestChainedEngine:
    async def test_first_engine_approves(self):
        e1 = AsyncMock(spec=ElicitationEngine)
        e1.request_approval = AsyncMock(return_value=True)
        e2 = AsyncMock(spec=WhatsAppEngine)
        chain = ChainedEngine([e1, e2])
        assert await chain.request_approval(_ctx()) is True
        e2.request_approval.assert_not_awaited()

    async def test_first_engine_none_falls_through(self):
        e1 = AsyncMock(spec=ElicitationEngine)
        e1.request_approval = AsyncMock(return_value=None)
        e2 = AsyncMock(spec=WhatsAppEngine)
        e2.request_approval = AsyncMock(return_value=True)
        chain = ChainedEngine([e1, e2])
        assert await chain.request_approval(_ctx()) is True
        e2.request_approval.assert_awaited_once()

    async def test_all_none_returns_default_false(self):
        e1 = AsyncMock()
        e1.request_approval = AsyncMock(return_value=None)
        e2 = AsyncMock()
        e2.request_approval = AsyncMock(return_value=None)
        chain = ChainedEngine([e1, e2], default=False)
        assert await chain.request_approval(_ctx()) is False

    async def test_all_none_custom_default(self):
        e1 = AsyncMock()
        e1.request_approval = AsyncMock(return_value=None)
        chain = ChainedEngine([e1], default=True)
        assert await chain.request_approval(_ctx()) is True

    async def test_first_denies_second_not_called(self):
        e1 = AsyncMock()
        e1.request_approval = AsyncMock(return_value=False)
        e2 = AsyncMock()
        chain = ChainedEngine([e1, e2])
        assert await chain.request_approval(_ctx()) is False
        e2.request_approval.assert_not_awaited()

    async def test_empty_engines_raises(self):
        with pytest.raises(ValueError, match="at least one engine"):
            ChainedEngine([])


# ─────────────────────────────────────────────────────────────────────────────
# Helper: _client_supports_elicitation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestClientSupportsElicitation:
    async def test_extension_check_true(self):
        ctx = AsyncMock()
        ctx.client_supports_extension = AsyncMock(return_value=True)
        assert await _client_supports_elicitation(ctx) is True

    async def test_extension_check_false(self):
        ctx = AsyncMock()
        ctx.client_supports_extension = AsyncMock(return_value=False)
        assert await _client_supports_elicitation(ctx) is False

    async def test_falls_back_to_capabilities(self):
        ctx = MagicMock()
        ctx.client_supports_extension = AsyncMock(side_effect=AttributeError)
        caps = MagicMock()
        caps.elicitation = MagicMock()
        ctx.session.client_params.capabilities = caps
        assert await _client_supports_elicitation(ctx) is True

    async def test_returns_false_on_all_errors(self):
        ctx = MagicMock()
        ctx.client_supports_extension = AsyncMock(side_effect=Exception)
        ctx.session.client_params.capabilities = None
        assert await _client_supports_elicitation(ctx) is False


# ─────────────────────────────────────────────────────────────────────────────
# Helper: _build_elicitation_message
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildElicitationMessage:
    def test_contains_tool_name(self):
        msg = _build_elicitation_message(
            server_name="srv",
            tool_name="my_tool",
            tool_args={},
            description="",
            annotations=None,
        )
        assert "my_tool" in msg

    def test_contains_risk(self):
        msg = _build_elicitation_message(
            server_name="srv",
            tool_name="t",
            tool_args={},
            description="",
            annotations=None,
            risk="high",
        )
        assert "HIGH" in msg
        assert "🔴" in msg

    def test_contains_reason(self):
        msg = _build_elicitation_message(
            server_name="srv",
            tool_name="t",
            tool_args={},
            description="",
            annotations=None,
            reason="This is risky",
        )
        assert "This is risky" in msg

    def test_args_included(self):
        msg = _build_elicitation_message(
            server_name="srv",
            tool_name="t",
            tool_args={"path": "/tmp/x"},
            description="",
            annotations=None,
        )
        assert "/tmp/x" in msg

    def test_long_args_truncated(self):
        msg = _build_elicitation_message(
            server_name="srv",
            tool_name="t",
            tool_args={"data": "x" * 700},
            description="",
            annotations=None,
        )
        assert "truncated" in msg
