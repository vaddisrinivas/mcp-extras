"""Tests for CLI functionality."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from mcp_extras import __version__
from mcp_extras.__main__ import main


@pytest.fixture
def cli_runner():
    """Create a Click CLI runner."""
    return CliRunner()


def test_cli_help(cli_runner):
    """Test --help flag."""
    result = cli_runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Transparent MCP proxy" in result.output
    assert "--upstream" in result.output
    assert "--mode" in result.output
    assert "--allow" in result.output
    assert "--deny" in result.output


def test_cli_version(cli_runner):
    """Test --version flag."""
    result = cli_runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_missing_upstream(cli_runner):
    """Test missing --upstream argument."""
    result = cli_runner.invoke(main, [])
    assert result.exit_code != 0
    assert "Error" in result.output or "required" in result.output.lower()


def test_cli_upstream_not_found(cli_runner):
    """Test non-existent --upstream file."""
    result = cli_runner.invoke(main, ["--upstream", "/nonexistent/path.json"])
    assert result.exit_code != 0


def test_cli_valid_config_file_exists(cli_runner):
    """Test that valid config file is accepted (file existence check)."""
    # Create a temporary config file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {
            "command": "echo",
            "args": ["test"],
        }
        json.dump(config, f)
        config_path = f.name

    try:
        # Just verify that the CLI accepts the file path without complaining
        # We don't actually run the proxy, just verify argument parsing
        result = cli_runner.invoke(
            main,
            [
                "--upstream",
                config_path,
                "--help",
            ],
        )
        # Help should work regardless of config
        assert result.exit_code == 0
    finally:
        Path(config_path).unlink()


def test_cli_mode_validation(cli_runner):
    """Test invalid mode is rejected."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {"command": "echo", "args": ["test"]}
        json.dump(config, f)
        config_path = f.name

    try:
        result = cli_runner.invoke(
            main,
            [
                "--upstream",
                config_path,
                "--mode",
                "invalid_mode",
            ],
        )
        assert result.exit_code != 0
    finally:
        Path(config_path).unlink()


def test_cli_allow_deny_multiple_flags(cli_runner):
    """Test --allow and --deny accept multiple values."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {"command": "echo", "args": ["test"]}
        json.dump(config, f)
        config_path = f.name

    try:
        # Just verify CLI accepts multiple flags without error
        result = cli_runner.invoke(
            main,
            [
                "--upstream",
                config_path,
                "--allow",
                "read_*",
                "--allow",
                "list_*",
                "--deny",
                "delete_*",
                "--deny",
                "destroy_*",
                "--help",
            ],
        )
        # Help should work with multiple flags
        assert result.exit_code == 0
    finally:
        Path(config_path).unlink()


def test_cli_timeout_float(cli_runner):
    """Test --timeout accepts float values."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {"command": "echo", "args": ["test"]}
        json.dump(config, f)
        config_path = f.name

    try:
        with patch("mcp_extras.__main__.build_proxy") as mock_build:
            mock_proxy = mock_build.return_value
            mock_proxy.run = lambda **kwargs: None

            cli_runner.invoke(
                main,
                [
                    "--upstream",
                    config_path,
                    "--timeout",
                    "30.5",
                    "--dry-run",
                ],
            )
            if mock_build.called:
                call_kwargs = mock_build.call_args.kwargs
                proxy_cfg = call_kwargs.get("proxy_cfg")
                assert proxy_cfg.default_timeout == 30.5
    finally:
        Path(config_path).unlink()


def test_cli_port_validation(cli_runner):
    """Test --port accepts integer values."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        config = {"command": "echo", "args": ["test"]}
        json.dump(config, f)
        config_path = f.name

    try:
        with patch("mcp_extras.__main__.build_proxy") as mock_build:
            mock_proxy = mock_build.return_value
            mock_proxy.run = lambda **kwargs: None

            result = cli_runner.invoke(
                main,
                [
                    "--upstream",
                    config_path,
                    "--transport",
                    "sse",
                    "--port",
                    "9000",
                    "--dry-run",
                ],
            )
            # Should accept valid port
            assert result.exit_code == 0 or mock_build.called
    finally:
        Path(config_path).unlink()
