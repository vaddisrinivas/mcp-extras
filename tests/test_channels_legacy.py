"""Tests for legacy channels compatibility layer."""

from __future__ import annotations

import pytest

from mcp_extras.channels.cli import CliChannel
from mcp_extras.channels.webhook import WebhookChannel


def test_cli_channel_emits_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="legacy channels API"):
        CliChannel(auto_approve=True)


def test_webhook_channel_emits_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="legacy channels API"):
        WebhookChannel(url="http://localhost:9999/hook")
