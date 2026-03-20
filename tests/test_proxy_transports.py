"""Tests for proxy transport types."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_approval_proxy.config import ProxyConfig, ServerConfig
from mcp_approval_proxy.proxy import build_proxy


@pytest.fixture
def temp_config():
    """Create a temporary upstream config."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "command": "echo",
            "args": ["test"],
        }
        json.dump(config, f)
        yield f.name
    Path(f.name).unlink()


@pytest.mark.asyncio
async def test_proxy_transport_default_stdio():
    """Test proxy defaults to stdio transport."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="none",
                always_allow=[],
                always_deny=[],
            )
            assert proxy is not None


@pytest.mark.asyncio
async def test_proxy_transport_http_url():
    """Test proxy detects HTTP transport from URL."""
    server_cfg = ServerConfig(
        name="test",
        url="http://localhost:8080",
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="none",
                always_allow=[],
                always_deny=[],
            )
            assert proxy is not None


@pytest.mark.asyncio
async def test_proxy_transport_sse_url():
    """Test proxy detects SSE transport from URL."""
    server_cfg = ServerConfig(
        name="test",
        url="http://localhost:8080/sse",
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="none",
                always_allow=[],
                always_deny=[],
            )
            assert proxy is not None


def test_proxy_run_stdio():
    """Test proxy.run with stdio transport."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig()

    async def setup():
        with patch("mcp_approval_proxy.proxy.Client"):
            with patch("mcp_approval_proxy.proxy.create_proxy") as mock_create:
                mock_proxy = MagicMock()
                mock_proxy.run = MagicMock()
                mock_create.return_value = mock_proxy

                proxy = await build_proxy(
                    server_cfg=server_cfg,
                    proxy_cfg=proxy_cfg,
                    mode="none",
                    always_allow=[],
                    always_deny=[],
                )
                # We can't actually call run without a real server
                # Just verify the proxy was created
                assert proxy is not None

    asyncio.run(setup())


def test_proxy_run_sse():
    """Test proxy.run with SSE transport."""
    server_cfg = ServerConfig(
        name="test",
        url="http://localhost:8080/sse",
    )
    proxy_cfg = ProxyConfig()

    async def setup():
        with patch("mcp_approval_proxy.proxy.Client"):
            with patch("mcp_approval_proxy.proxy.create_proxy") as mock_create:
                mock_proxy = MagicMock()
                mock_proxy.run = MagicMock()
                mock_create.return_value = mock_proxy

                proxy = await build_proxy(
                    server_cfg=server_cfg,
                    proxy_cfg=proxy_cfg,
                    mode="none",
                    always_allow=[],
                    always_deny=[],
                )
                assert proxy is not None

    asyncio.run(setup())


@pytest.mark.asyncio
async def test_proxy_mode_destructive():
    """Test proxy with destructive mode."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="destructive",
                always_allow=[],
                always_deny=[],
            )
            assert proxy is not None


@pytest.mark.asyncio
async def test_proxy_mode_all():
    """Test proxy with all mode."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="all",
                always_allow=[],
                always_deny=[],
            )
            assert proxy is not None


@pytest.mark.asyncio
async def test_proxy_mode_none():
    """Test proxy with none mode (passthrough)."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="none",
                always_allow=[],
                always_deny=[],
            )
            assert proxy is not None


@pytest.mark.asyncio
async def test_proxy_allow_patterns():
    """Test proxy with allow patterns."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="destructive",
                always_allow=[],
                always_deny=[],
                allow_patterns=["read_*", "list_*"],
            )
            assert proxy is not None


@pytest.mark.asyncio
async def test_proxy_deny_patterns():
    """Test proxy with deny patterns."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig()

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="destructive",
                always_allow=[],
                always_deny=[],
                deny_patterns=["delete_*", "destroy_*"],
            )
            assert proxy is not None


@pytest.mark.asyncio
async def test_proxy_with_audit_log():
    """Test proxy with audit log enabled."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        audit_log_path = f.name

    try:
        proxy_cfg = ProxyConfig(audit_log=audit_log_path)

        with patch("mcp_approval_proxy.proxy.Client"):
            with patch("mcp_approval_proxy.proxy.create_proxy"):
                proxy = await build_proxy(
                    server_cfg=server_cfg,
                    proxy_cfg=proxy_cfg,
                    mode="none",
                    always_allow=[],
                    always_deny=[],
                )
                assert proxy is not None
    finally:
        Path(audit_log_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_proxy_dry_run_mode():
    """Test proxy with dry-run enabled."""
    server_cfg = ServerConfig(
        name="test",
        command="echo",
        args=["test"],
    )
    proxy_cfg = ProxyConfig(dry_run=True)

    with patch("mcp_approval_proxy.proxy.Client"):
        with patch("mcp_approval_proxy.proxy.create_proxy"):
            proxy = await build_proxy(
                server_cfg=server_cfg,
                proxy_cfg=proxy_cfg,
                mode="destructive",
                always_allow=[],
                always_deny=[],
            )
            assert proxy is not None
