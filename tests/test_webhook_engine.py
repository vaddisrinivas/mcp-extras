"""Tests for WebhookEngine."""

from __future__ import annotations

import pytest

from mcp_approval_proxy.engines import ApprovalContext, WebhookEngine


@pytest.mark.asyncio
async def test_webhook_engine_approve_accept_true(httpx_mock):
    """Test WebhookEngine approval with action=accept and approved=true."""
    webhook_url = "http://localhost:8080/approve"
    engine = WebhookEngine(url=webhook_url, timeout=10.0)

    httpx_mock.add_response(
        method="POST",
        url=webhook_url,
        json={"action": "accept", "content": {"approved": True, "reason": "looks good"}},
        status_code=200,
    )

    ctx = ApprovalContext(
        server_name="test_server",
        tool_name="delete_file",
        args={"path": "/tmp/test.txt"},
        risk="high",
        description="Delete a file",
    )

    result = await engine.request_approval(ctx)
    assert result is True


@pytest.mark.asyncio
async def test_webhook_engine_deny_accept_false(httpx_mock):
    """Test WebhookEngine denial with action=accept and approved=false."""
    webhook_url = "http://localhost:8080/approve"
    engine = WebhookEngine(url=webhook_url, timeout=10.0)

    httpx_mock.add_response(
        method="POST",
        url=webhook_url,
        json={"action": "accept", "content": {"approved": False, "reason": "too risky"}},
        status_code=200,
    )

    ctx = ApprovalContext(
        server_name="test_server",
        tool_name="delete_file",
        args={"path": "/tmp/test.txt"},
        risk="high",
    )

    result = await engine.request_approval(ctx)
    assert result is False


@pytest.mark.asyncio
async def test_webhook_engine_deny_action_decline(httpx_mock):
    """Test WebhookEngine denial with action=decline."""
    webhook_url = "http://localhost:8080/approve"
    engine = WebhookEngine(url=webhook_url, timeout=10.0)

    httpx_mock.add_response(
        method="POST",
        url=webhook_url,
        json={"action": "decline"},
        status_code=200,
    )

    ctx = ApprovalContext(
        server_name="test_server",
        tool_name="delete_file",
        args={"path": "/tmp/test.txt"},
    )

    result = await engine.request_approval(ctx)
    assert result is False


@pytest.mark.asyncio
async def test_webhook_engine_deny_action_cancel(httpx_mock):
    """Test WebhookEngine denial with action=cancel."""
    webhook_url = "http://localhost:8080/approve"
    engine = WebhookEngine(url=webhook_url, timeout=10.0)

    httpx_mock.add_response(
        method="POST",
        url=webhook_url,
        json={"action": "cancel"},
        status_code=200,
    )

    ctx = ApprovalContext(
        server_name="test_server",
        tool_name="delete_file",
        args={"path": "/tmp/test.txt"},
    )

    result = await engine.request_approval(ctx)
    assert result is False


@pytest.mark.asyncio
async def test_webhook_engine_http_error_returns_none(httpx_mock):
    """Test WebhookEngine returns None on HTTP error (allows fallthrough)."""
    webhook_url = "http://localhost:8080/approve"
    engine = WebhookEngine(url=webhook_url, timeout=10.0)

    httpx_mock.add_response(
        method="POST",
        url=webhook_url,
        status_code=500,
        json={"error": "Internal Server Error"},
    )

    ctx = ApprovalContext(
        server_name="test_server",
        tool_name="delete_file",
        args={"path": "/tmp/test.txt"},
    )

    result = await engine.request_approval(ctx)
    assert result is None


@pytest.mark.asyncio
async def test_webhook_engine_custom_headers(httpx_mock):
    """Test WebhookEngine includes custom headers."""
    webhook_url = "http://localhost:8080/approve"
    engine = WebhookEngine(
        url=webhook_url,
        timeout=10.0,
        headers={"Authorization": "Bearer secret123"},
    )

    httpx_mock.add_response(
        method="POST",
        url=webhook_url,
        json={"action": "accept", "content": {"approved": True}},
        status_code=200,
    )

    ctx = ApprovalContext(
        server_name="test_server",
        tool_name="test_tool",
        args={},
    )

    result = await engine.request_approval(ctx)
    assert result is True

    # Verify the custom header was sent
    request = httpx_mock.get_request()
    assert request.headers.get("Authorization") == "Bearer secret123"
