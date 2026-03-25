"""Tests for WAHAEngine."""

from __future__ import annotations

import pytest

from mcp_extras.engines import WAHAEngine


def test_waha_engine_normalizes_chat_id():
    """Test WAHAEngine normalizes @s.whatsapp.net to @c.us."""
    engine = WAHAEngine(
        waha_url="http://localhost:3000",
        chat_id="1234567890@s.whatsapp.net",  # Should be normalized
    )
    assert engine.chat_id == "1234567890@c.us"


def test_waha_engine_preserves_c_us_chat_id():
    """Test WAHAEngine preserves @c.us chat ID."""
    engine = WAHAEngine(
        waha_url="http://localhost:3000",
        chat_id="1234567890@c.us",
    )
    assert engine.chat_id == "1234567890@c.us"


def test_waha_engine_requires_chat_id():
    """Test WAHAEngine requires chat_id."""
    with pytest.raises(ValueError, match="chat_id"):
        WAHAEngine(
            waha_url="http://localhost:3000",
            chat_id="",  # Empty chat_id should raise
        )


def test_waha_engine_constructor_args():
    """Test WAHAEngine constructor accepts all arguments."""
    engine = WAHAEngine(
        waha_url="http://localhost:3000",
        chat_id="1234567890@c.us",
        session="default",
        api_key="test_key",
        timeout=300.0,
        poll_interval=2.0,
    )
    assert engine.waha_url == "http://localhost:3000"
    assert engine.chat_id == "1234567890@c.us"
    assert engine.session == "default"
    assert engine.api_key == "test_key"
    assert engine.timeout == 300.0
    assert engine.poll_interval == 2.0


def test_waha_engine_has_lock():
    """Test WAHAEngine has a lock for serialization."""
    engine = WAHAEngine(
        waha_url="http://localhost:3000",
        chat_id="1234567890@c.us",
    )
    assert engine._get_lock() is not None
